"""Mnemonic vault that runs inside an enclave.

A user backs up a wallet mnemonic under an email and a password. If they
lose the mnemonic, the same email and password get it back.

A password is a weak secret, so the whole design rests on one rule: the
enclave allows at most MAX_ATTEMPTS wrong passwords per account, then
erases the backup. Nobody outside the enclave can guess faster, because
the only copy of the mnemonic is encrypted under a key derived from the
password, and that ciphertext never leaves the enclave.

Storage, per account (keyed by sha256 of the normalized email):

    salt, nonce, ciphertext, attempts

    key        = Argon2id(password, salt)
    ciphertext = AES-256-GCM(key, nonce, mnemonic, aad=account_id)

A wrong password is a failed decrypt. The mnemonic is never on disk in
plaintext, and a copy of /data is useless without the password.

Every guess is counted before it is checked: the counter is incremented
and fsynced, and only then does the enclave run the KDF. A crash or a
restart in the middle of a request therefore costs the guesser a try
instead of giving one away. The counter can only be trusted if the host
can't roll /data back to an older copy. On Enclavia that needs a
production enclave created with --anti-rollback (see README.md).

There is no time-based backoff: the enclave's clock comes from the host,
so a timer is something the host could skip. A count can't be skipped.

State lives in /data (the enclave's encrypted volume). Without storage
it lives in tmpfs and is lost on restart.
"""

import hashlib
import http.server
import json
import os
import pathlib
import secrets
import socketserver
import sqlite3
import threading

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

