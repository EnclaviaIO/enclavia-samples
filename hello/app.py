"""Minimal HTTP responder used as a smoke test for the enclave runtime."""

import http.server
import json
import os
import socket
import socketserver


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            return

        body = json.dumps(
            {
                "message": "hello from inside the enclave",
                "hostname": socket.gethostname(),
                "path": self.path,
            }
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print("hello-sample: " + (fmt % args), flush=True)


def main():
    port = int(os.environ.get("PORT", "8080"))
    with socketserver.ThreadingTCPServer(("", port), Handler) as srv:
        srv.allow_reuse_address = True
        print(f"hello-sample: listening on :{port}", flush=True)
        srv.serve_forever()


if __name__ == "__main__":
    main()
