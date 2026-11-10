"""Run a command with network egress limited to an allowlist (macOS).

The child runs under sandbox-exec with all non-loopback outbound traffic denied and
file writes limited to the given directories. Its only way out is a loopback CONNECT
proxy run by this (unsandboxed) parent, which allows only the listed host:port pairs
and logs every attempt.

Usage: python3 -I egress.py --allow openrouter.ai:443 --log run/egress.log \
           --write-dir run/ -- <child argv...>
"""

from __future__ import annotations

import argparse
import os
import select
import socket
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path

LOG_LOCK = threading.Lock()


def log(path: Path, msg: str) -> None:
    with LOG_LOCK, path.open("a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")


def make_handler(allow: set[tuple[str, int]], log_path: Path):
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            conn = self.request
            conn.settimeout(30)
            data = b""
            while b"\r\n\r\n" not in data and len(data) < 65536:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            line = data.split(b"\r\n", 1)[0].decode("latin-1")
            parts = line.split()
            if len(parts) < 2 or parts[0].upper() != "CONNECT":
                log(log_path, f"DENY non-CONNECT {line[:120]!r}")
                conn.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                return
            host, _, port = parts[1].rpartition(":")
            try:
                target = (host.strip("[]").lower(), int(port))
            except ValueError:
                target = (parts[1], -1)
            if target not in allow:
                log(log_path, f"DENY {parts[1]}")
                conn.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                return
            try:
                up = socket.create_connection(target, timeout=30)
            except OSError as e:
                log(log_path, f"FAIL {parts[1]} {e}")
                conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                return
            log(log_path, f"ALLOW {parts[1]}")
            conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            conn.settimeout(None)
            up.settimeout(None)
            socks = [conn, up]
            try:
                while True:
                    r, _, x = select.select(socks, [], socks, 600)
                    if x or not r:
                        break
                    for s in r:
                        buf = s.recv(65536)
                        if not buf:
                            return
                        (up if s is conn else conn).sendall(buf)
            finally:
                up.close()

    return Handler


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def profile(write_dirs: list[str]) -> str:
    allows = " ".join(f'(subpath "{os.path.realpath(d)}")' for d in write_dirs)
    return f"""(version 1)
(allow default)
(deny network-outbound (remote ip))
(allow network-outbound (remote ip "localhost:*"))
(deny file-write*)
(allow file-write* {allows} (subpath "/dev"))
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--allow", action="append", required=True, help="host:port")
    ap.add_argument("--log", required=True)
    ap.add_argument("--write-dir", action="append", default=[])
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    cmd = a.cmd[1:] if a.cmd and a.cmd[0] == "--" else a.cmd
    log_path = Path(a.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    allow = set()
    for hp in a.allow:
        h, _, p = hp.rpartition(":")
        allow.add((h.lower(), int(p)))
    srv = Server(("127.0.0.1", 0), make_handler(allow, log_path))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    proxy = f"http://127.0.0.1:{port}"
    env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
    env.update({"HTTPS_PROXY": proxy, "HTTP_PROXY": proxy, "https_proxy": proxy, "http_proxy": proxy,
                "NO_PROXY": "", "no_proxy": ""})
    log(log_path, f"START proxy={proxy} allow={sorted(allow)} cmd={cmd[:3]}")
    rc = subprocess.call(["sandbox-exec", "-p", profile(a.write_dir), *cmd], env=env)
    log(log_path, f"EXIT rc={rc}")
    srv.shutdown()
    return rc


if __name__ == "__main__":
    sys.exit(main())
