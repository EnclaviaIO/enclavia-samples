# mnemonic-vault

[![Deploy on Enclavia](https://beta.enclavia.io/deploy-button.svg)](https://beta.enclavia.io/deploy?manifest=https://raw.githubusercontent.com/EnclaviaIO/enclavia-samples/main/mnemonic-vault/enclavia.json)

Back up a wallet mnemonic under an email and a password. If you lose the
mnemonic, the same email and password get it back.

The mnemonic and the password only exist in your browser and inside the
enclave. The web app ([`web/index.html`](web/index.html)) talks to the
enclave through
[`@enclavia/client-wasm`](https://www.npmjs.com/package/@enclavia/client-wasm),
so the encrypted channel ends in your browser, after the browser has
checked the enclave's attestation.

The one-click deploy above is a **demo**: it can't turn on the settings
that make the guess limit hold (see [Deploy for real](#deploy-for-real)).

## How it protects the mnemonic

A password is a weak secret. Anyone with unlimited guesses would find
most passwords. So the vault's real protection is a **guess limit**:

- The enclave allows **10 wrong passwords in a row** per account. The
  10th erases the backup. A correct password resets the count.
- Every guess is counted and written to disk **before** the password
  is checked. Killing the enclave in the middle of a request costs the
  guesser a try. It doesn't give one back.
- There is no "wait an hour" backoff. The enclave's clock comes from
  the host, so a timer is something the host could skip. A count can't
  be skipped.

On disk, each account is:

```
key        = Argon2id(password, salt)            64 MiB, 3 passes
ciphertext = AES-256-GCM(key, mnemonic)
row        = sha256(email), salt, nonce, ciphertext, attempts
```

A wrong password shows up as a failed decrypt. The mnemonic is never on
disk in plaintext, and the email isn't stored at all, only its hash. A
copy of `/data` still needs the password, and Argon2 makes each offline
guess slow.

The rules are [`app.py`](app.py). The enclave's PCRs pin that file, so a
client that checked the attestation knows them.

## Deploy for real

```sh
enclavia enclave create \
    --production \
    --anti-rollback \
    --container-port 8080 \
    --name mnemonic-vault \
    --storage-size-bytes 268435456
# Enclave created:
#   ID:     1d2c3b4a-...

docker build -t mnemonic-vault:v1 .
enclavia push mnemonic-vault:v1 1d2c3b4a
enclavia enclave status 1d2c3b4a-5e6f-7a8b-9c0d-1e2f3a4b5c6d   # wait for running, copy the PCRs
```

Two flags carry the security:

- **`--anti-rollback`** stops the host from rolling `/data` back to an
  older copy. Without it, the host can save a copy of the disk, make 9
  guesses, put the copy back, and repeat forever. The guess limit would
  then mean nothing. It needs `--production` and a plan that includes
  it. Check that it took effect with `enclavia reproduce`: the
  synchronizer setting is part of the measured image.
- **No `--upgradable`.** On an upgradable enclave with managed
  custody, whoever controls the Enclavia account can ship a new version
  that reads every mnemonic. If you need upgrades, use
  [`--control-key`](https://docs.enclavia.io/custody) with
  [`--min-upgrade-delay`](https://docs.enclavia.io/upgrades#minimum-upgrade-delay),
  so users get time to see an upgrade coming and pull their backups out.

## Try it (debug enclave)

For trying it out, skip `--production` and `--anti-rollback`. A debug
enclave runs under QEMU on Enclavia's host and has **no rollback
protection**, so don't put a real mnemonic in it.

```sh
enclavia deploy mnemonic-vault:v1 \
    --container-port 8080 \
    --name mnemonic-vault \
    --storage-size-bytes 268435456 \
    --upgradable
```

`--upgradable` makes iterating cheaper (see
[Staged deployments](https://docs.enclavia.io/upgrades)). Leave it out
for anything real, as above.

## Open the web app

- **Served by the enclave**, through the hosted proxy:
  `https://<enclave-id>.enclaves.beta.enclavia.io/proxy/`. You trust
  the proxy for the page's JavaScript. The mnemonic and password then
  travel over the SDK's own attested channel, not through the proxy.
- **From your own machine**: `python3 -m http.server -d web 8000`, then
  open `http://localhost:8000`. Now you don't trust anyone for the page.

Then paste the enclave ID and the three PCRs from
`enclavia enclave status`, tick **Debug mode** for a debug enclave, and
**Connect and verify**. "Get PCRs from proxy" fills them in from the
proxy's headers, which means trusting the proxy for those values.

The debug console shows every request and response, with passwords and
mnemonics replaced by `••••`.

## API

All requests go over the SDK channel. Never send a password or a
mnemonic through `/proxy/`: the proxy would see them.

| Method | Path | Body | Reply |
|--------|------|------|-------|
| `POST` | `/register` | `{"email", "password", "mnemonic"}` | `201`. `409` if the email already has a backup: backups are never overwritten. Password at least 8 characters. |
| `POST` | `/recover` | `{"email", "password"}` | `200 {"mnemonic"}`. `401 {"attempts_left"}` on a wrong password. `410` when that guess erased the backup. `404` if there's no backup. |
| `POST` | `/delete` | `{"email", "password"}` | `200`. A wrong password counts as a guess, same as `/recover`. |
| `GET` | `/info` | | The guess limit, the minimum password length and the KDF settings. |
| `GET` | `/health` | | Liveness. |
| `GET` | `/` | | The web app. |

Emails are trimmed and lowercased. The vault doesn't check that the
mnemonic is valid BIP-39: it stores any string up to 1024 characters.

## Known limits

- **Nobody checks that you own the email.** So anyone who knows your
  email can use up your 10 guesses and erase your backup, and anyone can
  claim an email before its owner does. The fix is to require a code
  sent to the email before `/register` and `/recover`: an egress
  allowlist entry for one email API (as in [`notifier/`](../notifier/))
  and its API key as a [secret](../secrets/).
- **Whether an email has a backup is not private.** `/register` answers
  `409` for a taken email, and `/recover` answers `404` for an unknown
  one.
- **A strong password still matters.** The guess limit only protects
  you while the enclave holds. If the enclave were ever broken, an
  attacker would get the ciphertext and could guess offline, slowed only
  by Argon2.
- **The enclave is the only copy.** `enclavia enclave destroy` erases
  every backup. So does losing the storage volume.

## Test it locally

The app runs in plain Docker too (no attestation, no rollback
protection):

```sh
docker build -t mnemonic-vault:dev .
docker run --rm -p 8080:8080 -v "$PWD/data:/data" mnemonic-vault:dev

curl -s localhost:8080/register -H 'Content-Type: application/json' \
    -d '{"email":"alice@example.com","password":"correct horse","mnemonic":"abandon abandon ... about"}'
curl -s localhost:8080/recover -H 'Content-Type: application/json' \
    -d '{"email":"alice@example.com","password":"correct horse"}'
```
