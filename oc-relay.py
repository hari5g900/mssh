#!/usr/bin/env python3
"""
oc-relay.py - Forward LLM endpoint(s) from this machine to a remote, via mssh.

Pure port forwarding with local key injection: it serves an HTTP endpoint on
127.0.0.1:<port> and forwards every request to the REAL gateway that THIS
machine can reach, injecting the API key locally. The key never leaves this
machine and the remote only ever talks to loopback.

mssh reads the endpoints to forward from a config file (endpoints.jsonc in the
repo root; see endpoints.example.jsonc), then runs one relay per endpoint with
--target/--key/--port. --endpoints prints that config as TSV for mssh.

Client-agnostic: anything that speaks OpenAI or Anthropic (/v1/messages) works
on the remote — claude, opencode, curl, etc.

Usage:
    oc-relay.py --port 18080 --target http://HOST:PORT/v1 [--key KEY]
    oc-relay.py --endpoints [PATH]        # print endpoint list as TSV, exit
"""

import argparse
import http.client
import http.server
import json
import os
import re
import socket
import sys
import threading
import urllib.parse


def _strip_jsonc(text: str) -> str:
    """Remove // comments (even with URLs like https://), /* */ blocks and
    trailing commas, without touching strings."""
    out = []
    in_str = False
    esc = False
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            depth = 1
            while i < n and depth:
                if text[i:i + 2] == "/*":
                    depth += 1
                    i += 2
                elif text[i:i + 2] == "*/":
                    depth -= 1
                    i += 2
                else:
                    i += 1
            continue
        out.append(c)
        i += 1
    text = "".join(out)
    text = re.sub(r",\s*([}\]])", r"\1", text)
    return text


def read_endpoints(config_path: str | None = None) -> list:
    """Read the endpoints to forward from the mssh config file.

    Format (opencode/codex-style JSONC):
        { "endpoints": [ { "name", "url", "apiKey"?, "port"? }, ... ], "port"? }
    Returns a list of (name, url, api_key, port). This is data-driven: no
    client is hard-coded; the user fills endpoints.jsonc (see
    endpoints.example.jsonc)."""
    p = config_path
    if not p:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "endpoints.jsonc")
    if not os.path.exists(p):
        raise SystemExit(
            "endpoints config not found: %s\n"
            "  copy endpoints.example.jsonc to endpoints.jsonc and fill it in"
            % p)
    with open(p, encoding="utf-8-sig") as f:
        # utf-8-sig transparently strips a BOM if present (Notepad/VS Code).
        data = json.loads(_strip_jsonc(f.read()))
    if not isinstance(data, dict) or not isinstance(data.get("endpoints"), list) \
            or not data["endpoints"]:
        raise SystemExit("no 'endpoints' array found in %s" % p)
    base = int(data.get("port", 18080))
    out = []
    for i, e in enumerate(data["endpoints"]):
        name = e.get("name")
        url = e.get("url") or e.get("baseURL")
        if not name or not url:
            raise SystemExit("each endpoint needs 'name' and 'url' in %s" % p)
        port = int(e.get("port", base + i))
        # The key also travels through TSV (--endpoints) on its way to the env
        # var; tabs/newlines would break that, and are never valid in a key.
        key = (str(e.get("apiKey") or "").replace("\t", "").replace("\n", ""))
        out.append((str(name), str(url).rstrip("/"), key, port))
    return out


# --- keep-alive connection pool (relay -> upstream gateway) ---
# `http.client` connections are not thread-safe, so a connection is only ever
# used by one request thread at a time (it is popped under the lock, used
# exclusively, then returned).
_pool: list = []
_pool_lock = threading.Lock()
_POOL_MAX = 16


def _pool_get(scheme: str, host: str, port: int, timeout: int):
    with _pool_lock:
        if _pool:
            return _pool.pop()
    if scheme == "https":
        return http.client.HTTPSConnection(host, port, timeout=timeout)
    return http.client.HTTPConnection(host, port, timeout=timeout)


def _pool_put(conn, reusable: bool) -> None:
    if not reusable:
        try:
            conn.close()
        except Exception:
            pass
        return
    with _pool_lock:
        if len(_pool) < _POOL_MAX:
            _pool.append(conn)
        else:
            try:
                conn.close()
            except Exception:
                pass


