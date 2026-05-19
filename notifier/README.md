# notifier

A small service that watches one or more Bitcoin descriptors and
sends a push notification to a matching Expo client whenever a
transaction in or out of any derived address is seen on
`mempool.space`. Wires together three things you'd want in a real
enclave deployment:

- **Persistent encrypted storage** at `/data` for the Expo access
  token and the (descriptor → push-token) pairings.
- **Outbound egress to a hostname allowlist** so the workload can
  reach `mempool.space` (WS for tx events) and `exp.host` (the Expo
  push API), and nothing else.
- **First-start bootstrap**: the Expo secret is *not* baked into the
  image — it's POSTed once to `/bootstrap` after the enclave is
  running and persisted in the DB.

## HTTP surface

| Method | Path         | Purpose |
|--------|--------------|---------|
| POST   | `/bootstrap` | One-shot. `{"expo_access_token":"..."}`. 409 if already set. |
| POST   | `/register`  | `{"descriptor":"wpkh(xpub.../0/*)","expo_push_token":"ExponentPushToken[...]","gap_limit":20,"network":"bitcoin"}`. Derives `gap_limit` addresses, stores them, re-subscribes mempool.space WS. `network` is optional (`bitcoin`/`testnet`/`testnet4`/`signet`/`regtest`; default `bitcoin`). |
| GET    | `/pairs`     | Lists current pairings (push tokens are not returned). |
| GET    | `/health`    | Readiness probe. |

## Create the enclave

The workload needs persistent storage *and* an egress allowlist for
both `mempool.space` (WebSocket + REST) and `exp.host` (Expo push):

```sh
enclavia enclave create \
    --container-port 8080 \
    --name notifier \
    --storage-size-bytes 268435456 \
    --egress-allow mempool.space:443 \
    --egress-allow exp.host:443 \
    --egress-resolver 1.1.1.1
```

256 MiB is plenty for the SQLite DB (typical pairing ≈ 1 KiB +
20 × 50 B of derived addresses).

## Build and push

```sh
docker build -t notifier:v1 .
enclavia push notifier:v1 <enclave-id>
```

## Talking to the enclave

The enclave's HTTP endpoints are only reachable through the
Noise-encrypted WebSocket that the runtime exposes — plain `curl`
against the enclave URL won't reach the in-enclave server. The
[`enclavia`](https://github.com/EnclaviaIO/enclavia) Rust client
handles the handshake, attestation check and request encryption.
Python / Java / JS bindings are tracked at
[enclavia#7](https://github.com/EnclaviaIO/enclavia/issues/7).

A minimal client (Cargo dep: `enclavia = { git = "https://github.com/EnclaviaIO/enclavia" }`):

```rust
use enclavia::{Client, Pcrs};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let client = Client::builder("wss://<enclave-id>.enclaves.beta.enclavia.io")
        .pcrs(Pcrs {
            pcr0: hex::decode("…")?,  // from `enclavia enclave status`
            pcr1: hex::decode("…")?,
            pcr2: hex::decode("…")?,
        })
        .build()
        .await?;
    // ... see Bootstrap / Register below
    Ok(())
}
```

## Bootstrap (one-time, after the enclave reaches `running`)

You need an [Expo access token](https://docs.expo.dev/push-notifications/sending-notifications/#access-tokens)
from your Expo account. The service rejects every other endpoint with
HTTP 412 until this is done.

```rust
let resp = client
    .post("/bootstrap")
    .json(&serde_json::json!({ "expo_access_token": "YOUR_EXPO_ACCESS_TOKEN" }))?
    .send()
    .await?;
assert_eq!(resp.status(), 200);
```

(Calling `/bootstrap` a second time returns 409. Destroy the enclave
and re-create it if you need to rotate the secret. The persistent
volume is recycled on `enclave destroy`.)

## Register a pairing

```rust
let resp = client
    .post("/register")
    .json(&serde_json::json!({
        "descriptor": "wpkh([abcdef00/84h/0h/0h]xpub6.../0/*)",
        "expo_push_token": "ExponentPushToken[xxxxxxxxxxxxxxxxxxxxxx]",
        "gap_limit": 20,
    }))?
    .send()
    .await?;
// {"id": 1, "addresses": ["bc1q...", "bc1q...", ...]}
```

What the service does:

1. Parses the descriptor with the [Bitcoin Dev Kit](https://github.com/bitcoindevkit/bdk)
   (`bdkpython`), creating a single-keychain wallet in memory.
2. Derives `gap_limit` addresses from the wildcard range (`0/*`) with
   `Wallet.peek_address(External, i)`.
3. Stores `(descriptor, push_token)` and the derived addresses in
   `/data/pairings.db`.
4. Sends `{"track-addresses":[...]}` to the mempool.space WS so the
   new addresses join the watched set without bouncing the
   connection.

For wallets that separate receive (`0/*`) and change (`1/*`)
branches, register each branch as its own pair — the sample only
walks one wildcard at a time.

## What you'll see

The runtime log (dashboard or
`enclave_logs` over [MCP](https://docs.enclavia.io/mcp)) prints:

```
notifier: starting on :8080 (db=/data/pairings.db) bootstrap=yes
notifier: ws: connecting to wss://mempool.space/api/v1/ws
notifier: ws: connected
notifier: ws: tracking 20 addresses
notifier: block: height=950064 tx_count=3812
notifier: mempool blocks: top-3 fee ranges (sat/vB): [[3.1, 18.4], [2.0, 3.0], [1.7, 2.0]]
notifier: match pairing #1 (addr=bc1qxy0zlrz…) bc1qxy0… received 142000 sat · tx 5f3a9b2c… — sending push
notifier: expo push: status=200 to=ExponentPushT… resp={'data': {'status': 'ok', 'id': '...'}}
```

The push arrives on the device whose `expo_push_token` is registered
against that descriptor.

## What this proves

- **The egress allowlist is doing real work.** Outbound to
  `mempool.space:443` (TLS over a WebSocket) and `exp.host:443`
  (TLS POST) is permitted by name; anything else is denied at the
  in-enclave egress filter.
- **The persistent volume survives `stop`/`start`.** Bootstrap and
  registered pairings come back without re-POSTing anything; the
  service just reconnects the WS and re-subscribes.
- **Secrets don't have to live in the image.** The Expo access
  token enters the enclave once over the encrypted channel and never
  appears in the EIF / PCRs / build log.

## Limitations / room to grow

- **Descriptor support is intentionally minimal** — one ranged
  wildcard branch per pair, derived eagerly to `gap_limit` addresses.
  Multipath (`<0;1>/*`) and unbounded gap recovery aren't handled.
- **No retry on Expo failures.** A 5xx from the Expo API drops the
  notification on the floor; a real deployment would queue and retry.
- **mempool.space is the only data source.** Running your own
  electrs/esplora and pointing this at it is the obvious upgrade for
  a privacy-sensitive deployment.
