# mssh — forward your LLM endpoints to any machine over SSH

`mssh` is a drop-in replacement for `ssh` that also **reverse-forwards the LLM
endpoint(s) you list in a config file**, so tools on the remote box (claude,
opencode, curl — anything that speaks OpenAI or Anthropic) can use the models
*this* machine can reach.

- **Configurable, not client-specific:** you point it at your endpoints; it does
  pure port forwarding. No opencode/claude knowledge is baked in (in this repo's
  own setup the config happens to be an opencode-style file).
- **Keys stay local:** each endpoint's API key is injected on this machine; the
  remote never sees it and **nothing is installed on the remote** — just sshd and
  whatever tools you already use there.
- **Works anywhere SSH does:** air-gapped hosts, shared boxes with no install
  permissions, hosts with no VPN/tailscale client.

---

## How it works

```
THIS machine (owns the keys, can reach the gateways — e.g. via VPN/tailnet)
  ├─ oc-relay (endpoint 1) :18080 ─┐
  ├─ oc-relay (endpoint 2) :18081 ─┤── ssh -R ... ──►  REMOTE
  │                                │                   ├─ <NAME>_BASE_URL (per endpoint)
  └─ keys never leave here  ◄──────┘                   ├─ ANTHROPIC_BASE_URL  (first)
                                                       └─ LLM_BASE_URL        (first)
```

For each endpoint in the config, `mssh`:

1. starts a **local relay** (`oc-relay.py`) that forwards requests to the real
   gateway and injects the API key locally,
2. **reverse-forwards** it to the remote (`ssh -R`): the **remote** side stays
   on the configured port for that endpoint (agents installed on the remote
   point at those specific ports), while the **local** relay port is picked
   fresh per session so parallel sessions never collide on this machine,
3. exports on the remote:
   - `<NAME>_BASE_URL=http://127.0.0.1:<port>` for **every** endpoint,
   - plus `ANTHROPIC_BASE_URL` and `LLM_BASE_URL` for the **first** endpoint
     (convenience aliases for claude and OpenAI-compatible clients),
4. starts your remote shell (or runs your command), then cleans everything up
   when you disconnect.

---

## The config file

Copy `endpoints.example.jsonc` to `endpoints.jsonc` **in the repo root** and
fill it in. `mssh` re-reads it on **every run** — edit the file, no reinstall.

```jsonc
{
  "endpoints": [
    // name:   becomes <NAME>_BASE_URL on the remote
    // url:    base URL of the server THIS machine can reach (http/https, /v1-style)
    // apiKey: optional; injected locally, never sent to the remote
    // port:   the port exposed on the REMOTE (default 18080, 18081, ...).
    //         Keep this fixed — agents installed on the remote point at these
    //         ports. Only the local relay port is re-allocated per session.
    { "name": "deepseek", "url": "http://<your-server-1>:30800/v1", "apiKey": "", "port": 18080 },
    { "name": "qwen",     "url": "http://<your-server-2>:5007/v1", "port": 18081 }
  ]
}
```

- `endpoints.jsonc` is **gitignored** — don't commit your real `apiKey`s (this
  repo is public; git history is forever).
- Any JSON/JSONC file with an `endpoints` array works — the schema is data, not
  a product. (An opencode/codex-style config is just another JSONC file that
  happens to describe the same endpoints.)
- If you don't have `endpoints.jsonc` yet, `oc-relay.py` explains exactly what
  to do when `mssh` runs.

---

## Files

| File | Purpose |
|---|---|
| `oc-relay.py` | Relay: forwards to a real endpoint, injects the key locally, streams responses, pools upstream connections, and reads `--endpoints` as TSV for mssh. |
| `mssh.ps1` | PowerShell wrapper (Windows). |
| `mssh` | bash wrapper (macOS / Linux / WSL). |
| `install.sh` | Installer (macOS / Linux): copies `mssh` + `oc-relay.py` (and the endpoint template) into a bin dir. |
| `endpoints.example.jsonc` | Template you copy to `endpoints.jsonc` and fill in. |

---

## Prerequisites

- **Local machine (the one with the keys):** Python 3.10+ (for the relay),
  OpenSSH client, and network access to your gateways (e.g. FortiClient VPN,
  Tailscale, LAN).
- **Remote:** only what you already use to connect over SSH — `sshd` and your
  own tools (claude, opencode, ...). Nothing is installed by `mssh`.
  *(If the gateways are behind a VPN/tailnet, only this machine needs to be on
  it — the remote never does.)*

## Quick start

```bash
# 1) configure the endpoints
cp endpoints.example.jsonc endpoints.jsonc   # then edit endpoints.jsonc

# 2) connect
mssh mybox                  # forward all endpoints, interactive shell
mssh mybox "claude"         # and run claude on the remote
mssh -A mybox               # also forward your ssh-agent
```

On the remote, your tools now see localhost endpoints that act like your real
servers:

```bash
claude                      # uses $ANTHROPIC_BASE_URL (first endpoint)
opencode                    # point it at $LLM_BASE_URL / <NAME>_BASE_URL
curl -s http://127.0.0.1:18080/v1/messages   # sanity-check a forward
```

**Windows PowerShell:** add to `$PROFILE`:
```powershell
function mssh { & "C:\path\to\mssh\mssh.ps1" @args }
```
(or use the `install.sh` on macOS/Linux/WSL).

