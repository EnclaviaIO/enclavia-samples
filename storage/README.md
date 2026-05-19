# storage

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

To actually hit `/increment` and `/` from outside the enclave you need
a Noise-speaking client (see the [top-level README](../README.md) for
the minimum Rust example, or [docs.enclavia.io/connect](https://docs.enclavia.io/connect)).
Bindings for **Python, Java, and JavaScript (WASM)** are coming
([enclavia#7](https://github.com/EnclaviaIO/enclavia/issues/7)) so you
won't need Rust forever.

## What this proves

- The enclave can mount and write to a persistent volume.
- The volume is preserved across requests, enclave restarts, and
  `stop` / `start` cycles.
- Data on disk is LUKS-encrypted: a raw read of the backing file
  from outside the enclave returns ciphertext.
