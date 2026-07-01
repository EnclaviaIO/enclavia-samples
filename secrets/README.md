# secrets

Reads a per-enclave secret from its environment and reports that it is
present, without ever revealing the value. Secrets are set with
`enclavia secret set`, encrypted at rest with the backend's master key,
and injected into the workload's `process.env` at boot. They never
appear in the image, the EIF, the measured PCRs, or the build log, so
you don't rebuild to rotate a credential.

## Create and push

```sh
docker build -t secrets:v1 .
enclavia enclave create --container-port 8080 --name secrets
enclavia push secrets:v1 <enclave-id>
```

Wait for `running`, then `curl` it through the hosted proxy. The secret
isn't set yet:

```sh
ID=<enclave-id>
curl -s https://$ID.enclaves.beta.enclavia.io/proxy/
# {"secret_name":"API_TOKEN","present":false,"masked":null}
```

## Set the secret

```sh
# NAME=value pairs, repeatable. For a value that shouldn't land in your
# shell history, use `--from-stdin` or `--from-file` for a single named
# secret instead.
enclavia secret set $ID API_TOKEN=sk-live-0123456789abcdef

# The value is write-only: the API never hands it back.
enclavia secret list $ID
# NAME       UPDATED                    PENDING
# API_TOKEN  2026-07-01T...             yes
```

`PENDING yes` means the value is stored but not yet live in the running
enclave. Injection happens at boot, so apply it with a restart:

```sh
enclavia enclave restart $ID
```

## Verify it's injected

```sh
curl -s https://$ID.enclaves.beta.enclavia.io/proxy/
# {"secret_name":"API_TOKEN","present":true,"masked":"sk-...ef (40 chars)"}
```

The app only ever returns presence, length, and a masked hint, never the
value, and it prints the same masked line to its logs on startup.

## Rotate or remove

```sh
enclavia secret set    $ID API_TOKEN=sk-live-rotated   # rotate
enclavia secret delete $ID API_TOKEN                   # remove
enclavia enclave restart $ID                           # apply either
```

## What this proves

- A credential reaches the workload's environment at boot without being
  baked into the image (rebuild-free rotation: change the value, restart).
- Secrets are write-only from the API's perspective: `secret list` and
  every response return names and metadata, never the value.
- Because injection is at boot, a `set` / `delete` only takes effect on
  the next `enclave restart`.

> **Prefer that the operator never sees the value?** With `secret set`
> the backend holds the encrypted secret and injects it at boot. For a
> stronger trust boundary, hand the secret to the enclave from your own
> client *after* verifying attestation, so it never passes through
> Enclavia at all: the [`notifier/`](../notifier/) sample shows that
> bootstrap-over-the-channel pattern.
