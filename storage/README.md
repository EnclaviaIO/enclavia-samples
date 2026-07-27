# storage

[![Deploy on Enclavia](https://beta.enclavia.io/deploy-button.svg)](https://beta.enclavia.io/deploy?manifest=https://raw.githubusercontent.com/EnclaviaIO/enclavia-samples/main/storage/enclavia.json)

One-click deploy attaches the encrypted volume from [`enclavia.json`](enclavia.json), no CLI needed.

Reads and writes `/data/state.json` to exercise the enclave's
persistent LUKS-encrypted btrfs volume.

Each boot bumps `boot_count`. Each request to `/increment` bumps
`request_count`. Both fields persist across `enclave stop` +
`enclave start` and across enclave restarts.

## Create the enclave (with a persistent volume)

```sh
# 256 MiB encrypted btrfs volume at /data (must be >= 128 MiB).
enclavia enclave create \
    --container-port 8080 \
    --name storage \
    --storage-size-bytes 268435456
# Enclave created:
#   ID:     1d2c3b4a-...
```

`--storage-size-bytes` attaches an encrypted btrfs volume to `/data`
inside the workload container. Without it, writes land in tmpfs and
reset on every boot, so the `boot_count` reads as 1 every time.

## Build and push

```sh
docker build -t storage:v1 .
enclavia push storage:v1 1d2c3b4a
```

## Verify

```sh
enclavia enclave status 1d2c3b4a-5e6f-7a8b-9c0d-1e2f3a4b5c6d
# Status: running
```

The app prints the current state on every request, so the easiest way
to confirm persistence is to:

1. Wait for the enclave to be `running`.
2. Read the runtime logs (from the dashboard at [beta.enclavia.io](https://beta.enclavia.io)
   or via the MCP `enclave_logs` tool — see the top-level README's
   *Observing the workload* section). You should see a line like
   `boot_count=1` from app startup.
3. `enclavia enclave stop <id>`, then `enclavia enclave start <id>`.
   The new boot prints `boot_count=2`.

To hit `/increment` and `/` from outside the enclave, `curl` them
through the hosted proxy (the `/proxy/` prefix is stripped before the
request reaches the workload):

```sh
ID=<enclave-id>
curl -s https://$ID.enclaves.beta.enclavia.io/proxy/increment
curl -s https://$ID.enclaves.beta.enclavia.io/proxy/
# {"boot_count":2,"request_count":1}
```

For a trustless connection that verifies attestation in your own
process, use a Noise-speaking client instead (see the
[top-level README](../README.md) for the minimum Rust example, or
[docs.enclavia.io/connect](https://docs.enclavia.io/connect); Python,
Java, and JavaScript (WASM) bindings are tracked at
[enclavia#7](https://github.com/EnclaviaIO/enclavia/issues/7)).

## What this proves

- The enclave can mount and write to a persistent volume.
- The volume is preserved across requests, enclave restarts, and
  `stop` / `start` cycles.
- Data on disk is LUKS-encrypted: a raw read of the backing file
  from outside the enclave returns ciphertext.
