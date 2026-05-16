# enclavia-samples

A handful of minimal Docker images for exercising different facets of the
[Enclavia](https://beta.enclavia.io) enclave runtime. Each subdirectory
ships one image plus a README walking through the create-and-test flow.

## Samples

| Directory | What it tests |
|-----------|---------------|
| [`hello/`](hello/)     | The basic happy path: build, push, create, HTTP-respond. |
| [`storage/`](storage/) | Persistent LUKS-encrypted volume at `/data`. |
| [`egress/`](egress/)   | Outbound HTTPS through the in-enclave allowlist. |

## Prereqs

- An account on `beta.enclavia.io` (or your own deployment).
- `enclavia` CLI installed and authenticated:

  ```sh
  enclavia auth login
  ```

- `docker` running locally for the `docker build` step.

## Generic workflow

Every sample follows the same pattern:

```sh
# Pick a sample.
cd hello/

# Build with a tag in your handle's namespace.
docker build -t <your-handle>/hello:v1 .

# Push to the Enclavia registry. The CLI handles the auth dance.
enclavia push <your-handle>/hello:v1

# Create an enclave from the image. Returns an enclave UUID.
enclavia enclave create --image hello:v1

# Talk to it. The dashboard surfaces the URL.
curl https://<enclave-id>.enclaves.beta.enclavia.io/
```

Per-sample variations (storage flags, egress allowlists) live in each
subdirectory's README.

## Image conventions

All samples follow the same shape so the runtime is happy with them:

- HTTP server listening on `:8080` (override with `PORT` env if needed).
- A `GET /health` endpoint returning `200 ok` (the runtime's startup probe).
- All logs go to stdout/stderr; the dashboard's runtime logs view pulls
  from the systemd journal underneath.

If you author your own image, copying these conventions is the fastest
path to a working enclave.

## License

Apache 2.0. See [LICENSE](LICENSE).
