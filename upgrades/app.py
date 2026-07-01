"""Reports its build version so you can watch an upgrade cut over.

The version comes from the APP_VERSION build arg baked into the image
(see the Dockerfile), so `v1` and `v2` are genuinely different images
with different measured PCRs.
"""

import http.server
import json
import os
import socketserver

VERSION = os.environ.get("APP_VERSION", "dev")


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            return

        body = json.dumps({"version": VERSION, "path": self.path}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print("upgrades-sample: " + (fmt % args), flush=True)


def main():
    port = int(os.environ.get("PORT", "8080"))
    print(f"upgrades-sample: version={VERSION} listening on :{port}", flush=True)
    with socketserver.ThreadingTCPServer(("", port), Handler) as srv:
        srv.allow_reuse_address = True
        srv.serve_forever()


if __name__ == "__main__":
    main()
