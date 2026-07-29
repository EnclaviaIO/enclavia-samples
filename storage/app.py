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
    data = json.dumps(state).encode()

    # Durable write-then-rename: fsync the tmp file's data before the
    # rename makes it visible, then fsync the directory entry too, so a
    # power-off/kill right after this call returns can't lose the write
    # (this is what makes the state actually survive a stop/start cycle;
    # without it the write can sit dirty in the guest's page cache for
    # up to a filesystem commit interval).
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)

    os.replace(tmp, STATE_PATH)

    dir_fd = os.open(STATE_PATH.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


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
