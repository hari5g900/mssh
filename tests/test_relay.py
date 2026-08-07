"""Functional test for oc-relay.py: auth injection, wrong-key override,
path routing, and SSE streaming. Run from the repo root:
    python tests/test_relay.py
Expects: no-auth and wrong-key /v1/messages later get 200 upstream (key injected
locally), and a /v1/messages/stream passes all 5 SSE events through."""

import http.client
import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MOCK_SERVER = Path(__file__).resolve().parent / "mock_server.py"
RELAY = REPO / "oc-relay.py"

MOCK = 53110
RELAY_PORT = 53111


def wait_port(port, proc=None, timeout=6.0):
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
    raise SystemExit("port %d never came up" % port)


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

        def post(path, body, headers=None):
            h = http.client.HTTPConnection("127.0.0.1", RELAY_PORT, timeout=15)
            h.request("POST", path, body=json.dumps(body), headers=headers or {})
            r = h.getresponse()
            return r.status, r.read().decode("utf-8", "replace")

        st, data = post("/v1/messages", {"model": "x", "messages": []})
        assert st == 200, "expected 200 with local key injection, got %d" % st
        seen = json.loads(data)
        assert seen["authorized"] is True, seen
        assert seen["path"] == "/v1/messages", seen
        assert seen["xkey_seen"] == "testkey" or "Bearer testkey" in seen["auth_seen"]
        print("no-auth /v1/messages -> 200, key injected, path OK")

        st, _ = post("/v1/messages", {"model": "x", "messages": []},
                     {"Authorization": "Bearer wrong", "x-api-key": "wrong"})
        assert st == 200, "expected relay to override wrong key, got %d" % st
        print("wrong-key /v1/messages -> 200 (overridden)")

        conn = http.client.HTTPConnection("127.0.0.1", RELAY_PORT, timeout=15)
        conn.request("POST", "/v1/messages/stream", body=json.dumps({"model": "x"}),
                     headers={"content-type": "application/json"})
        r = conn.getresponse()
        chunk = ""
        while True:
            b = r.read(64)
            if not b:
                break
            chunk += b.decode("utf-8", "replace")
        assert chunk.count("data:") == 5, "expected 5 SSE events"
        conn.close()
        print("SSE streaming -> 5 events through")

        print("RELAY TEST PASS")
    finally:
        relay.terminate()
finally:
    mock.terminate()
