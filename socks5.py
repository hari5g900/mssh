#!/usr/bin/env python3
"""Minimal SOCKS5 proxy: CONNECT only, no authentication.
Loopback-only, stdlib-only, threaded.

Used by `mssh -Internet` (port 1080) so a remote can reach the internet
through this machine. shortcut: no auth methods and no UDP ASSOCIATE/BIND
-- add only if a real tool needs them.
"""

import socket
import struct
import sys
import threading

BUFSIZE = 65536


def _readn(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError("closed")
        buf += chunk
    return buf


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


def _reply(client, code):
    # VER REP RSV ATYP(IPv4) BND.ADDR(0.0.0.0) BND.PORT(0)
    client.sendall(bytes([5, code, 0, 1]) + b"\x00" * 6)


def _serve(client):
    try:
        ver, nmethods = _readn(client, 2)
        if ver != 5:
            return
        methods = _readn(client, nmethods)
        if 0 not in methods:  # require "no authentication"
            client.sendall(b"\x05\xff")
            return
        client.sendall(b"\x05\x00")

        ver, cmd, _rsv, atyp = _readn(client, 4)
        if ver != 5 or cmd != 1:  # CONNECT only
            _reply(client, 7)
            return
        if atyp == 1:
            host = socket.inet_ntoa(_readn(client, 4))
        elif atyp == 3:
            n = _readn(client, 1)[0]
            host = _readn(client, n).decode("latin1")
        elif atyp == 4:
            host = socket.inet_ntop(socket.AF_INET6, _readn(client, 16))
        else:
            _reply(client, 8)
            return
        port = struct.unpack(">H", _readn(client, 2))[0]

        try:
            remote = socket.create_connection((host, port), timeout=10)
        except OSError:
            _reply(client, 1)  # general failure
            return
        _reply(client, 0)  # success
        up = threading.Thread(target=_pump, args=(client, remote), daemon=True)
        up.start()
        _pump(remote, client)
        up.join(timeout=30)
        remote.close()
    except OSError:
        pass
    finally:
        try:
            client.close()
        except OSError:
            pass


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 1080
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(64)
    print("socks5: listening on 127.0.0.1:%d" % port, file=sys.stderr)
    while True:
        client, _addr = srv.accept()
        threading.Thread(target=_serve, args=(client,), daemon=True).start()


if __name__ == "__main__":
    main()