---

## Options

### PowerShell (`mssh.ps1`)

| Option | Meaning |
|---|---|
| `-Target` | ssh destination (positional) |
| `-Command` | command to run instead of an interactive shell |
| `-Config <path>` | endpoints config file (default: `endpoints.jsonc` beside the script) |
| `-A` / `-ForwardAgent` | enable SSH agent forwarding (`ssh -A`) |
| `-Steal` | before connecting, kill this user's stale sshd sessions on the remote that hold the configured forward ports (oldest first). Use when an abandoned session blocks the ports; it cannot tell abandoned from live sessions. |
| `-SshArgs "<opts>"` | extra ssh options, e.g. `"-p 2222 -i <key>"` |

### bash (`mssh`)

| Env var | Meaning |
|---|---|
| `MSSH_CONFIG` | endpoints config path (default: `endpoints.jsonc` beside the script) |

Extra ssh flags pass through directly after options: `mssh -A -p 2222 user@host`
(`-A` forwards your ssh-agent, same as `ssh -A`). Ports come from the config
file (defaults `18080, 18081, ...` per endpoint).

### Relay (`oc-relay.py`)

```bash
python oc-relay.py --endpoints [PATH]       # print the configured endpoints (TSV)
python oc-relay.py --target http://host:PORT/v1 --key KEY --port 18080   # one endpoint
```

---

## How the relay works

- **Config-driven:** reads the endpoint list from `endpoints.jsonc` (JSON/JSONC,
  comment-safe parsing) — no hard-coded clients.
- **Key injection:** strips whatever auth the caller sent and injects the real
  key as **both** `Authorization: Bearer` and `x-api-key`, so OpenAI-style and
  Anthropic-style (`claude`) callers both authenticate.
- **Path routing:** joins the endpoint base path with the client path without
  duplicating `/v1` (`/v1` + `/v1/messages` → `/v1/messages`).
- **Streaming:** responses relay byte-by-byte with the client leg closed at the
  end — SSE token streaming works in near real time.
- **Keep-alive pooling:** upstream connections reused (bounded pool, stale
  connections retried once) — essentially zero added latency per request.
- **Loopback only:** relays bind to `127.0.0.1`; nothing is exposed on the LAN.

---

## Performance

Measured on loopback against a mock gateway:

| Metric | Value |
|---|---|
| Added latency per request | **~0 ms** (parity with a direct connection) |
| 16 concurrent streaming `/v1/messages` | 16/16 OK, wall time flat |
| 32 concurrent streams | 32/32 OK |
| Relay footprint | ~30–50 MB RAM per relay, a few % of one core |

Each remote `mssh` run gets its own relays, so load is naturally sharded across
machines.

**Parallel sessions are fully supported.** The remote forward port stays on the
fixed configured port (agents on the remote keep pointing at it), while the
local relay port is re-allocated per session, so you can `mssh` many machines
at once from this box — no session disturbs another, and `mssh` never kills an
existing ssh/sshd session. (Two sessions against the *same* box share the same
fixed remote ports, so only one holds each forward; ssh prints a harmless
"remote port forwarding failed" warning for the second.)

---

## Security

- **API keys never leave this machine** — the relay injects them locally; the
  remote only ever sees `127.0.0.1` endpoints with no credentials required.
- **Nothing is installed on the remote** — only SSH port forwarding and shell
  variables.
- Relays listen on **loopback only** and are torn down when the SSH session
  ends.
- Forwarded ports on the remote bind to `127.0.0.1` (no `GatewayPorts`
  needed); other users on the remote could attempt to reach them while your
  session is up — the injected key is the real protection.
- **Don't commit real `apiKey`s** — `endpoints.jsonc` is gitignored.

---

## Troubleshooting

- **`endpoints config not found`** — copy `endpoints.example.jsonc` →
  `endpoints.jsonc` and fill it in (works for both `mssh` and the relay).
- **`relay: WARNING: <host> is unreachable from THIS machine`** — the gateway
  is not reachable from *this* machine right now (VPN/Tailscale down, wrong
  network). The remote can never fix this; reconnect this machine first.
- **`claude` on the remote can't reach the endpoint** — confirm the tunnel:
  `curl -s http://127.0.0.1:<port>/v1/messages`.
- **"remote port forwarding failed" on start** — the fixed remote port is
  already held on that box (e.g. an abandoned session that never timed out).
  Nothing is killed by default; that forward is skipped. Re-run with
  `mssh -Steal <target>` to kill the stale remote sessions holding the ports
  (oldest first) and take them over.
- **Auth rejected at the gateway** — `oc-relay.py` injects both `Authorization`
  and `x-api-key`; if your gateway expects another header, adjust
  `oc-relay.py`'s `_forward()`.

---

## Development / testing

Self-contained tests (Python 3.10+, no deps). From the repo root:

```bash
python tests/test_relay.py         # auth injection, wrong-key override, path routing, SSE
python tests/bench_relay.py        # latency: direct vs. through the relay (keep-alive)
python tests/test_concurrency.py   # 4/16/32 concurrent streaming streams
```

`tests/mock_server.py` is a tiny fake gateway; everything runs on loopback.

---

## License

MIT — use it, change it, throw it away.
