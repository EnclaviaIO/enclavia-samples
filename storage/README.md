# storage

Reads and writes `/data/state.json` to exercise the enclave's persistent
LUKS-encrypted btrfs volume.

Each boot bumps `boot_count`. Each request to `/increment` bumps
`request_count`. Both fields persist across stop/start (once start exists)
and across enclave restarts.

## Build and push

```sh
docker build -t <your-handle>/storage:v1 .
enclavia push <your-handle>/storage:v1
```

## Create the enclave with a persistent volume

```sh
enclavia enclave create --image storage:v1 --storage 1G
```

The `--storage` flag attaches an encrypted btrfs volume to `/data` inside
the workload container. Without it, writes land in tmpfs and reset on
every boot.

## Verify

```sh
ENC=<enclave-id>

# First request: boot_count=1, request_count=0
curl https://$ENC.enclaves.beta.enclavia.io/

# Bump the counter
curl https://$ENC.enclaves.beta.enclavia.io/increment
# {"boot_count":1,"request_count":1, ...}

# Restart the enclave (stop + create again; `start` is a TODO):
enclavia enclave stop $ENC
# ... and re-create from the same backing volume once that flow exists ...

# Or just hit it again: request_count keeps climbing for the lifetime
# of this enclave.
```

## What this proves

- The enclave can mount and write to a persistent volume.
- The volume is preserved across requests, restarts, and (when start lands)
  stop/start cycles.
- Data on disk is LUKS-encrypted: a raw read of the backing file from
  outside the enclave returns ciphertext.
