"""notifier: Bitcoin descriptor → Expo push bridge.

Wires together three Enclavia features:

  - **Persistent encrypted storage** (`/data/pairings.db`). The Expo
    access token and every (descriptor → push-token) pairing live
    here. The disk is LUKS-encrypted at rest by Enclavia.
  - **Outbound egress to a hostname allowlist**. The workload connects
    to `wss://mempool.space/api/v1/ws` for transaction events and to
    `https://exp.host` for the push API. Both are declared at
    `enclave create` time and enforced by the in-enclave egress
    daemon.
  - **A small HTTP control plane** on `:8080` for bootstrapping the
    Expo secret on first start and registering pairings afterwards.

## HTTP surface

  POST /bootstrap     {"expo_access_token": "..."}
                      One-shot: 409 if a token is already stored.

  POST /register      {"descriptor": "wpkh(xpub.../0/*)",
                       "expo_push_token": "ExponentPushToken[...]",
                       "gap_limit": 20}
                      Derives `gap_limit` addresses from the
                      descriptor's wildcard range, stores them
                      against the pair, and re-subscribes the
                      mempool.space WS to the new address set.

  GET  /pairs         Lists current pairings (push tokens redacted)
                      for visibility.

  GET  /health        Startup probe; returns 200 ok.

## Notification flow

A long-running asyncio task holds the mempool.space WS connection and
subscribes to `multi-address-transactions` for every tracked address.
When mempool.space emits a tx (mempool or confirmed) that touches one
of the addresses, the matching pair's push token gets a notification
via Expo. Direction (received vs sent) and amount in sats are inferred
from the tx's vin/vout against the matched address.

## Restart behaviour

Everything in the SQLite DB survives `enclave stop` → `enclave start`
because `/data` is the persistent LUKS volume. On every boot:

  1. open the DB, log "bootstrapped" iff a token is present.
  2. reconnect the WS, re-subscribe with the current address set.
  3. resume serving the HTTP endpoints.
"""

import asyncio
import json
import logging
import os
import sqlite3
import time

import bdkpython as bdk
import websockets
from aiohttp import ClientSession, web

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("notifier")

DB_PATH = os.environ.get("DB_PATH", "/data/pairings.db")
WS_URL = os.environ.get("MEMPOOL_WS_URL", "wss://mempool.space/api/v1/ws")
EXPO_PUSH_URL = os.environ.get("EXPO_PUSH_URL", "https://exp.host/--/api/v2/push/send")
DEFAULT_GAP_LIMIT = int(os.environ.get("DEFAULT_GAP_LIMIT", "20"))
DEFAULT_NETWORK = os.environ.get("DEFAULT_NETWORK", "bitcoin")
RECONNECT_DELAY_SECS = 5

# Map the JSON-friendly network name (what /register accepts) to the BDK enum.
NETWORKS: dict[str, bdk.Network] = {
    "bitcoin": bdk.Network.BITCOIN,
    "testnet": bdk.Network.TESTNET,
    "testnet4": bdk.Network.TESTNET4,
    "signet": bdk.Network.SIGNET,
    "regtest": bdk.Network.REGTEST,
}

# Globals wired up in main().
db: sqlite3.Connection
db_lock = asyncio.Lock()
# Set by /register; the WS task awaits this to learn it should send a
# fresh `track-addresses` subscription with the new address list.
ws_resubscribe = asyncio.Event()


# --- DB ------------------------------------------------------------------

