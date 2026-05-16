"""Makes an outbound HTTPS request from inside the enclave.

Exposed endpoints:

  GET /                  fetches a default URL (https://api.github.com/zen)
                         and returns its status + body.
  GET /?url=<url>        fetches the supplied URL instead.
  GET /health            startup probe (200 ok).

The result includes the response status and the first 500 bytes of the
body, plus a clear error message on failure. Connection refused / timeout
on a non-allow-listed destination is the expected failure mode and is
returned as JSON for easy inspection.

DNS note: the enclave runtime exposes a validating resolver on 127.0.0.1.
For the workload to use it, /etc/resolv.conf must point at 127.0.0.1
(the Dockerfile in this directory sets that up). If you change the base
image, make sure resolv.conf gets the same treatment.
"""

import http.server
import json
import os
import socket
import socketserver
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_URL = "https://api.github.com/zen"
TIMEOUT_SECS = 10


def fetch(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "egress-sample/1"})
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECS) as r:
            body = r.read(500)
            return {
                "url": url,
                "status": r.status,
                "body": body.decode("utf-8", errors="replace"),
            }
    except urllib.error.HTTPError as e:
        return {"url": url, "status": e.code, "error": str(e)}
    except urllib.error.URLError as e:
        return {"url": url, "error": f"URLError: {e.reason}"}
    except socket.timeout:
        return {"url": url, "error": f"timeout after {TIMEOUT_SECS}s"}
    except Exception as e:
        return {"url": url, "error": f"{type(e).__name__}: {e}"}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            return

        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        url = params.get("url", [DEFAULT_URL])[0]

        result = fetch(url)

        body = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print("egress-sample: " + (fmt % args), flush=True)


def main():
    port = int(os.environ.get("PORT", "8080"))
    with socketserver.ThreadingTCPServer(("", port), Handler) as srv:
        srv.allow_reuse_address = True
        print(f"egress-sample: listening on :{port}", flush=True)
        srv.serve_forever()


if __name__ == "__main__":
    main()
