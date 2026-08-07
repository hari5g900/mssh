"""Latency benchmark: direct to mock gateway vs. through oc-relay (keep-alive).
Run from the repo root:
    python tests/bench_relay.py
"""

import http.client
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MOCK_SERVER = Path(__file__).resolve().parent / "mock_server.py"
RELAY = REPO / "oc-relay.py"

MOCK = 53120
RELAY_PORT = 53121


def wait_port(port, proc=None, timeout=6):
    end = time.time() + timeout
    while time.time() < end:
        if proc is not None and proc.poll() is not None:
            raise SystemExit("process died")
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            c.request("GET", "/")
            c.getresponse()
            return
        except Exception:
            time.sleep(0.05)
    raise SystemExit("port %d never up" % port)


def bench(port, n=200):
    body = json.dumps({"model": "x", "messages": [{"role": "user", "content": "hi"}]})
    lat = []
    for _ in range(n):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        t0 = time.perf_counter()
        c.request("POST", "/v1/messages", body=body,
                  headers={"content-type": "application/json"})
        r = c.getresponse()
        r.read()
        lat.append((time.perf_counter() - t0) * 1000)
        c.close()
    return lat


mock = subprocess.Popen(
    [sys.executable, str(MOCK_SERVER), str(MOCK)],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    wait_port(MOCK, mock)
    relay = subprocess.Popen(
        [sys.executable, str(RELAY),
         "--target", "http://127.0.0.1:%d/v1" % MOCK, "--key", "testkey",
         "--port", str(RELAY_PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_port(RELAY_PORT, relay)
        for name, d in (("direct   ", bench(MOCK)), ("via-relay", bench(RELAY_PORT))):
            print("%s  p50=%.2fms  p95=%.2fms  avg=%.2fms" % (
                name, statistics.median(d),
                sorted(d)[int(len(d) * 0.95)], statistics.mean(d)))
        med = statistics.median(bench(RELAY_PORT)) - statistics.median(bench(MOCK))
        print("added median latency: %.2f ms" % med)
    finally:
        relay.terminate()
finally:
    mock.terminate()
