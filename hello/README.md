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

`curl` it through the hosted HTTPS proxy (the `/proxy/` prefix is
stripped, so the app's `GET /` is at `/proxy/`):

```sh
ID=1d2c3b4a-5e6f-7a8b-9c0d-1e2f3a4b5c6d   # from `enclave status`

curl -i https://$ID.enclaves.beta.enclavia.io/proxy/
# HTTP/2 200
# x-enclavia-pcr0: <hex>
# x-enclavia-pcr1: <hex>
# x-enclavia-pcr2: <hex>
# ...
# {"message":"hello from inside the enclave", ...}
```

The `x-enclavia-pcr*` response headers are the PCRs the proxy verified
during the attestation handshake; they match `enclave status`. The
hosted proxy runs that attestation check on your behalf (see the
[trust model](https://docs.enclavia.io/proxy#trust-model)); for a
trustless connection that attests in your own process, use the
[client SDK](https://docs.enclavia.io/connect) (the Rust snippet in
the top-level README is the canonical example).

Two more ways to see the greeting without any HTTP client:

1. **Dashboard.** Open the enclave in [beta.enclavia.io](https://beta.enclavia.io);
   the runtime-logs view shows the per-request line the app prints.
2. **MCP.** With the [MCP connector](https://docs.enclavia.io/mcp)
   wired up, ask your agent to `enclave_logs <id>`.
