"""Reads a per-enclave secret from the environment and reports that it is
present, without ever revealing the value.

Set the secret with `enclavia secret set <id> API_TOKEN=...`; the backend
encrypts it at rest and injects it into this process's environment at boot.
It never appears in the image, the EIF, the measured PCRs, or the build log.
"""

import http.server
import json
import os
import socketserver

SECRET_NAME = "API_TOKEN"


def masked(value):
    """A value hint safe to log/return: never the secret itself."""
    if value is None:
        return None
    if len(value) <= 6:
        return f"({len(value)} chars)"
    return f"{value[:3]}...{value[-2:]} ({len(value)} chars)"


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            return

        value = os.environ.get(SECRET_NAME)
        body = json.dumps(
            {
                "secret_name": SECRET_NAME,
                "present": value is not None,
                "masked": masked(value),
            }
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print("secrets-sample: " + (fmt % args), flush=True)


def main():
    port = int(os.environ.get("PORT", "8080"))
    value = os.environ.get(SECRET_NAME)
    print(
        f"secrets-sample: {SECRET_NAME} present={value is not None} "
        f"masked={masked(value)}",
        flush=True,
    )
    with socketserver.ThreadingTCPServer(("", port), Handler) as srv:
        srv.allow_reuse_address = True
        print(f"secrets-sample: listening on :{port}", flush=True)
        srv.serve_forever()


if __name__ == "__main__":
    main()