PORT = 8080
DATA_DIR = pathlib.Path(os.environ.get("VAULT_DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "vault.db"
WEB_DIR = pathlib.Path(__file__).resolve().parent / "web"

MAX_ATTEMPTS = 10
MAX_BODY = 4096
MAX_EMAIL = 254
MIN_PASSWORD = 8
MAX_PASSWORD = 256
MAX_MNEMONIC = 1024

# Argon2id parameters (RFC 9106, second recommended option). They are
# stored with each record, so they can be raised later without breaking
# existing backups. Memory is in KiB.
KDF_PARAMS = {"memory_kib": 64 * 1024, "iterations": 3, "lanes": 4}

# Each KDF run holds 64 MiB. A small enclave has 2 GiB, so cap how many
# run at once; extra requests wait their turn.
kdf_slots = threading.BoundedSemaphore(2)

db_lock = threading.Lock()


# --- storage -------------------------------------------------------------

def open_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
    # synchronous=FULL makes every commit fsync before it returns. The
    # attempt counter depends on that: a guess must be on disk before
    # the password is checked.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id         TEXT PRIMARY KEY,
            kdf        TEXT NOT NULL,
            salt       BLOB NOT NULL,
            nonce      BLOB NOT NULL,
            ciphertext BLOB NOT NULL,
            attempts   INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    return conn


db = open_db()


def normalize_email(email):
    return email.strip().lower()


def account_id(email):
    # The email itself is never stored. The id is enough to find the
    # record when the user types the same email again.
    return hashlib.sha256(normalize_email(email).encode()).hexdigest()


def derive_key(password, salt, params):
    kdf = Argon2id(
        salt=salt,
        length=32,
        iterations=params["iterations"],
        lanes=params["lanes"],
        memory_cost=params["memory_kib"],
    )
    with kdf_slots:
        return kdf.derive(password.encode())


# --- operations ----------------------------------------------------------

class VaultError(Exception):
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status = status
        self.body = {"error": message, **extra}


def register(email, password, mnemonic):
    aid = account_id(email)
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(12)
    key = derive_key(password, salt, KDF_PARAMS)
    ciphertext = AESGCM(key).encrypt(nonce, mnemonic.encode(), aid.encode())
    with db_lock:
        try:
            db.execute(
                "INSERT INTO accounts(id, kdf, salt, nonce, ciphertext) VALUES (?, ?, ?, ?, ?)",
                (aid, json.dumps(KDF_PARAMS), salt, nonce, ciphertext),
            )
        except sqlite3.IntegrityError:
            # Never overwrite: otherwise anyone who knows the email
            # could replace the backup with their own.
            raise VaultError(409, "an account with this email already exists")
    log("register", aid)


def unlock(email, password):
    """Check the password and return (account_id, mnemonic).

    The guess is counted and committed before the KDF runs. Success
    resets the count. The guess that reaches MAX_ATTEMPTS erases the
    record.
    """
    aid = account_id(email)
    with db_lock:
        row = db.execute(
            "SELECT kdf, salt, nonce, ciphertext, attempts FROM accounts WHERE id = ?",
            (aid,),
        ).fetchone()
        if row is None:
            raise VaultError(404, "no backup for this email")
        kdf, salt, nonce, ciphertext, attempts = row
        attempts += 1
        db.execute("UPDATE accounts SET attempts = ? WHERE id = ?", (attempts, aid))

    key = derive_key(password, salt, json.loads(kdf))
    try:
        mnemonic = AESGCM(key).decrypt(nonce, ciphertext, aid.encode()).decode()
    except InvalidTag:
        if attempts >= MAX_ATTEMPTS:
            with db_lock:
                db.execute("DELETE FROM accounts WHERE id = ?", (aid,))
            log("erased after too many wrong passwords", aid)
            raise VaultError(410, "too many wrong passwords: the backup has been erased")
        log("wrong password", aid)
        raise VaultError(401, "wrong password", attempts_left=MAX_ATTEMPTS - attempts)

    with db_lock:
        db.execute("UPDATE accounts SET attempts = 0 WHERE id = ?", (aid,))
    return aid, mnemonic


def recover(email, password):
    aid, mnemonic = unlock(email, password)
    log("recover", aid)
    return mnemonic


def delete(email, password):
    aid, _ = unlock(email, password)
    with db_lock:
        db.execute("DELETE FROM accounts WHERE id = ?", (aid,))
    log("delete", aid)


def log(event, aid):
    # Only a prefix of the account id: no email, password or mnemonic.
    print(f"{event} account={aid[:12]}", flush=True)


# --- input checks --------------------------------------------------------

def field(body, name, max_len, min_len=1):
    value = body.get(name)
    if not isinstance(value, str):
        raise VaultError(400, f"{name} is required")
    if name != "password":
        value = value.strip()
    if len(value) < min_len:
        raise VaultError(400, f"{name} must be at least {min_len} characters")
    if len(value) > max_len:
        raise VaultError(400, f"{name} must be at most {max_len} characters")
    return value


def email_field(body):
    email = field(body, "email", MAX_EMAIL)
    if "@" not in email:
        raise VaultError(400, "email doesn't look like an email address")
    return email


def password_field(body, min_len=1):
    return field(body, "password", MAX_PASSWORD, min_len)


# --- HTTP ----------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "mnemonic-vault"

    def log_message(self, fmt, *args):
        # The default access log is fine: paths only, never bodies.
        print("%s %s" % (self.address_string(), fmt % args), flush=True)

    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise VaultError(413, "request body too large")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            raise VaultError(400, "body must be JSON")
        if not isinstance(body, dict):
            raise VaultError(400, "body must be a JSON object")
        return body

    def do_GET(self):
        if self.path == "/health":
            self.reply(200, {"ok": True})
        elif self.path in ("/", "/index.html"):
            page = (WEB_DIR / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
        elif self.path == "/info":
            self.reply(200, {"max_attempts": MAX_ATTEMPTS, "min_password": MIN_PASSWORD, "kdf": KDF_PARAMS})
        else:
            self.reply(404, {"error": "not found"})

    def do_POST(self):
        try:
            body = self.read_json()
            if self.path == "/register":
                register(
                    email_field(body),
                    password_field(body, MIN_PASSWORD),
                    field(body, "mnemonic", MAX_MNEMONIC),
                )
                self.reply(201, {"ok": True})
            elif self.path == "/recover":
                mnemonic = recover(email_field(body), password_field(body))
                self.reply(200, {"mnemonic": mnemonic})
            elif self.path == "/delete":
                delete(email_field(body), password_field(body))
                self.reply(200, {"ok": True})
            else:
                self.reply(404, {"error": "not found"})
        except VaultError as e:
            self.reply(e.status, e.body)
        except Exception as e:
            # Type only: the message could quote the request.
            print(f"internal error: {type(e).__name__}", flush=True)
            self.reply(500, {"error": "internal error"})


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    print(f"mnemonic-vault listening on :{PORT}, data in {DB_PATH}", flush=True)
    Server(("0.0.0.0", PORT), Handler).serve_forever()
