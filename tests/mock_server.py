"""Tiny mock gateway used by the tests: requires an API key and can stream."""

import http.server
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

EXPECT = "testkey"


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _auth_ok(self):
        if self.headers.get("Authorization") == "Bearer " + EXPECT:
            return True
        if self.headers.get("x-api-key") == EXPECT:
            return True
        return False

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(n).decode("utf-8", "replace")
        ok = self._auth_ok()
        if self.path.endswith("/stream"):
            self.send_response(200 if ok else 401)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            for i in range(5):
                self.wfile.write(("data: {\"tok\":%d}\n\n" % i).encode())
                self.wfile.flush()
            return
        payload = json.dumps({
            "path": self.path, "authorized": ok,
            "auth_seen": self.headers.get("Authorization", ""),
            "xkey_seen": self.headers.get("x-api-key", ""),
            "body": body,
        }).encode()
        self.send_response(200 if ok else 401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1])
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
