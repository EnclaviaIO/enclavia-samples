# localhost-proxy

A small Rust binary that runs on your laptop and bridges vanilla HTTP +
WebSocket traffic on `127.0.0.1:NNNN` to an enclavia enclave over the
Noise channel. Drop it in front of a browser wallet, a CLI, or any tool
that expects plain HTTP/WS and doesn't know how to do attestation
itself.

Use case: a Cashu mint running inside an enclave at
`wss://<id>.enclaves.beta.enclavia.io` is reachable from an unmodified
browser wallet by pointing it at `http://localhost:8000`.

## What it forwards

- All HTTP/1.1 methods supported by the SDK (`GET`, `POST`, `PUT`,
  `DELETE`, `PATCH`, `HEAD`, `OPTIONS`).
- Every request header except `Host` and `Content-Length`, which the
  SDK manages itself.
- WebSocket upgrades (`Upgrade: websocket`). The proxy does the upgrade
  end-to-end through the enclave, recomputes `Sec-WebSocket-Accept` from
  the browser's `Sec-WebSocket-Key`, then splices bytes both ways.

## Build

```sh
nix develop  # from the enclavia checkout, gives you a stable Rust + cargo
cargo build --release -p enclavia-localhost-proxy
```

The compiled binary lands at
`target/release/enclavia-localhost-proxy`.

## Run

```sh
enclavia-localhost-proxy \
  --listen 127.0.0.1:8000 \
  --enclave-url wss://<enclave-id>.enclaves.beta.enclavia.io \
  --pcr0 <hex> --pcr1 <hex> --pcr2 <hex>
```

For a debug-mode enclave (no real attestation):

```sh
enclavia-localhost-proxy \
  --listen 127.0.0.1:8000 \
  --enclave-url wss://<enclave-id>.enclaves.beta.enclavia.io \
  --debug-mode
```

PCR values come from `enclavia enclave status <id>`.

## Smoke test

Assuming an enclave that serves `/health`:

```sh
# Plain HTTP roundtrip
curl http://localhost:8000/health

# WS roundtrip (needs the workload to expose a WS endpoint)
websocat ws://localhost:8000/v1/ws
# or:
wscat -c ws://localhost:8000/v1/ws
```

## Known limitations

- The proxy synthesizes the 101 response head locally from the browser's
  `Sec-WebSocket-Key`. It does not forward back any
  `Sec-WebSocket-Protocol` or extension negotiation the workload chose.
  For applications that use a single subprotocol or no negotiation (NUT-17
  WS, most chat-style WS) this is fine.
- Request bodies are read into memory before being sent through the SDK
  (the SDK accepts `Vec<u8>` bodies). Streaming uploads aren't supported.
- The proxy speaks HTTP/1.1 only. HTTP/2 from the browser is out of
  scope.

## Why a binary, not a browser extension or library?

A localhost binary is the smallest piece a wallet integrator has to
trust today: it speaks the wallet's existing protocol (HTTP+WS) over
loopback, and the only thing inside it is the enclavia Noise channel.
Once we ship WASM bindings for the SDK (tracked in
[enclavia#7](https://github.com/EnclaviaIO/enclavia/issues/7)) the
wallet can do this directly and the proxy becomes unnecessary.
