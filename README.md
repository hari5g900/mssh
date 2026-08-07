# mssh — SSH that bridges your opencode model endpoints

`mssh` is a drop-in replacement for `ssh` that also bridges **this opencode
session's model endpoints** to whatever machine you SSH into, so remote tools
(claude shell, opencode) use the same models — while the API keys stay on your
machine and **nothing needs to be installed on the remote**.

Works with remote machines you can only reach over SSH (air-gapped hosts,
shared boxes with no package install permission, no VPN/tailscale client).

---

## What it does

For each SSH session, `mssh`:

1. Reads the **active provider** (and its `baseURL` + `apiKey`) from your local
   `~/.config/opencode/opencode.jsonc` (or `.json`), plus the `vllm`/qwen
   provider used by subagents.
2. Starts a tiny **local relay** (`oc-relay.py`) per endpoint that forwards
   traffic to the real gateway and **injects the API key locally** — it never
   travels to the remote.
3. **Reverse-forwards** each relay port to the remote via `ssh -R`.
4. Exports on the remote:
   - `ANTHROPIC_BASE_URL` → deepseek gateway (so `claude` just works)
   - `LLM_BASE_URL` → deepseek OpenAI endpoint (for opencode)
   - `QWEN_BASE_URL` → qwen vLLM endpoint
5. Starts your remote shell (or runs your command), then cleans everything up
   when you disconnect.

```
THIS machine (has the keys, reaches the gateways)
  ├─ oc-relay (deepseek) :18080 ─┐
  ├─ oc-relay (qwen)     :18081 ─┤── ssh -R ... ──►  REMOTE
  │                              │                   ├─ claude  → ANTHROPIC_BASE_URL
  │                              │                   └─ opencode→ LLM_BASE_URL / QWEN_BASE_URL
  └─ API keys stay here  ◄───────┘                    (nothing installed there)
```

---

## Files

| File | Purpose |
|---|---|
| `oc-relay.py` | Local HTTP relay: detects the provider from the opencode config, forwards to the gateway, injects the key, streams responses, keeps upstream connections pooled. |
| `mssh.ps1` | PowerShell wrapper (Windows). |
| `mssh` | bash wrapper (WSL / Linux). |

---

## Prerequisites

- **Local machine:** Python 3.10+ (for the relay), OpenSSH client (built into
  Windows 10/11, or installed on Linux/WSL), and `opencode` with the provider(s)
  configured in `~/.config/opencode/opencode.jsonc`.
- **Remote:** only what you already use to connect over SSH — just `sshd` and
  your own tools (e.g. `claude`). Nothing is installed by `mssh`.
  *(The endpoints the relay targets must be reachable from this machine, which
  is already true since opencode uses them.)*

## Quick start

```bash
# Put the repo on your PATH (copy mssh.ps1 + mssh + oc-relay.py into a bin dir)

# PowerShell: add to your $PROFILE so `mssh` is a normal command
function mssh { & "C:\path\to\mssh\mssh.ps1" @args }

# WSL / Linux: install the bash wrapper AND the relay into the same directory
install -m 755 mssh oc-relay.py ~/.local/bin/
```

Ensure the remote is reachable via an alias or `user@host`:

```
Host mybox
    HostName mybox.example.com
    User me
```

Connect:

```bash
mssh mybox                  # interactive remote shell, models bridged
mssh mybox "claude"         # run claude on the remote → uses your deepseek model
mssh -p 2222 user@host      # extra ssh options pass through
mssh mybox "curl -s http://127.0.0.1:18080/v1/models"   # check the tunnel
```

On the remote you can now use, for example:

```bash
claude                      # respects ANTHROPIC_BASE_URL (already exported)
opencode                    # respects LLM_BASE_URL / QWEN_BASE_URL
```

---

## Configuration

### PowerShell (`mssh.ps1`)

| Option | Default | Meaning |
|---|---|---|
| `-Target` | — | ssh destination (positional) |
| `-Command` | — | command to run instead of an interactive shell |
| `-LocalPort` | `18080` | deepseek relay listen port (local) |
| `-RemotePort` | `18080` | deepseek port exposed on the remote |
| `-QwenLocalPort` | `18081` | qwen relay listen port (local) |
| `-QwenRemotePort` | `18081` | qwen port exposed on the remote |
| `-NoQwen` | off | skip the qwen bridge |
| `-SshArgs` | `""` | extra ssh options (`"-p 2222 -i <key>"`) |

### bash (`mssh`)

| Env var | Default | Meaning |
|---|---|---|
| `MSSH_LOCAL_PORT` | `18080` | deepseek relay listen port |
| `MSSH_REMOTE_PORT` | `18080` | deepseek port on the remote |
| `MSSH_QWEN_LOCAL_PORT` | `18081` | qwen relay listen port |
| `MSSH_QWEN_REMOTE_PORT` | `18081` | qwen port on the remote |
| `MSSH_NO_QWEN` | `0` | set to `1` to skip the qwen bridge |

