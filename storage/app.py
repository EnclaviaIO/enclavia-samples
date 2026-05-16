"""Reads and writes /data to exercise the enclave's persistent volume.

On boot the app increments a `boot_count` stored in /data/state.json and
keeps it around across restarts. Each request to `/increment` bumps a
`request_count` field and persists the new state. `GET /` returns the
current snapshot.

If /data is not mounted (no `--storage` flag at create time) the writes
land in the rootfs tmpfs and disappear on stop, so the boot_count will
read as 1 every time.
"""

import http.server
import json
import os
import pathlib
import socketserver
import time

STATE_PATH = pathlib.Path("/data/state.json")


def read_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {"boot_count": 0, "request_count": 0, "first_seen": None}


def write_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE_PATH)


state = read_state()
state["boot_count"] = state.get("boot_count", 0) + 1
state["last_boot_at"] = int(time.time())
if state.get("first_seen") is None:
    state["first_seen"] = state["last_boot_at"]
write_state(state)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            return

        current = read_state()
        if self.path == "/increment":
            current["request_count"] = current.get("request_count", 0) + 1
            write_state(current)

        body = json.dumps(current).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print("storage-sample: " + (fmt % args), flush=True)


def main():
    port = int(os.environ.get("PORT", "8080"))
    print(
        f"storage-sample: boot_count={state['boot_count']} "
        f"first_seen={state['first_seen']} listening on :{port}",
        flush=True,
    )
    with socketserver.ThreadingTCPServer(("", port), Handler) as srv:
        srv.allow_reuse_address = True
        srv.serve_forever()


if __name__ == "__main__":
    main()
