#!/usr/bin/env python3
"""Remote ASTAP plate-solve through Home Assistant / Nabu Casa.

This talks to the ASTAP Plate Solver add-on entirely over the Home Assistant
Cloud (Nabu Casa) remote URL — no VPN, no exposed ports, no reverse proxy.

How it works (all steps verified against a live install):
  1. Open the HA WebSocket API and authenticate with a Long-Lived Access Token.
  2. Ask HA Core to proxy a Supervisor request (``supervisor/api``) for an
     ingress *session* — this works even with a normal (non-admin) token,
     because HA Core makes the Supervisor call with its own privileges.
  3. Look up the add-on's dynamic ``ingress_url`` (it is NOT the slug).
  4. POST the image to ``<ingress_url>/solve`` with the ingress_session cookie.
     The add-on's ``ingress_stream: true`` lets the full file through.

Usage:
  export HASS_URL="https://xxxxx.ui.nabu.casa"
  export HASS_TOKEN="ey..."            # long-lived access token
  python3 remote_solve.py M31.fits --ra 10.68 --dec 41.27 --fov 1.5

Only the Python standard library is used, so it runs anywhere.
"""
import argparse
import base64
import json
import os
import socket
import ssl
import struct
import sys
import uuid
from urllib.parse import urlparse

ADDON_SLUG = "d5d3ad43_astap_solver"


# --- minimal WebSocket client (stdlib only) --------------------------------

class WS:
    def __init__(self, host, port=443):
        raw = socket.create_connection((host, port), timeout=30)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            f"GET /api/websocket HTTP/1.1\r\nHost: {host}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode()
        )
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += self.sock.recv(1024)
        if b"101" not in resp.split(b"\r\n")[0]:
            raise RuntimeError(f"WebSocket handshake failed: {resp[:80]!r}")

    def send(self, obj):
        data = json.dumps(obj).encode()
        header = bytearray([0x81])
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        self.sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self):
        def rd(n):
            buf = b""
            while len(buf) < n:
                chunk = self.sock.recv(n - len(buf))
                if not chunk:
                    raise ConnectionError("WebSocket closed")
                buf += chunk
            return buf
        _, b1 = rd(2)
        length = b1 & 0x7f
        if length == 126:
            length = struct.unpack(">H", rd(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", rd(8))[0]
        return json.loads(rd(length).decode(errors="ignore"))

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def supervisor_api(ws, msg_id, endpoint, method="get"):
    ws.send({"id": msg_id, "type": "supervisor/api", "endpoint": endpoint, "method": method})
    while True:
        m = ws.recv()
        if m.get("id") == msg_id and m.get("type") == "result":
            if not m.get("success"):
                raise RuntimeError(f"supervisor/api {endpoint} failed: {m}")
            return m["result"]


def get_session_and_ingress(host, token, slug):
    """Return (ingress_session, ingress_url) via the WebSocket API."""
    ws = WS(host)
    try:
        assert ws.recv().get("type") == "auth_required"
        ws.send({"type": "auth", "access_token": token})
        if ws.recv().get("type") != "auth_ok":
            raise RuntimeError("token rejected (auth failed)")
        session = supervisor_api(ws, 1, "/ingress/session", "post")["session"]
        info = supervisor_api(ws, 2, f"/addons/{slug}/info", "get")
        ingress_url = info.get("ingress_url")
        if info.get("state") != "started":
            print(f"warning: add-on state is {info.get('state')!r}", file=sys.stderr)
        if not ingress_url:
            raise RuntimeError("add-on has no ingress_url (is ingress enabled?)")
        return session, ingress_url
    finally:
        ws.close()


# --- multipart upload over plain HTTPS -------------------------------------

def post_multipart(host, path, session, filename, file_bytes, fields):
    boundary = "----astap" + uuid.uuid4().hex
    pre = []
    for k, v in fields.items():
        if v is None:
            continue
        pre.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n")
    body = "".join(pre).encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
             f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode()
    body += file_bytes + f"\r\n--{boundary}--\r\n".encode()

    raw = socket.create_connection((host, 443), timeout=300)
    sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
    headers = (
        f"POST {path} HTTP/1.1\r\nHost: {host}\r\n"
        f"Cookie: ingress_session={session}\r\n"
        f"Content-Type: multipart/form-data; boundary={boundary}\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    ).encode()
    sock.sendall(headers + body)

    resp = b""
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        resp += chunk
    sock.close()
    head, _, payload = resp.partition(b"\r\n\r\n")
    status = int(head.split(b"\r\n")[0].split()[1])
    # Handle chunked transfer encoding minimally.
    if b"transfer-encoding: chunked" in head.lower():
        payload = _dechunk(payload)
    return status, payload.decode(errors="ignore")


def _dechunk(data):
    out = b""
    while data:
        line, _, rest = data.partition(b"\r\n")
        try:
            size = int(line.strip(), 16)
        except ValueError:
            break
        if size == 0:
            break
        out += rest[:size]
        data = rest[size + 2:]
    return out


def main():
    ap = argparse.ArgumentParser(description="Remote ASTAP plate-solve via Nabu Casa")
    ap.add_argument("image", help="FITS/JPG/PNG file to solve")
    ap.add_argument("--ra", type=float, help="RA hint (degrees)")
    ap.add_argument("--dec", type=float, help="Dec hint (degrees)")
    ap.add_argument("--fov", type=float, help="field-of-view height (degrees)")
    ap.add_argument("--radius", type=float, help="search radius (degrees)")
    ap.add_argument("--url", default=os.environ.get("HASS_URL"), help="HA base URL (or $HASS_URL)")
    ap.add_argument("--token", default=os.environ.get("HASS_TOKEN"), help="long-lived token (or $HASS_TOKEN)")
    ap.add_argument("--slug", default=ADDON_SLUG, help="add-on slug")
    args = ap.parse_args()

    if not args.url or not args.token:
        ap.error("set --url/--token or $HASS_URL/$HASS_TOKEN")

    host = urlparse(args.url).netloc or args.url
    with open(args.image, "rb") as f:
        file_bytes = f.read()

    print(f"→ getting ingress session for {args.slug} ...", file=sys.stderr)
    session, ingress_url = get_session_and_ingress(host, args.token, args.slug)

    print(f"→ uploading {len(file_bytes) / 1e6:.1f} MB and solving ...", file=sys.stderr)
    fields = {
        "ra": args.ra, "dec": args.dec, "fov": args.fov, "radius": args.radius,
    }
    status, text = post_multipart(
        host, ingress_url.rstrip("/") + "/solve",
        session, os.path.basename(args.image), file_bytes, fields,
    )

    if status == 200:
        print(json.dumps(json.loads(text), indent=2, ensure_ascii=False))
    else:
        print(f"HTTP {status}\n{text}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
