"""Sealed-bid auction that runs inside an enclave.

Bids are collected while the round is open and nobody can read them: not
the other bidders, not the operator, not the admin. Closing the round
publishes the full list. The rules are this file, and the enclave's PCRs
pin this file, so a client that verified the attestation knows them.

Every accepted bid gets a receipt and extends a hash chain:

    head_0 = sha256(round header)
    head_n = sha256(head_{n-1} || sha256(receipt_n))

The current head is public while the round is open. After the reveal
anyone can recompute the chain from the published list and check that it
passes through the head they saw when they bid, so the list cannot have
been edited after the fact. Receipts carry a random salt, so the head
leaks nothing about the bids.

State lives in /data (the enclave's encrypted volume). Without
--storage the bids live in tmpfs and are lost on restart.

Admin actions (open a new round, close the current one) need the
ADMIN_TOKEN secret. Even the admin cannot read bids before the close.
"""

import hashlib
import hmac
import http.server
import json
import os
import pathlib
import secrets
import socketserver
import threading
import time
import urllib.parse

PORT = 8080
DATA_DIR = pathlib.Path(os.environ.get("AUCTION_DATA_DIR", "/data"))
STATE_PATH = DATA_DIR / "auction.json"
WEB_DIR = pathlib.Path(__file__).resolve().parent / "web"

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN") or None
DEFAULT_TITLE = os.environ.get("AUCTION_TITLE", "TABConf sealed-bid auction")

MAX_BODY = 4096
MAX_BIDDER = 32
MAX_MESSAGE = 140
MAX_TITLE = 80
MAX_BIDS_PER_ROUND = 5000
MAX_SATS = 21_000_000 * 100_000_000

# An SDK response travels as one Noise message: at most 64 KiB after CBOR
# encoding, which can double the bytes. Larger replies drop the client's
# channel, so the bid list is served in pages under this budget.
PAGE_BUDGET = 16 * 1024

lock = threading.Lock()


def now():
    return int(time.time())


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def canonical(obj):
    # The exact string that gets hashed. It is returned alongside every
    # hash, so a client checks sha256(canonical) instead of re-serializing.
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def chain(head_hex, receipt_hash_hex):
    return sha256_hex(bytes.fromhex(head_hex) + bytes.fromhex(receipt_hash_hex))


def new_round(number, title, closes_at=None):
    header = {
        "round": number,
        "title": title,
        "opened_at": now(),
        "closes_at": closes_at,
        "salt": secrets.token_hex(16),
    }
    header_canonical = canonical(header)
    genesis = sha256_hex(header_canonical.encode())
    return {
        "round": number,
        "title": title,
        "phase": "open",
        "opened_at": header["opened_at"],
        "closes_at": closes_at,
        "closed_at": None,
        "header_canonical": header_canonical,
        "genesis": genesis,
        "head": genesis,
        "bids": [],
    }


def read_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {"current": new_round(1, DEFAULT_TITLE), "archive": []}


