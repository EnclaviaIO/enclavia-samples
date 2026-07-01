# upgrades

Ships a new image version to an already-running enclave through the
attested upgrade flow: create it `--upgradable`, stage a v2 push, confirm
the cutover, and read the append-only, attested upgrade chain. The app
just reports its `APP_VERSION` so you can watch the swap.

## Create an upgradable enclave and push v1

```sh
docker build --build-arg APP_VERSION=1 -t upgrades:v1 .

# --upgradable mints an ECDSA P-256 control keypair and bakes the public
# half into every EIF for this enclave; the in-enclave server accepts
# signed upgrade commands only because of it. Immutable post-create: a
# non-upgradable enclave takes a single genesis push and nothing more.
enclavia enclave create --container-port 8080 --name upgrades --upgradable
enclavia push upgrades:v1 <enclave-id>
```

Wait for `running`, confirm you're on v1:

```sh
ID=<enclave-id>
curl -s https://$ID.enclaves.beta.enclavia.io/proxy/
# {"version":"1","path":"/"}
```

## Stage v2

A push to an `--upgradable` enclave that already has a genesis image does
not swap immediately; it **stages** an upgrade. The backend builds the v2
EIF and records its PCRs while the running v1 keeps serving.

```sh
docker build --build-arg APP_VERSION=2 -t upgrades:v2 .
enclavia push upgrades:v2 $ID
# Staged upgrade <upgrade-id> for enclave <enclave-id>
# Confirm with: enclavia upgrade confirm <enclave-id> <upgrade-id>

enclavia upgrade list $ID
# id            status  image                     valid_from  created
# <upgrade-id>  staged  ...@sha256:...            -           2026-...
```

## Confirm the cutover

```sh
# --immediate swaps now. Omit it to schedule (default: now + 7 days), or
# use --at <RFC3339> for a specific time; the enclave swaps automatically
# once valid_from passes, no further CLI action needed.
enclavia upgrade confirm $ID <upgrade-id> --immediate
```

A plain (no-storage) enclave just relaunches on the new EIF. A storage
enclave re-keys its encrypted volume for the new measurement before
swapping, and rolls back to the old version if anything fails. Once it
settles:

```sh
curl -s https://$ID.enclaves.beta.enclavia.io/proxy/
# {"version":"2","path":"/"}
```

## Read the attested chain

Every boot, upgrade, and revocation is recorded in an append-only, signed
chain that the CLI re-validates locally against the enclave's PCRs, image
digest, and control key:

```sh
enclavia upgrade chain $ID
```

## What this proves

- A running enclave can move to a new, independently-measured image
  without losing its identity: same control key, same persistent storage.
- Upgrades are a two-step commit (stage, then confirm) with optional
  scheduling, so the new PCRs are known and inspectable before anything
  swaps.
- The public upgrade chain is a tamper-evident log of every version the
  enclave has ever run.

Revoke a confirmed-but-not-yet-fired upgrade (the enclave keeps its
current version and a Revocation link is recorded):

```sh
enclavia upgrade revoke $ID <upgrade-id>
```
