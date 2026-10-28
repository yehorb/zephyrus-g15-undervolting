"""Minimal client for the LACT daemon socket (newline-delimited JSON)."""

import json
import socket

SOCK = "/run/lactd.sock"


def call(command, **args):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(SOCK)
    s.sendall((json.dumps({"command": command, "args": args}) + "\n").encode())
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    s.close()
    r = json.loads(buf)
    if r.get("status") != "ok":
        raise RuntimeError(r)
    return r["data"]
