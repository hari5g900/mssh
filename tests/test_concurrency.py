"""Concurrency test: N simultaneous streaming /v1/messages through oc-relay.
Run from the repo root:
    python tests/test_concurrency.py
Expects all streams to complete with all events at 4/16/32 concurrent.
"""

import http.client
import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RELAY = REPO / "oc-relay.py"

MOCK = 53130
RELAY_PORT = 53131


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        self.rfile.read(n)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        for i in range(40):
            self.wfile.write(("data: {\"n\":%d}\n\n" % i).encode())
            self.wfile.flush()
            time.sleep(0.05)
        return

    def log_message(self, *a):
        pass


def wait_port(port, proc=None, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        if proc is not None and proc.poll() is not None:
            raise SystemExit("proc died")
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            c.request("GET", "/")
            c.getresponse()
            return
        except Exception:
            time.sleep(0.05)
    raise SystemExit("port not up")


def one_stream(idx, results):
    t0 = time.perf_counter()
    try:
        c = http.client.HTTPConnection("127.0.0.1", RELAY_PORT, timeout=60)
        c.request("POST", "/v1/messages", body=json.dumps({"m": idx}),
                  headers={"content-type": "application/json"})
        r = c.getresponse()
        data = b""
        while True:
            b = r.read()
            if not b:
                break
            data += b
        results[idx] = (data.count(b"data:"), time.perf_counter() - t0)
        c.close()
    except Exception as e:
        results[idx] = ("ERR:" + str(e), time.perf_counter() - t0)


server = ThreadingHTTPServer(("127.0.0.1", MOCK), H)
threading.Thread(target=server.serve_forever, daemon=True).start()

relay = subprocess.Popen(
    [sys.executable, str(RELAY),
     "--target", "http://127.0.0.1:%d/v1" % MOCK, "--key", "testkey",
     "--port", str(RELAY_PORT)],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    wait_port(MOCK, None)
    wait_port(RELAY_PORT, None)

    for concurrency in (4, 16, 32):
        results = {}
        threads = [threading.Thread(target=one_stream, args=(i, results))
                   for i in range(concurrency)]
        t0 = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        wall = time.perf_counter() - t0
        ok = sum(1 for v in results.values() if v[0] == 40)
        errs = [str(v[0]) for v in results.values() if v[0] != 40]
        print("concurrency=%2d  wall=%.2fs  streams_complete_ok=%d/%d  errs=%s" % (
            concurrency, wall, ok, concurrency, errs[:3]))
        assert ok == concurrency and not errs, "failure at concurrency=%d" % concurrency
    print("CONCURRENCY TEST PASS")
finally:
    server.shutdown()
    relay.terminate()
