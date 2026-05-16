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

This image sets `/etc/resolv.conf` to `nameserver 127.0.0.1`, pointing
libc at the unbound resolver the enclave runtime stands up on loopback.
Without that, hostname lookups go to whatever public resolver the base
image was built with, and the egress daemon denies the resulting traffic
because the resolver was never allow-listed.

When you author your own image, replicate the `RUN echo "nameserver
127.0.0.1" > /etc/resolv.conf` line, or build on top of one that already
does.