def init_db() -> sqlite3.Connection:
    """Open the SQLite DB at DB_PATH, applying schema migrations.

    The database lives on the LUKS-encrypted `/data` volume in
    production, so it survives stop/start. WAL mode and
    `isolation_level=None` (autocommit) keep concurrent reads from the
    HTTP handlers and the WS task simple — every statement is its own
    transaction.
    """
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS config (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pairings (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            descriptor      TEXT NOT NULL,
            expo_push_token TEXT NOT NULL,
            created_at      INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS addresses (
            address    TEXT PRIMARY KEY,
            pairing_id INTEGER NOT NULL REFERENCES pairings(id) ON DELETE CASCADE
        );
        """
    )
    return conn


def get_expo_access_token() -> str | None:
    row = db.execute(
        "SELECT value FROM config WHERE key = 'expo_access_token'"
    ).fetchone()
    return row[0] if row else None


def all_tracked_addresses() -> list[str]:
    return [r[0] for r in db.execute("SELECT address FROM addresses").fetchall()]


# --- Descriptor derivation -----------------------------------------------

def derive_addresses(
    descriptor_str: str, gap_limit: int, network: bdk.Network
) -> list[str]:
    """Derive the first `gap_limit` addresses from a ranged descriptor.

    Uses BDK's single-keychain wallet so a wildcard descriptor like
    `wpkh(xpub.../0/*)` derives normally. For wallets that separate
    receive and change branches into two descriptors, register each
    branch as its own pair — this sample only walks one wildcard
    keychain at a time.

    BDK requires a persister even though we do not care about
    persistence: an in-memory persister gives us a stateless,
    side-effect-free address derivation that throws away its state
    after the call returns.
    """
    desc = bdk.Descriptor(descriptor_str, network)
    persister = bdk.Persister.new_in_memory()
    wallet = bdk.Wallet.create_single(desc, network, persister)
    return [
        str(wallet.peek_address(bdk.KeychainKind.EXTERNAL, i).address)
        for i in range(gap_limit)
    ]


# --- HTTP handlers -------------------------------------------------------

def _err(message: str, status: int) -> web.Response:
    return web.json_response({"error": message}, status=status)


async def handle_bootstrap(request: web.Request) -> web.Response:
    """One-shot: store the Expo access token. 409 if already set."""
    if get_expo_access_token():
        return _err("already bootstrapped", 409)
    try:
        body = await request.json()
    except Exception:
        return _err("invalid json body", 400)
    token = (body.get("expo_access_token") or "").strip()
    if not token:
        return _err("missing expo_access_token", 400)
    async with db_lock:
        db.execute(
            "INSERT INTO config(key, value) VALUES('expo_access_token', ?)",
            (token,),
        )
    log.info("bootstrap: stored expo_access_token (len=%d)", len(token))
    return web.json_response({"status": "bootstrapped"})


async def handle_register(request: web.Request) -> web.Response:
    """Register a (descriptor, push_token) pair, derive addresses, re-subscribe."""
    if not get_expo_access_token():
        return _err("not bootstrapped; POST /bootstrap first", 412)
    try:
        body = await request.json()
    except Exception:
        return _err("invalid json body", 400)
    descriptor = (body.get("descriptor") or "").strip()
    push_token = (body.get("expo_push_token") or "").strip()
    gap_limit = int(body.get("gap_limit", DEFAULT_GAP_LIMIT))
    network_name = (body.get("network") or DEFAULT_NETWORK).lower()
    if not descriptor or not push_token:
        return _err("descriptor and expo_push_token are required", 400)
    if gap_limit < 1 or gap_limit > 1000:
        return _err("gap_limit must be in 1..1000", 400)
    network = NETWORKS.get(network_name)
    if network is None:
        return _err(
            f"unknown network {network_name!r} (expected one of {sorted(NETWORKS)})",
            400,
        )
    try:
        addrs = derive_addresses(descriptor, gap_limit, network)
    except Exception as e:
        return _err(f"invalid descriptor: {e}", 400)

    async with db_lock:
        cur = db.execute(
            "INSERT INTO pairings(descriptor, expo_push_token, created_at) "
            "VALUES(?, ?, ?)",
            (descriptor, push_token, int(time.time())),
        )
        pairing_id = cur.lastrowid
        for addr in addrs:
            db.execute(
                "INSERT OR IGNORE INTO addresses(address, pairing_id) VALUES(?, ?)",
                (addr, pairing_id),
            )

    log.info(
        "register: pairing #%d → %d addresses (push=%s…)",
        pairing_id,
        len(addrs),
        push_token[:12],
    )
    ws_resubscribe.set()
    return web.json_response({"id": pairing_id, "addresses": addrs})


async def handle_list_pairs(request: web.Request) -> web.Response:
    rows = db.execute(
        """
        SELECT p.id, p.descriptor, p.created_at, COUNT(a.address)
        FROM pairings p LEFT JOIN addresses a ON a.pairing_id = p.id
        GROUP BY p.id
        ORDER BY p.id
        """
    ).fetchall()
    return web.json_response(
        [
            {
                "id": r[0],
                "descriptor": r[1],
                "created_at": r[2],
                "address_count": r[3],
            }
            for r in rows
        ]
    )


async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


# --- Expo push -----------------------------------------------------------

async def send_expo_push(
    session: ClientSession,
    push_token: str,
    title: str,
    body: str,
) -> None:
    """POST one push notification to Expo. Failures are logged, not raised."""
    access_token = get_expo_access_token()
    if not access_token:
        log.warning("send_expo_push: no expo_access_token in db, skipping")
        return
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    payload = {
        "to": push_token,
        "title": title,
        "body": body,
        "sound": "default",
    }
    try:
        async with session.post(
            EXPO_PUSH_URL, json=payload, headers=headers, timeout=10
        ) as resp:
            data = await resp.json()
            log.info(
                "expo push: status=%d to=%s… resp=%s",
                resp.status,
                push_token[:12],
                data,
            )
    except Exception as e:
        log.warning("expo push failed: %s", e)


# --- mempool.space WS ----------------------------------------------------

def _amount_in_out(tx: dict, address: str) -> tuple[int, int]:
    """Sum sats received-to and sent-from `address` in this tx.

    `vout` carries the receiver's scriptpubkey_address directly.
    `vin[].prevout.scriptpubkey_address` is populated for confirmed
    transactions; for unconfirmed mempool entries the prevout may be
    incomplete, in which case we return 0 for the sent side and let
    the next confirmation event emit the full picture.
    """
    received = sum(
        o.get("value", 0)
        for o in tx.get("vout", [])
        if o.get("scriptpubkey_address") == address
    )
    sent = sum(
        (i.get("prevout") or {}).get("value", 0)
        for i in tx.get("vin", [])
        if (i.get("prevout") or {}).get("scriptpubkey_address") == address
    )
    return received, sent


async def notify_for_tx(
    session: ClientSession, address: str, tx: dict, kind: str
) -> None:
    """Map the tx to the pairing that owns `address`, send a push."""
    row = db.execute(
        """
        SELECT p.expo_push_token, p.id
        FROM addresses a
        JOIN pairings p ON p.id = a.pairing_id
        WHERE a.address = ?
        """,
        (address,),
    ).fetchone()
    if not row:
        log.warning("match: no pairing owns address %s", address)
        return
    push_token, pairing_id = row
    received, sent = _amount_in_out(tx, address)
    if received > 0 and sent == 0:
        direction, amount = "received", received
    elif sent > 0:
        direction, amount = "sent", sent
    else:
        direction, amount = "touched by", 0
    txid = tx.get("txid", "?")
    title = f"Bitcoin tx ({kind})"
    body = (
        f"{address[:8]}… {direction} {amount} sat · tx {txid[:8]}…"
        if amount
        else f"{address[:8]}… {direction} tx {txid[:8]}…"
    )
    log.info(
        "match pairing #%d (addr=%s…) %s — sending push",
        pairing_id,
        address[:12],
        body,
    )
    await send_expo_push(session, push_token, title, body)


async def handle_ws_event(session: ClientSession, event: dict) -> None:
    """One inbound WS message from mempool.space. Logs the data we want
    to surface (blocks, fee estimates) and walks any
    `multi-address-transactions` block to fire notifications."""
    if "block" in event:
        b = event["block"]
        log.info(
            "block: height=%d tx_count=%d",
            b.get("height", 0),
            b.get("tx_count", 0),
        )
    if "mempool-blocks" in event:
        mbs = event["mempool-blocks"]
        if mbs:
            top = mbs[:3]
            ranges = [mb.get("feeRange", []) for mb in top]
            log.info(
                "mempool blocks: top-3 fee ranges (sat/vB): %s",
                [[round(x, 1) for x in r] for r in ranges],
            )
    mat = event.get("multi-address-transactions") or {}
    for address, buckets in mat.items():
        for kind in ("confirmed", "mempool", "removed"):
            for tx in buckets.get(kind, []) or []:
                if kind == "removed":
                    log.info(
                        "addr %s…: tx removed from mempool: %s",
                        address[:12],
                        tx.get("txid", "?")[:12],
                    )
                    continue
                await notify_for_tx(session, address, tx, kind)


async def ws_loop() -> None:
    """Hold a single WS connection to mempool.space; reconnect on drop.

    On (re)connect we subscribe to `blocks` and `mempool-blocks` for
    fee-estimate logging, then push the current address set as a
    `track-addresses` request. `handle_register` sets
    `ws_resubscribe` after every new pairing so this loop can refresh
    the address subscription without bouncing the connection.
    """
    async with ClientSession() as session:
        while True:
            try:
                log.info("ws: connecting to %s", WS_URL)
                async with websockets.connect(
                    WS_URL, user_agent_header="enclavia-notifier/1"
                ) as ws:
                    log.info("ws: connected")
                    await ws.send(
                        json.dumps(
                            {"action": "want", "data": ["blocks", "mempool-blocks"]}
                        )
                    )
                    addrs = all_tracked_addresses()
                    if addrs:
                        await ws.send(json.dumps({"track-addresses": addrs}))
                        log.info("ws: tracking %d addresses", len(addrs))
                    else:
                        log.info("ws: no addresses tracked yet (no pairings)")

                    async def watch_resubscribe() -> None:
                        while True:
                            await ws_resubscribe.wait()
                            ws_resubscribe.clear()
                            current = all_tracked_addresses()
                            await ws.send(json.dumps({"track-addresses": current}))
                            log.info(
                                "ws: re-tracking %d addresses (after registration)",
                                len(current),
                            )

                    resub_task = asyncio.create_task(watch_resubscribe())
                    try:
                        async for msg in ws:
                            try:
                                event = json.loads(msg)
                            except json.JSONDecodeError:
                                log.warning("ws: malformed json (len=%d)", len(msg))
                                continue
                            await handle_ws_event(session, event)
                    finally:
                        resub_task.cancel()
            except Exception as e:
                log.warning(
                    "ws: connection error (%s); reconnecting in %ds",
                    e,
                    RECONNECT_DELAY_SECS,
                )
                await asyncio.sleep(RECONNECT_DELAY_SECS)


# --- main ----------------------------------------------------------------

async def main() -> None:
    global db
    db = init_db()
    log.info(
        "starting notifier on :8080 (db=%s) bootstrap=%s",
        DB_PATH,
        "yes" if get_expo_access_token() else "NO — POST /bootstrap first",
    )

    app = web.Application()
    app.router.add_post("/bootstrap", handle_bootstrap)
    app.router.add_post("/register", handle_register)
    app.router.add_get("/pairs", handle_list_pairs)
    app.router.add_get("/health", handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()

    log.info("ready")
    await ws_loop()


if __name__ == "__main__":
    asyncio.run(main())
