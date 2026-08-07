#!/usr/bin/env python3
"""
oc-relay.py - Local relay that bridges THIS opencode session's model endpoint.

Auto-detects the active provider's baseURL (and API key) from the opencode
config, then serves an OpenAI-compatible endpoint on 127.0.0.1:<port> that
forwards every request to the real gateway, injecting the Authorization header
from the local config. The API key therefore never leaves this machine.

Used by mssh so a remote host can reach the same model this opencode session
uses, via an SSH reverse tunnel.

Usage:
    oc-relay.py [--port 18080] [--target http://HOST:PORT/v1] [--key KEY]
                [--model deepseek2]
"""

import argparse
import http.client
import http.server
import json
import os
import re
import sys
import threading
import urllib.parse

_CONFIG_CANDIDATES = (
    ".config/opencode/opencode.jsonc",
    ".config/opencode/opencode.json",
)


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


def _home() -> str:
    for env in ("USERPROFILE", "HOME"):
        v = os.environ.get(env)
        if v:
            return v
    return os.path.expanduser("~")


def _find_config() -> str | None:
    home = _home()
    for rel in _CONFIG_CANDIDATES:
        p = os.path.join(home, rel.replace("/", os.sep))
        if os.path.exists(p):
            return p
    return None


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


def detect(model_id: str | None) -> dict:
    """Return dict(provider, model, base_url, api_key) from opencode config."""
    cfg_path = _find_config()
    if not cfg_path:
        raise SystemExit("No opencode config found under ~/.config/opencode")
    with open(cfg_path, encoding="utf-8") as f:
        data = json.loads(_strip_jsonc(f.read()))

    providers = data.get("provider", {})
    if not providers:
        raise SystemExit("No providers configured in opencode config")

    if model_id:
        provider = model_id.split("/")[0]
    else:
        active = data.get("model", "")
        if "/" not in active:
            raise SystemExit("No active 'model' in config (and no --model given)")
        provider = active.split("/")[0]

    p = providers.get(provider)
    if not p:
        raise SystemExit("Provider '%s' not found in config (have: %s)"
                         % (provider, ", ".join(providers)))
    opts = p.get("options", {})
    base = opts.get("baseURL") or opts.get("baseUrl")
    if not base:
        raise SystemExit("Provider '%s' has no baseURL in options" % provider)
    return {
        "provider": provider,
        "base_url": base.rstrip("/"),
        "api_key": opts.get("apiKey"),
    }


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
        if base and (req == base or req.startswith(base + "/")):
            req = req[len(base):] or "/"
        path = base + req + (("?" + parsed.query) if parsed.query else "")

        # acquire a pooled connection; retry once if it went stale while idle
        conn = _pool_get(parsed.scheme, host, port, self.timeout)
        resp = None
        for _attempt in range(2):
            try:
                conn.request(self.command, path, body=body, headers=headers)
                resp = conn.getresponse()
                break
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
                if _attempt == 1:
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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=18080)
    ap.add_argument("--target", help="override baseURL (http://host:port/v1)")
    ap.add_argument("--key", help="override API key (none = inherit header)")
    ap.add_argument("--model", help="provider id (default: active model)")
    ap.add_argument("--timeout", type=int, default=120)
    args = ap.parse_args()

    if args.target:
        upstream = args.target.rstrip("/")
        api_key = args.key
    else:
        cfg = detect(args.model)
        upstream = cfg["base_url"]
        api_key = cfg["api_key"]
        print("relay: bridging %s -> %s" % (cfg["provider"], upstream),
              file=sys.stderr)

    if "/v1" not in upstream.split("//")[-1].split("/")[-2:] and not upstream.endswith("/"):
        print("relay: note: upstream does not end in /v1: %s" % upstream,
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
