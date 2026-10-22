#!/usr/bin/env python3
"""Minimal HTTP proxy: CONNECT tunneling (https) + absolute-URI forwarding
(http). Loopback-only, stdlib-only, threaded.

Used by `mssh -Internet` (port 1081) so a remote can reach the internet
through this machine. shortcut: no chunked request bodies, no caching or
proxy auth -- add only if a real tool needs them.
"""

import http.client
import io
import socket
import sys
import threading
from http.client import parse_headers
from urllib.parse import urlsplit

BUFSIZE = 65536


def _pump(src, dst):
    try:
        while True:
            data = src.recv(BUFSIZE)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _tunnel(client, host, port):
    try:
        remote = socket.create_connection((host, port), timeout=10)
    except OSError:
        try:
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n"
                           b"Content-Length: 0\r\nConnection: close\r\n\r\n")
        except OSError:
            pass
        return
    client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    up = threading.Thread(target=_pump, args=(client, remote), daemon=True)
    up.start()
    _pump(remote, client)
    up.join(timeout=30)
    remote.close()


def _read_head(client):
    head = b""
    client.settimeout(60)
    while b"\r\n\r\n" not in head:
        if len(head) > 65536:
            return None
        try:
            chunk = client.recv(BUFSIZE)
        except OSError:
            return None
        if not chunk:
            return None
        head += chunk
    return head


def _forward(client, method, target, rest):
    u = urlsplit(target)
    if not u.hostname:
        return
    hdrs = parse_headers(io.BytesIO(rest))
    fwd = {k: v for k, v in hdrs.items()
           if k.lower() not in ("host", "connection", "content-length",
                                "proxy-connection", "proxy-authorization",
                                "keep-alive", "upgrade", "expect",
                                "transfer-encoding")}
    cl = int(hdrs.get("Content-Length", 0) or 0)
    body = rest[:cl] if cl else None
    while body is not None and len(body) < cl:
        chunk = client.recv(BUFSIZE)
        if not chunk:
            body = None
            break
        body += chunk
    if body is not None:
        body = body[:cl]
    try:
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=60)
        path = (u.path or "/") + (("?" + u.query) if u.query else "")
        conn.request(method, path, body=body, headers=fwd)
        resp = conn.getresponse()
    except OSError:
        try:
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n"
                           b"Content-Length: 0\r\nConnection: close\r\n\r\n")
        except OSError:
            pass
        return
    out = ("HTTP/1.1 %d %s\r\n" % (resp.status, resp.reason)).encode("latin1")
    for k, v in resp.getheaders():
        if k.lower() not in ("connection", "transfer-encoding",
                             "keep-alive", "upgrade"):
            out += ("%s: %s\r\n" % (k, v)).encode("latin1")
    out += b"Connection: close\r\n\r\n"
    client.sendall(out)
    try:
        while True:
            chunk = resp.read(BUFSIZE)
            if not chunk:
                break
            client.sendall(chunk)
    except OSError:
        pass
    conn.close()


def _serve(client):
    try:
        head = _read_head(client)
        if head is None:
            return
        line, _, rest = head.partition(b"\r\n\r\n")
        parts = line.split(None, 2)
        if len(parts) < 2:
            return
        method = parts[0].decode("latin1")
        target = parts[1].decode("latin1")
        if method == "CONNECT":
            host, _, port = target.partition(":")
            _tunnel(client, host, int(port or 443))
        elif method in ("GET", "HEAD", "POST", "PUT", "PATCH",
                        "DELETE", "OPTIONS"):
            _forward(client, method, target, rest)
    finally:
        try:
            client.close()
        except OSError:
            pass


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 1081
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(64)
    print("http_proxy: listening on 127.0.0.1:%d" % port, file=sys.stderr)
    while True:
        client, _addr = srv.accept()
        threading.Thread(target=_serve, args=(client,), daemon=True).start()


if __name__ == "__main__":
    main()
