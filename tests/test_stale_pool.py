"""Stale keep-alive pool test: a gateway that closes idle keep-alive
connections (vLLM/uvicorn closes them after ~5s; the mock here uses 0.3s)
must not cause failed requests through the relay. Run from the repo root:
    python tests/test_stale_pool.py
Expects: a POST that arrives after the gateway closed the pooled upstream
connection still gets 200 -- the relay discards pooled connections idle
beyond its TTL and retries once on a fresh socket. Before the fix this
request died with http.client.RemoteDisconnected and the remote SDK had
to retry."""

import http.client
import importlib.util
import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RELAY = REPO / "oc-relay.py"

MOCK_PORT = 53120
RELAY_PORT = 53121
IDLE_CLOSE = 0.3  # mock gateway closes idle keep-alive conns after this


class IdleClosingMock(BaseHTTPRequestHandler):
    """Keeps the connection alive but closes it once it sits idle, which is
    what uvicorn/vLLM do (--timeout-keep-alive)."""

    protocol_version = "HTTP/1.1"

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        self.rfile.read(n)
        payload = json.dumps({"ok": True, "path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        # from now on, an idle gap longer than IDLE_CLOSE kills this conn
        self.connection.settimeout(IDLE_CLOSE)

    def log_message(self, *a):
        pass


def wait_port(port, timeout=6.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            c.request("GET", "/")
            c.getresponse().read()
            return
        except Exception:
            time.sleep(0.05)
    raise SystemExit("port %d never came up" % port)


# --- unit check: the pool TTL discards stale entries, reuses fresh ones ---
spec = importlib.util.spec_from_file_location("oc_relay", str(RELAY))
oc_relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oc_relay)


class Dummy:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


oc_relay._pool.clear()
fresh = Dummy()
oc_relay._pool.append((fresh, time.monotonic()))
conn, reused = oc_relay._pool_get("http", "127.0.0.1", 1, 5)
assert reused is True and conn is fresh, "recent entry must be reused"

oc_relay._pool.clear()
stale = Dummy()
oc_relay._pool.append((stale, time.monotonic() - oc_relay._POOL_TTL - 1))
conn, reused = oc_relay._pool_get("http", "127.0.0.1", 1, 5)
assert reused is False and stale.closed, "stale entry must be discarded"
assert conn is not stale
print("pool TTL: stale entries discarded, fresh reused")


# --- integration: POST after the gateway closed the idle connection ---
mock_server = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), IdleClosingMock)
# the relay legitimately discards idle pooled sockets and terminates at the
# end; the mock's handler thread then sees a reset -- don't print tracebacks
mock_server.handle_error = lambda request, client_address: None
threading.Thread(target=mock_server.serve_forever, daemon=True).start()

relay = subprocess.Popen(
    [sys.executable, str(RELAY),
     "--target", "http://127.0.0.1:%d/v1" % MOCK_PORT, "--key", "testkey",
     "--port", str(RELAY_PORT)],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    wait_port(RELAY_PORT)

    def post():
        h = http.client.HTTPConnection("127.0.0.1", RELAY_PORT, timeout=15)
        h.request("POST", "/v1/messages", body=json.dumps({"model": "x"}),
                  headers={"content-type": "application/json"})
        r = h.getresponse()
        data = r.read()
        h.close()
        return r.status, json.loads(data)

    st, body = post()
    assert st == 200 and body["path"] == "/v1/messages", (st, body)
    print("POST #1 -> 200 (upstream connection pooled)")

    time.sleep(1.0)  # gateway closed the pooled conn after 0.3s idle; this
    # is still below the relay's pool TTL, so only the fresh-socket retry
    # can save the next request (exactly the reported symptom)

    st, body = post()
    assert st == 200 and body["path"] == "/v1/messages", (st, body)
    print("POST #2 after idle close -> 200 (dead pooled conn retried fresh)")

    st, body = post()
    assert st == 200 and body["path"] == "/v1/messages", (st, body)
    print("POST #3 back-to-back -> 200")
    print("STALE POOL TEST PASS")
finally:
    relay.terminate()
    mock_server.shutdown()
