"""Tests for the -Internet proxy scripts: http_proxy.py (absolute-URI GET +
CONNECT tunneling) and socks5.py (CONNECT). Run from the repo root:
    python tests/test_proxies.py
Expects: all requests return the target's body through each proxy."""

import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

TARGET_PORT = 53130
HTTP_PROXY_PORT = 53131
SOCKS_PORT = 53132


class Target(BaseHTTPRequestHandler):
    def do_GET(self):
        body = ("target-ok:%s" % self.path).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def recv_until(sock, marker, timeout=10):
    sock.settimeout(timeout)
    buf = b""
    while marker not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf += chunk
    return buf


def http_proxy_absolute_get():
    s = socket.create_connection(("127.0.0.1", HTTP_PROXY_PORT), timeout=10)
    s.sendall(("GET http://127.0.0.1:%d/abs HTTP/1.1\r\n"
               "Host: 127.0.0.1\r\nConnection: close\r\n\r\n"
               % TARGET_PORT).encode())
    resp = recv_until(s, b"target-ok")
    assert b"200" in resp.split(b"\r\n")[0], resp[:200]
    assert b"target-ok:/abs" in resp, resp[:200]
    s.close()
    print("http_proxy absolute-URI GET -> 200 target-ok:/abs")


def http_proxy_connect():
    s = socket.create_connection(("127.0.0.1", HTTP_PROXY_PORT), timeout=10)
    s.sendall(("CONNECT 127.0.0.1:%d HTTP/1.1\r\n\r\n" % TARGET_PORT).encode())
    resp = recv_until(s, b"\r\n\r\n")
    assert b"200" in resp.split(b"\r\n")[0], resp[:200]
    s.sendall(("GET /tunnel HTTP/1.1\r\nHost: 127.0.0.1\r\n"
               "Connection: close\r\n\r\n").encode())
    body = recv_until(s, b"target-ok")
    assert b"target-ok:/tunnel" in body, body[:200]
    s.close()
    print("http_proxy CONNECT tunnel -> 200 target-ok:/tunnel")


def socks5_connect():
    s = socket.create_connection(("127.0.0.1", SOCKS_PORT), timeout=10)
    s.sendall(b"\x05\x01\x00")
    greeting = recv_until(s, b"\x05\x00")
    assert greeting[:2] == b"\x05\x00", greeting[:64]
    s.sendall(b"\x05\x01\x00\x01" + socket.inet_aton("127.0.0.1")
              + TARGET_PORT.to_bytes(2, "big"))
    reply = recv_until(s, b"\x00" * 4)
    assert reply[1] == 0, reply[:64]  # 0 = succeeded
    s.sendall(("GET /socks HTTP/1.1\r\nHost: 127.0.0.1\r\n"
               "Connection: close\r\n\r\n").encode())
    body = recv_until(s, b"target-ok")
    assert b"target-ok:/socks" in body, body[:200]
    s.close()
    print("socks5 CONNECT -> 200 target-ok:/socks")


target_server = ThreadingHTTPServer(("127.0.0.1", TARGET_PORT), Target)
threading.Thread(target=target_server.serve_forever, daemon=True).start()

http_proxy = subprocess.Popen(
    [sys.executable, str(REPO / "http_proxy.py"), str(HTTP_PROXY_PORT)],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
socks5 = subprocess.Popen(
    [sys.executable, str(REPO / "socks5.py"), str(SOCKS_PORT)],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    deadline = time.time() + 6
    for port in (HTTP_PROXY_PORT, SOCKS_PORT):
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                time.sleep(0.05)
        else:
            raise SystemExit("proxy on %d never came up" % port)

    http_proxy_absolute_get()
    http_proxy_connect()
    socks5_connect()
    print("PROXY TEST PASS")
finally:
    http_proxy.terminate()
    socks5.terminate()
    target_server.shutdown()
