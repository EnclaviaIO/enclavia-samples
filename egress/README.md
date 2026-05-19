# egress

Makes outbound HTTPS requests from inside the enclave to exercise the
egress allowlist. Logs the result of each fetch to stdout on startup
and also serves them as JSON over its HTTP endpoint, so you can verify
the allowlist behaviour either by reading runtime logs or by hitting
the workload from your own client.

A permitted destination returns a real response; a non-permitted one
fails at the in-enclave resolver (`REFUSED`) or at the egress filter
(connection refused / timeout) before any packet leaves the host.

## Create the enclave (with an allowlist)

```sh
# Permit api.github.com:443 via Cloudflare's resolver.
enclavia enclave create \
    --container-port 8080 \
    --name egress \
    --egress-allow api.github.com:443 \
    --egress-resolver 1.1.1.1
# Enclave created:
#   ID:     1d2c3b4a-...
```

See [docs.enclavia.io/egress](https://docs.enclavia.io/egress) for the
full allowlist grammar — single IPs, CIDRs, multiple destinations, and
the JSON-file form (`--egress-config`) for non-trivial policies.

## Build and push

```sh
docker build -t egress:v1 .
enclavia push egress:v1 1d2c3b4a
```

## Verify

The simplest verification path doesn't require a Noise-speaking client:
the app prints the result of its startup fetch to stdout. Once the
enclave reaches `running`, read its runtime logs from the dashboard
at [beta.enclavia.io](https://beta.enclavia.io) (or via the
MCP `enclave_logs` tool — see the [top-level README](../README.md)).
For the allowlist above you'll see a line like:

```
egress-sample: fetched https://api.github.com/zen — 200 OK
```

To probe arbitrary URLs the app also accepts `?url=<url>` on its
HTTP endpoint. Hitting that from outside requires a Noise-speaking
client today (a small amount of Rust against the
[`enclavia` crate](https://docs.enclavia.io/connect); Python/Java/JS
bindings are tracked at [enclavia#7](https://github.com/EnclaviaIO/enclavia/issues/7)).
A permitted destination returns 200 + a body snippet; a non-permitted
one returns:

```json
{"url":"https://example.com","error":"URLError: ..."}
```

## What this proves

- Outbound TCP from the workload routes through the in-enclave egress
  daemon, over vsock to the host, and out to the internet.
- Permitted destinations succeed; non-permitted destinations fail at
  the daemon or at the in-enclave resolver, before any packet leaves
  the host.
- The allowlist is baked into the EIF at build time and covered by
  the PCRs: changing it changes the image identity, surfaced by
  `enclavia reproduce`.

## DNS and the in-enclave resolver

The enclave runtime exposes a validating `unbound` resolver on
`127.0.0.1:53` inside the workload's network namespace. When the
workload calls `getaddrinfo`, libc reads `/etc/resolv.conf` and finds
`nameserver 127.0.0.1`, the resolver checks the request against the
configured allowlist, and either forwards it to one of the
`--egress-resolver` upstreams or returns `REFUSED`.

The runtime writes `/etc/resolv.conf` into the workload rootfs
automatically when an egress allowlist is configured, so your image
doesn't need to do anything for hostname lookups to work.