class Relay(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    upstream = ""
    api_key = None
    timeout = 120

    def _forward(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length > 0 else None

        parsed = urllib.parse.urlsplit(self.upstream)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)

        headers = {
            k: v
            for k, v in self.headers.items()
            if k.lower()
            not in ("host", "connection", "content-length",
                    "transfer-encoding", "authorization", "x-api-key",
                    "proxy-connection", "keep-alive", "upgrade", "expect")
        }
        if self.api_key:
            # inject the real key locally; cover both auth conventions so
            # OpenAI-style (Authorization: Bearer) and Anthropic-style
            # (x-api-key, used by claude) both authenticate against the
            # upstream gateway.
            headers["Authorization"] = "Bearer " + self.api_key
            headers["x-api-key"] = self.api_key
        headers["Host"] = host + ((":%d" % port) if parsed.port else "")
        headers["Connection"] = "keep-alive"

        # join the upstream base path with the client's requested path.
        # base='/v1' + client '/v1/messages'  ->  '/v1/messages'
        base = parsed.path.rstrip("/")
        req = self.path
        if base:
            if req == base:
                req = ""
            elif req.startswith(base + "/"):
                req = req[len(base):]
        path = base + req
        # Query strings belong to the caller; only add the upstream's own query
        # if the caller sent none (avoids a double '?').
        if "?" not in path and parsed.query:
            path += "?" + parsed.query

        # acquire a pooled connection; retry once if it went stale while idle.
        # Never retry non-idempotent writes (a retried POST could generate the
        # request twice upstream).
        idempotent = self.command in ("GET", "HEAD", "OPTIONS", "DELETE")
        attempts = 2 if idempotent else 1
        conn = _pool_get(parsed.scheme, host, port, self.timeout)
        resp = None
        for _attempt in range(attempts):
            try:
                conn.request(self.command, path, body=body, headers=headers)
                resp = conn.getresponse()
                break
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
                if _attempt == attempts - 1:
                    raise
                conn = _pool_get(parsed.scheme, host, port, self.timeout)

        # client leg always closes: responses without a content-length (SSE)
        # rely on EOF to signal the end of the stream
        self.send_response_only(resp.status, resp.reason)
        for k, v in resp.getheaders():
            if k.lower() not in (
                    "connection", "transfer-encoding", "keep-alive", "upgrade"):
                self.send_header(k, v)
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()

        reusable = not resp.will_close
        try:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except Exception:
            reusable = False
        finally:
            _pool_put(conn, reusable)

    do_GET = _forward
    do_POST = _forward
    do_PUT = _forward
    do_PATCH = _forward
    do_DELETE = _forward
    do_HEAD = _forward
    do_OPTIONS = _forward

    def log_message(self, *args):
        pass


def _probe_upstream(upstream: str) -> bool:
    """Non-fatal startup check: can we reach the real gateway host:port from
    THIS machine? This is where the VPN/tailnet requirement lives — the relay
    must reach it before the remote can."""
    try:
        p = urllib.parse.urlsplit(upstream)
        host = p.hostname
        port = p.port or (443 if p.scheme == "https" else 80)
        s = socket.create_connection((host, port), timeout=3)
        s.close()
        return True
    except OSError:
        return False


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=18080)
    ap.add_argument("--endpoints", nargs="?", const="", metavar="PATH",
                    help="print the endpoints from the config as TSV "
                         "(name <TAB> url <TAB> apiKey <TAB> port) and exit")
    ap.add_argument("--target", help="baseURL to forward to (http://host:port/v1)")
    ap.add_argument("--key", help="API key to inject locally (none = inherit header)")
    ap.add_argument("--timeout", type=int, default=120)
    args = ap.parse_args()

    if args.endpoints is not None:
        for name, url, key, port in read_endpoints(args.endpoints or None):
            print("%s\t%s\t%s\t%d" % (name, url, key, port))
        sys.exit(0)

    if args.target:
        upstream = args.target.rstrip("/")
        # --key wins; otherwise read MSSH_KEY (the mssh wrappers pass the API
        # key via env so it never appears in process listings/argv).
        api_key = args.key or os.environ.get("MSSH_KEY") or None
    else:
        raise SystemExit("no --target given (and no automated provider lookup); "
                         "use --endpoints to see the configured endpoints, or run via mssh")

    if "/v1" not in upstream.split("//")[-1].split("/")[-2:] and not upstream.endswith("/"):
        print("relay: note: upstream does not end in /v1: %s" % upstream,
              file=sys.stderr)

    if not _probe_upstream(upstream):
        host = urllib.parse.urlsplit(upstream).hostname
        print("relay: WARNING: %s is unreachable from THIS machine (VPN/tailnet down?) "
              "— bridging will fail until this machine can reach it" % host,
              file=sys.stderr)

    Relay.upstream = upstream
    Relay.api_key = api_key
    Relay.timeout = args.timeout

    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Relay)
    print("relay: listening on http://127.0.0.1:%d  (target %s)" % (args.port, upstream),
          file=sys.stderr)
    server.serve_forever()


if __name__ == "__main__":
    main()
