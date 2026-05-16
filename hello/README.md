# hello

Smallest possible workload: a Python HTTP server that responds with a JSON
greeting on every request and serves `/health` for the enclave runtime's
startup probe.

Use this image to confirm the enclave end-to-end: build, push, create,
connect, get an HTTP response. Nothing else.

## Build and push

```sh
docker build -t <your-handle>/hello:v1 .
enclavia push <your-handle>/hello:v1
```

## Create the enclave

```sh
enclavia enclave create --image hello:v1
```

## Verify

```sh
# Replace <enclave-id> with the value `enclave create` printed.
curl https://<enclave-id>.enclaves.beta.enclavia.io/
# {"message":"hello from inside the enclave","hostname":"...","path":"/"}
```