def write_state(state):
    # Write-then-rename with fsyncs, as in the storage sample, so an
    # accepted bid survives a stop right after the reply.
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(state).encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, STATE_PATH)
    dir_fd = os.open(DATA_DIR, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


state = read_state()
write_state(state)


def close_if_expired(rnd):
    if rnd["phase"] == "open" and rnd["closes_at"] and now() >= rnd["closes_at"]:
        rnd["phase"] = "closed"
        rnd["closed_at"] = rnd["closes_at"]
        return True
    return False


def ranking(rnd):
    # Highest amount wins; on a tie the earlier bid wins.
    return sorted(rnd["bids"], key=lambda b: (-b["amount_sats"], b["seq"]))


def public_status(rnd):
    return {
        "round": rnd["round"],
        "title": rnd["title"],
        "phase": rnd["phase"],
        "opened_at": rnd["opened_at"],
        "closes_at": rnd["closes_at"],
        "closed_at": rnd["closed_at"],
        "bid_count": len(rnd["bids"]),
        "genesis": rnd["genesis"],
        "head": rnd["head"],
        "server_time": now(),
        "admin_enabled": ADMIN_TOKEN is not None,
    }


def published(bid):
    # The receipt fields are all inside receipt_canonical; clients parse
    # them from there, which keeps each entry small.
    return {k: bid[k] for k in ("receipt_canonical", "receipt_hash", "head_after")}


def results_page(rnd, offset):
    ranked = ranking(rnd)
    winner = ranked[0] if ranked else None
    # The best bid from anyone other than the winner: the price a
    # second-price (Vickrey) auction would charge.
    runner_up = next((b for b in ranked if winner and b["bidder"] != winner["bidder"]), None)
    page = {
        **public_status(rnd),
        "header_canonical": rnd["header_canonical"],
        "winner": published(winner) if winner else None,
        "second_price_sats": runner_up["amount_sats"] if runner_up else None,
        "offset": offset,
        "bids": [],
        "next_offset": None,
    }
    size = len(json.dumps(page, ensure_ascii=False).encode()) + 32
    for i in range(offset, len(rnd["bids"])):
        entry = published(rnd["bids"][i])
        entry_size = len(json.dumps(entry, ensure_ascii=False).encode()) + 2
        if page["bids"] and size + entry_size > PAGE_BUDGET:
            page["next_offset"] = i
            break
        page["bids"].append(entry)
        size += entry_size
    return page


class BadRequest(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def parse_bid(body):
    bidder = body.get("bidder")
    amount = body.get("amount_sats")
    message = body.get("message") or ""
    if not isinstance(bidder, str) or not bidder.strip():
        raise BadRequest(400, "bidder is required")
    bidder = bidder.strip()
    if len(bidder) > MAX_BIDDER:
        raise BadRequest(400, f"bidder is longer than {MAX_BIDDER} characters")
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise BadRequest(400, "amount_sats must be an integer")
    if not 1 <= amount <= MAX_SATS:
        raise BadRequest(400, "amount_sats must be between 1 and 21M BTC")
    if not isinstance(message, str) or len(message) > MAX_MESSAGE:
        raise BadRequest(400, f"message must be a string of at most {MAX_MESSAGE} characters")
    return bidder, amount, message


def place_bid(body):
    bidder, amount, message = parse_bid(body)
    with lock:
        rnd = state["current"]
        if close_if_expired(rnd):
            write_state(state)
        if rnd["phase"] != "open":
            raise BadRequest(409, "the round is closed")
        if len(rnd["bids"]) >= MAX_BIDS_PER_ROUND:
            raise BadRequest(409, "this round has reached its bid limit")
        receipt = {
            "round": rnd["round"],
            "seq": len(rnd["bids"]) + 1,
            "bidder": bidder,
            "amount_sats": amount,
            "message": message,
            "received_at": now(),
            "salt": secrets.token_hex(16),
        }
        receipt_canonical = canonical(receipt)
        receipt_hash = sha256_hex(receipt_canonical.encode())
        head_before = rnd["head"]
        rnd["head"] = chain(head_before, receipt_hash)
        rnd["bids"].append(
            {
                **receipt,
                "receipt_canonical": receipt_canonical,
                "receipt_hash": receipt_hash,
                "head_after": rnd["head"],
            }
        )
        write_state(state)
        return {
            "receipt": receipt,
            "receipt_canonical": receipt_canonical,
            "receipt_hash": receipt_hash,
            "head_before": head_before,
            "head_after": rnd["head"],
        }


def check_admin(headers):
    if ADMIN_TOKEN is None:
        raise BadRequest(503, "admin is disabled: set the ADMIN_TOKEN secret and restart")
    auth = headers.get("Authorization", "")
    given = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
    if not hmac.compare_digest(given.encode(), ADMIN_TOKEN.encode()):
        raise BadRequest(401, "wrong admin token")


def admin_close():
    with lock:
        rnd = state["current"]
        if rnd["phase"] != "open":
            raise BadRequest(409, "the round is already closed")
        rnd["phase"] = "closed"
        rnd["closed_at"] = now()
        write_state(state)
        return public_status(rnd)


def admin_open(body):
    title = body.get("title") or DEFAULT_TITLE
    minutes = body.get("closes_in_minutes")
    if not isinstance(title, str) or len(title) > MAX_TITLE:
        raise BadRequest(400, f"title must be a string of at most {MAX_TITLE} characters")
    closes_at = None
    if minutes is not None:
        if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not 0 < minutes <= 7 * 24 * 60:
            raise BadRequest(400, "closes_in_minutes must be a number between 0 and 10080")
        closes_at = now() + int(minutes * 60)
    with lock:
        old = state["current"]
        if old["phase"] == "open" and not close_if_expired(old):
            raise BadRequest(409, "close the current round first")
        state["archive"].append(old)
        state["current"] = new_round(old["round"] + 1, title, closes_at)
        write_state(state)
        return public_status(state["current"])


def find_round(number):
    if state["current"]["round"] == number:
        return state["current"]
    return next((r for r in state["archive"] if r["round"] == number), None)


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "enclavia-auction/1"

    def send_json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # Lets a page on another origin read the proxy's X-Enclavia-PCR*
        # headers when it calls /proxy/health to prefill the pins.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header(
            "Access-Control-Expose-Headers",
            "X-Enclavia-PCR0, X-Enclavia-PCR1, X-Enclavia-PCR2",
        )
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, content_type):
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise BadRequest(413, "request body too large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise BadRequest(400, "body must be JSON")
        if not isinstance(body, dict):
            raise BadRequest(400, "body must be a JSON object")
        return body

    def handle_errors(self, fn):
        try:
            fn()
        except BadRequest as e:
            self.send_json(e.status, {"error": e.message})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self):
        self.handle_errors(self.get)

    def do_POST(self):
        self.handle_errors(self.post)

    def get(self):
        path, _, query = self.path.partition("?")
        if path == "/health":
            self.send_json(200, {"ok": True})
        elif path in ("/", "/index.html"):
            self.send_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
        elif path == "/status":
            with lock:
                if close_if_expired(state["current"]):
                    write_state(state)
                self.send_json(200, public_status(state["current"]))
        elif path == "/bids" or path.startswith("/rounds/"):
            with lock:
                if close_if_expired(state["current"]):
                    write_state(state)
                if path == "/bids":
                    rnd = state["current"]
                else:
                    try:
                        rnd = find_round(int(path[len("/rounds/"):]))
                    except ValueError:
                        rnd = None
                    if rnd is None:
                        raise BadRequest(404, "no such round")
                if rnd["phase"] == "open":
                    # The whole point: no endpoint returns bids before the close.
                    raise BadRequest(403, "sealed: bids are published when the round closes")
                offset = urllib.parse.parse_qs(query).get("offset", ["0"])[0]
                if not offset.isdigit():
                    raise BadRequest(400, "offset must be a non-negative integer")
                self.send_json(200, results_page(rnd, int(offset)))
        else:
            raise BadRequest(404, "not found")

    def post(self):
        path = self.path.partition("?")[0]
        if path == "/bid":
            reply = place_bid(self.read_body())
            # Log that a bid arrived, never what it was.
            print(f"bid accepted: round {reply['receipt']['round']} seq {reply['receipt']['seq']}", flush=True)
            self.send_json(200, reply)
        elif path == "/admin/close":
            check_admin(self.headers)
            reply = admin_close()
            print(f"round {reply['round']} closed with {reply['bid_count']} bids", flush=True)
            self.send_json(200, reply)
        elif path == "/admin/open":
            check_admin(self.headers)
            reply = admin_open(self.read_body())
            print(f"round {reply['round']} opened", flush=True)
            self.send_json(200, reply)
        else:
            raise BadRequest(404, "not found")

    def log_message(self, fmt, *args):
        # The default access log would print paths only, but keep the
        # console quiet: the handlers above log the events that matter.
        pass


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    rnd = state["current"]
    print(
        f"auction listening on :{PORT}; round {rnd['round']} ({rnd['phase']}, "
        f"{len(rnd['bids'])} bids); admin {'enabled' if ADMIN_TOKEN else 'disabled'}",
        flush=True,
    )
    Server(("0.0.0.0", PORT), Handler).serve_forever()
