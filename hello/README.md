# hello

Smallest possible workload: a Python HTTP server that responds with a
JSON greeting on every request and serves `/health` for the enclave
runtime's startup probe.

Use this image to confirm the enclave end-to-end: build, push, create,
get to `running`, see your "hello" come back.

## Create the enclave

```sh
# Reserves an enclave id and a private repo for it.
enclavia enclave create --container-port 8080 --name hello
# Enclave created:
#   ID:     1d2c3b4a-5e6f-7a8b-9c0d-1e2f3a4b5c6d
#   Status: waiting_for_image
```

## Build and push

```sh
docker build -t hello:v1 .

# Push to the enclave you just created. The second arg is the enclave
# id (a unique prefix is fine); the CLI rewrites the image tag to
# registry.beta.enclavia.io/<your-handle>/<enclave-uuid>:latest.
enclavia push hello:v1 1d2c3b4a
```

The push flips the enclave from `waiting_for_image` to `building`.

## Verify it reached `running`

```sh
enclavia enclave status 1d2c3b4a-5e6f-7a8b-9c0d-1e2f3a4b5c6d
# Status: running
# PCRs:   PCR0/PCR1/PCR2 ...
```

A `running` status means the image was built into an EIF, booted, and
its `/health` probe is replying `200 ok`. That's enough to prove the
end-to-end path works.

## See the response body

There is **no plain-HTTPS endpoint to `curl`** — the enclave is
reachable on `wss://<enclave-id>.enclaves.beta.enclavia.io` over an
end-to-end-encrypted Noise channel. Three ways to actually see the
`hello from inside the enclave` JSON the app returns:

1. **Dashboard.** Open the enclave in [beta.enclavia.io](https://beta.enclavia.io)
   and the runtime-logs view shows the per-request log line the app
   prints (this app logs every request to stdout).
2. **MCP.** With the [MCP connector](https://docs.enclavia.io/mcp)
   wired up, ask your agent to `enclave_logs <id>`.
3. **Your own code.** A small Rust client using the [`enclavia` crate](https://docs.enclavia.io/connect)
   does the Noise handshake, attestation verification, and an HTTP
   GET in a few lines. Bindings for **Python, Java, and JavaScript
   (WASM)** are on the way — see [enclavia#7](https://github.com/EnclaviaIO/enclavia/issues/7).

The Rust snippet from the top-level README is the canonical example.
