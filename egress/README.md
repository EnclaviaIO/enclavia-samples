# egress

Makes an outbound HTTPS request from inside the enclave, returning the
response status and a snippet of the body as JSON. Useful for sanity-
checking the egress allowlist: a permitted destination returns a real
response; a non-permitted one returns "Connection refused" or a timeout.

## Build and push

```sh
docker build -t <your-handle>/egress:v1 .
enclavia push <your-handle>/egress:v1
```

## Create the enclave (with an allowlist)

```sh
# Permit api.github.com:443 via Cloudflare's resolver
enclavia enclave create \
    --image egress:v1 \
    --egress-allow api.github.com:443 \
    --egress-resolver 1.1.1.1
```

## Verify

```sh
ENC=<enclave-id>

# Default URL (api.github.com): permitted, returns 200 + a GitHub zen quote.
curl https://$ENC.enclaves.beta.enclavia.io/

# Same target, supplied via query: also permitted.
curl 'https://$ENC.enclaves.beta.enclavia.io/?url=https://api.github.com/zen'

# A non-allow-listed target: connection refused (or timeout).
curl 'https://$ENC.enclaves.beta.enclavia.io/?url=https://example.com'
# {"url":"https://example.com","error":"URLError: ..."}
```

## What this proves

- Outbound TCP from the workload routes through the in-enclave egress
  daemon, over vsock to the host, and out to the internet.
- Permitted destinations succeed; non-permitted destinations fail at the
  daemon, before any packet leaves the host.
- The allowlist is baked into the EIF at build time and covered by the
  PCRs: changing it changes the image identity, surfaced by
  `enclavia reproduce`.

## DNS and the in-enclave resolver

The enclave runtime exposes a validating unbound resolver on `127.0.0.1:53`
inside the workload's network namespace. When the workload calls
`getaddrinfo`, libc reads `/etc/resolv.conf` and finds `nameserver 127.0.0.1`,
the resolver checks the request against the configured allowlist, and either
forwards it to one of the `--egress-resolver` upstreams or returns REFUSED.

The runtime writes `/etc/resolv.conf` into the workload rootfs automatically
when an egress allowlist is configured, so your image doesn't need to do
anything for hostname lookups to work. (Earlier versions of this sample
contained a `RUN echo "nameserver 127.0.0.1" > /etc/resolv.conf` line to
work around this gap; that's no longer necessary.)