### Relay (`oc-relay.py`)

Usually run via `mssh`, but usable standalone:

```bash
python oc-relay.py --port 18080                    # auto-detect active provider
python oc-relay.py --model vllm --port 18081       # pick a provider by id
python oc-relay.py --target http://host:port/v1 --key KEY --port 18080  # override
```

---

## How the relay works

- **Auto-detection:** parses `opencode.jsonc` (URL-safe comment stripping),
  finds the active `model`, looks up its provider, and reads `options.baseURL`
  and `options.apiKey`.
- **Key injection:** strips any incoming `Authorization` / `x-api-key` and
  injects the real key as **both** `Authorization: Bearer` and `x-api-key`,
  so OpenAI-style callers *and* Anthropic-style callers (`claude`) both
  authenticate against the gateway.
- **Path routing:** joins the upstream base path with the client path without
  duplicating `/v1` (base `/v1` + `/v1/messages` → `/v1/messages`).
- **Streaming:** responses are relayed byte-by-byte (64 KB chunks) with the
  client leg closed at the end — SSE token streaming works in near real time.
- **Keep-alive pooling:** upstream connections are reused across requests
  (bounded pool, lock-protected, stale connections retried once), removing the
  per-request TCP handshake (~15 ms added latency → ~0).
- **Loopback only:** relays bind to `127.0.0.1`; nothing is exposed to the LAN.

### Which endpoints get bridged

- The **active model's** provider (e.g. `deepseek2/deepseek-v4-flash` →
  `http://<internal-gateway-ip>:<port>/v1`).
- The **`vllm`** provider used by subagents / `small_model`
  (`http://<tailscale-ip>:5007/v1`). Configured per your `opencode.jsonc`.
- To change which providers are bridged, edit the config or override with
  `--target`/`MSSH_NO_QWEN`.

---

## Performance

Measured on loopback against a mock gateway:

| Metric | Value |
|---|---|
| Added latency per request | **~0 ms** (parity with a direct connection) |
| 16 concurrent streaming `/v1/messages` (opencode `ultracode` cap) | 16/16 OK, wall time flat |
| 32 concurrent streams | 32/32 OK |
| Relay footprint | ~30–50 MB RAM, a few % of one core |

Streaming is I/O-bound, not CPU-bound, so the relay is *not* the bottleneck for
concurrent agent workloads. With `mssh`, each remote also gets its own relay
instance, so load is naturally sharded across machines.

---

## Security notes

- **API keys never leave this machine** — the relay injects them locally; the
  remote only ever sees `127.0.0.1` endpoints with no credentials required.
- **Nothing is installed on the remote** — only SSH port forwarding and shell
  environment variables are used.
- Relays listen on **loopback only** and are torn down when the SSH session
  ends.
- Forwarded ports on the remote bind to `127.0.0.1` (no `GatewayPorts`
  required); other users on the remote could attempt to reach them — the
  injected key is the real protection, and relay ports are only up for the
  duration of your session.
- Do **not** commit your `opencode.jsonc` credentials to this repo; the relay
  reads the key from your user config at runtime.

---

## Troubleshooting

- **"relay did not start" / "relay failed to start"** — check the printed log
  path (e.g. `%TEMP%\mssh-deepseek-*.log`, `/tmp/mssh-*.log`). Common causes:
  no active `model` in config, provider id missing, or port already in use.
- **`claude` on the remote says it can't reach the endpoint** — confirm the
  tunnel: `curl -s http://127.0.0.1:<RemotePort>/v1/models`.
- **Port conflict on the remote** — bump `-RemotePort`/`MSSH_REMOTE_PORT`
  (and the qwen equivalents) to free ports.
- **qwen bridge not wanted or unreachable** — `-NoQwen` /
  `MSSH_NO_QWEN=1`.
- **Gateway rejects auth** — the relay injects both `Authorization` and
  `x-api-key`; if your gateway expects a different header name/scheme, adjust
  `oc-relay.py`'s `_forward()`.

---

## Development / testing

The repo ships a self-contained test harness (Python 3.10+, no dependencies).
From the repo root:

```bash
python tests/test_relay.py         # auth injection, wrong-key override, path routing, SSE
python tests/bench_relay.py        # latency: direct vs. through the relay (keep-alive)
python tests/test_concurrency.py   # 4/16/32 concurrent streaming streams
```

- `tests/mock_server.py` is a tiny fake gateway requiring an API key, used by
  the other tests.
- Everything runs on loopback; nothing is touched outside the repo.

---

## License

MIT — use it, change it, throw it away.
