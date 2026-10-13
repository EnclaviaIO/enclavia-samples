# enclavia-samples

A handful of minimal Docker images for exercising different facets of the
[Enclavia](https://beta.enclavia.io) enclave runtime. Each subdirectory
ships one image plus a README walking through the create-and-test flow.

## Samples

| Directory | What it tests |
|-----------|---------------|
| [`hello/`](hello/)       | The basic happy path: build, push, create, HTTP-respond, `curl` it back through the hosted proxy. |
| [`storage/`](storage/)   | Persistent LUKS-encrypted volume at `/data`. |
| [`egress/`](egress/)     | Outbound HTTPS through the in-enclave allowlist. |
| [`secrets/`](secrets/)   | Per-enclave environment-variable secrets: set a value with `enclavia secret set`, injected into the workload at boot, never baked into the image. |
| [`upgrades/`](upgrades/) | Ship a new image version to a running enclave: `--upgradable` create, stage a v2 push, confirm the cutover, and read the attested upgrade chain. |
| [`notifier/`](notifier/) | Storage + egress allowlist + a secret together. Watches Bitcoin descriptors via `mempool.space` and pushes notifications through Expo on matching transactions. |
| [`mnemonic-vault/`](mnemonic-vault/) | Back up a wallet mnemonic under an email and password. Storage + a web app on the client SDK. The enclave limits wrong-password guesses, which only holds against the host with `--production --anti-rollback`. |
| [`localhost-proxy/`](localhost-proxy/) | Rust binary that runs on your laptop and bridges plain HTTP + WebSocket on `127.0.0.1:NNNN` to an enclave over the Noise channel, verifying attestation client-side. Point an unmodified browser wallet at `http://localhost:NNNN` and the requests reach the enclave attested. |

## One-click deploy

Every sample image is published to the GitHub Container Registry by the
[`publish-images`](.github/workflows/publish-images.yml) workflow, and each
sample directory carries an [`enclavia.json`](hello/enclavia.json) manifest,
so any of them can be launched straight from the browser with no CLI. Click
the **Deploy on Enclavia** badge in a sample's README (it opens the create
form prefilled from that manifest), or deploy the default `hello` workload
from the repo-root manifest:

[![Deploy on Enclavia](https://beta.enclavia.io/deploy-button.svg)](https://beta.enclavia.io/deploy?manifest=https://raw.githubusercontent.com/EnclaviaIO/enclavia-samples/main/enclavia.json)

See the [deploy-button docs](https://docs.enclavia.io/deploy-button) for the
manifest schema and how to add the button to your own repo.

## Prereqs

- An account on `beta.enclavia.io` (or your own deployment).
- `enclavia` CLI installed and authenticated:

  ```sh
  enclavia auth login
  ```

- `docker` running locally for the `docker build` step.

## Generic workflow

Every sample follows the same three-step pattern: **create an enclave**,
**push your image into the repo it provisioned**, then **observe what
the running workload does**.

```sh
cd hello/

# 1. Build the image locally. The tag is arbitrary — the CLI rewrites
#    it on push.
docker build -t hello:v1 .

# 2. Create an enclave. This reserves a private repo at
#    registry.beta.enclavia.io/<your-handle>/<enclave-uuid> and returns
#    the enclave id (a UUID). The enclave stays in `waiting_for_image`
#    until you push something.
enclavia enclave create --container-port 8080 --name hello

# Output (example):
#   Enclave created:
#     ID:     1d2c3b4a-5e6f-7a8b-9c0d-1e2f3a4b5c6d
#     Status: waiting_for_image

# 3. Push your local image to that enclave's repo. The second argument
#    is the enclave id (a unique prefix is fine). This flips it to
#    `building`; once the build finishes the enclave is `running`.
enclavia push hello:v1 1d2c3b4a

# Watch progress with the full UUID:
enclavia enclave status 1d2c3b4a-5e6f-7a8b-9c0d-1e2f3a4b5c6d
```

Per-sample variations (storage size, egress allowlists) live in each
subdirectory's README — they're flags you add to step 2's
`enclavia enclave create`.

## Observing the workload

Every running enclave gets a **hosted HTTPS URL** you can `curl`:

```
https://<enclave-id>.enclaves.beta.enclavia.io/proxy/<path>
```

The `/proxy/` prefix is stripped before the request reaches your
workload, so a container endpoint of `GET /health` is reachable at
`/proxy/health`. Every response carries `X-Enclavia-PCR0`, `-PCR1`,
`-PCR2` headers: the PCRs the proxy verified during the attestation
handshake (they match `enclavia enclave status`). Full reference at
[docs.enclavia.io/proxy](https://docs.enclavia.io/proxy).

The hosted path terminates TLS at Enclavia's edge and runs the Noise
handshake plus the Nitro attestation check **on your behalf**, so you
trust Enclavia's edge to have verified the enclave (it can see your
plaintext in transit). For a **trustless** connection where your own
device is the attestor, connect with the client SDK (below) or run the
same proxy yourself ([self-host](https://docs.enclavia.io/self-host-proxy)).

Beyond `curl`, three more ways to observe a running sample:

1. **Read build + runtime logs from the dashboard.** Open the enclave
   in [beta.enclavia.io](https://beta.enclavia.io) — the runtime-logs
   view streams from the host's journal under the hood, so anything
   your container prints to stdout/stderr shows up there. The egress
   and storage samples log their results on startup specifically so
   you can see them this way without needing a client at all.
2. **Ask an AI agent via MCP.** Once you've wired up the
   [MCP connector](https://docs.enclavia.io/mcp), the agent has
   `enclave_status` and `enclave_logs` tools that let you ask
   "what did my hello enclave just print?" in plain English.
3. **Connect trustlessly from your own code.** To verify attestation
   in your own process (not trusting the hosted proxy), use a client
   that speaks the Noise channel. Today that means a small amount of
   Rust against the [`enclavia` client crate](https://docs.enclavia.io/connect);
   official bindings for **Python, Java, and JavaScript (WASM)** are
   on the way (see [enclavia#7](https://github.com/EnclaviaIO/enclavia/issues/7))
   so the same flow will be available from your language of choice
   shortly.

The minimum Rust client is:

```rust
use enclavia::{Client, Pcrs};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let pcrs = Pcrs {
        pcr0: hex::decode("...your pcr0...")?,
        pcr1: hex::decode("...your pcr1...")?,
        pcr2: hex::decode("...your pcr2...")?,
    };
    let client = Client::connect(
        "wss://<enclave-id>.enclaves.beta.enclavia.io",
        pcrs,
    ).await?;
    let resp = client.get("/").send().await?;
    println!("{} — {}", resp.status(), resp.text()?);
    Ok(())
}
```

`enclave status` prints the PCRs you need to pin. See the linked docs
for the full setup (Cargo.toml dependency, `debug_mode` for local
debug enclaves, etc.).

## Image conventions

All samples follow the same shape so the runtime is happy with them:

- HTTP server listening on `:8080` (override with `PORT` env if needed).
  The port has to match the `--container-port` you pass to
  `enclave create`.
- A `GET /health` endpoint returning `200 ok` (the runtime's startup
  probe).
- All logs go to stdout/stderr; the dashboard's runtime-logs view
  pulls from the systemd journal underneath.

If you author your own image, copying these conventions is the fastest
path to a working enclave.

## License

Apache 2.0. See [LICENSE](LICENSE).
