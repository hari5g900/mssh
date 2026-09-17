<#
.SYNOPSIS
    mssh.ps1 - SSH into a machine while forwarding the LLM endpoints listed in
    a config file, so remote tools (claude, opencode, curl, ...) can use them.

.DESCRIPTION
    Behaves like `ssh` but:
      * reads the endpoints from a config file (default: endpoints.jsonc next to
        this script; copy endpoints.example.jsonc and fill it in);
      * starts one local relay (oc-relay.py) per endpoint that forwards to the
        real gateway and injects the API key locally (keys never leave this
        machine, and nothing is installed on the remote);
      * reverse-forwards each relay port to the remote via ssh -R;
      * exports on the remote, per endpoint:  <NAME>_BASE_URL=http://127.0.0.1:<port>
        The FIRST endpoint also gets ANTHROPIC_BASE_URL (for claude) and
        LLM_BASE_URL (for OpenAI-compatible clients).

    Ports: the port on the REMOTE is always the configured port for that
    endpoint (your remote agents are set up to use those fixed ports, e.g.
    18080/18081). Only the LOCAL relay port is allocated fresh per run, so any
    number of `mssh` sessions can run at the same time — many machines in
    parallel — without colliding or touching each other's ssh/sshd processes.

.PARAMETER Target
    SSH destination: an alias from ~/.ssh/config or user@host.

.PARAMETER Command
    Optional command to run on the remote (instead of an interactive shell).

.PARAMETER Config
    Path to the endpoints config file (default: endpoints.jsonc next to this
    script).

.PARAMETER ForwardAgent
    -A  Enable SSH agent forwarding (ssh -A).

.PARAMETER Steal
    Before connecting, kill this user's stale sshd sessions on the remote that
    hold the configured forward ports (oldest first, until each port frees).
    Use when an abandoned session blocks the fixed remote ports. Note: it
    cannot tell abandoned from live sessions, so it may kill an active one.

.PARAMETER SshArgs
    Extra ssh options as a single string, e.g. "-p 2222 -i ~/.ssh/id_ed25519".

.EXAMPLE
    .\mssh.ps1 mybox                # forward all configured endpoints
    .\mssh.ps1 user@host "claude"   # and run claude on the remote
#>
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Target,

    [Parameter(Position = 1, ValueFromRemainingArguments = $true)]
    [string[]]$Command = @(),

    [Alias('A')]
    [switch]$ForwardAgent,
    [switch]$Steal,
    [string]$SshArgs = "",
    [string]$Config = ""
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Relay = Join-Path $ScriptDir 'oc-relay.py'
if (-not (Test-Path $Relay)) { throw "relay not found: $Relay" }
if (-not $Config) { $Config = Join-Path $ScriptDir 'endpoints.jsonc' }

$extra = @()
foreach ($t in ($SshArgs -split '\s+')) { if ($t) { $extra += $t } }

# --- read the endpoints the user configured (TSV: name <TAB> url <TAB> key <TAB> port) ---
$eps = @()
try {
    $raw = python $Relay '--endpoints' $Config
    if ($LASTEXITCODE -ne 0 -or -not $raw) { throw "no endpoints parsed" }
    foreach ($line in $raw) {
        if (-not $line.Trim()) { continue }
        $p = $line -split "`t"
        if ($p.Count -lt 4) { continue }
        $eps += [pscustomobject]@{
            Name = $p[0]; Url = $p[1]; Key = $p[2]; Port = [int]$p[3]
        }
    }
} catch {
    Write-Host "failed to read endpoints from $Config" -ForegroundColor Yellow
    Write-Host "  copy endpoints.example.jsonc to endpoints.jsonc and fill it in." -ForegroundColor Yellow
    exit 1
}
if ($eps.Count -eq 0) { Write-Host "no endpoints in $Config" -ForegroundColor Yellow; exit 1 }

# --- optional: -Steal frees ports held by stale remote sessions ---
if ($Steal) {
    $cleanupBash = @'
user=$(id -u)
who=$(getent passwd "$user" | cut -d: -f1)
for port in "$@"; do
    hex=$(printf '%04X' "$port")
    if awk -v la="0100007F:$hex" '$2==la && $4=="0A" {f=1} END{exit !f}' /proc/net/tcp; then
        echo "port $port is held on the remote; killing stale sshd sessions (oldest first)"
        for d in $(ls -d /proc/[0-9]* 2>/dev/null); do
            pid=${d#/proc/}
            [ "$(stat -c %u "$d" 2>/dev/null)" = "$user" ] || continue
            cmdline=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null)
            cmdline=${cmdline%"${cmdline##*[![:space:]]}"}
            case "$cmdline" in
                "sshd: $who"@*pts/*|"sshd: $who"@tty[0-9]*|"sshd: $who")
                    start=$(awk '{print $22}' "$d/stat" 2>/dev/null)
                    echo "$start|$pid|$cmdline"
                    ;;
            esac
        done | sort -t'|' -k1 -n | while IFS='|' read start pid cmdline; do
            echo "killing stale remote sshd pid $pid ($cmdline)"
            kill -9 "$pid" 2>/dev/null
            sleep 1
            if ! awk -v la="0100007F:$hex" '$2==la && $4=="0A" {f=1} END{exit !f}' /proc/net/tcp; then
                echo "port $port freed"
                break
            fi
        done
    fi
done
for port in "$@"; do
    hex=$(printf '%04X' "$port")
    if awk -v la="0100007F:$hex" '$2==la && $4=="0A" {f=1} END{exit !f}' /proc/net/tcp; then
        echo "STILL-HELD $port"
    fi
done
'@
    $portsStr = ($eps.Port -join ' ')
    $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($cleanupBash -replace "`r", "")))
    $cleanupArgs = @('-o', 'RemoteCommand=none') + $extra + @($Target, "echo $b64 | base64 -d | bash -s $portsStr")
    $oldEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $cleanupOut = @(& ssh @cleanupArgs 2>&1 | ForEach-Object { "$_" })
        foreach ($line in $cleanupOut) {
            if ($line -match 'STILL-HELD') { Write-Host $line -ForegroundColor Yellow }
            elseif ($line -match 'killing stale|port .* freed') { Write-Host $line -ForegroundColor DarkYellow }
        }
    } catch { } finally {
        $ErrorActionPreference = $oldEap
    }
}

# --- port allocation ---
# The REMOTE forward port is FIXED to the configured port for each endpoint:
# the agents installed on the remote point at those specific ports. Only the
# LOCAL relay port is picked fresh per session, so parallel mssh sessions
# (different machines, or several on this machine) never collide on loopback.
# Existing ssh/sshd/relay processes are never searched for or killed — an
# abandoned session simply leaves its local relay up until the connection
# times out, and the next session picks another free local port.
#
# If the fixed remote port is already held (e.g. another session to the same
# box has it), ssh prints a "remote port forwarding failed" warning and that
# one forward is skipped — nothing is killed.

function Get-FreeLocalPort {
    param([int]$Start)
    for ($p = $Start; $p -lt ($Start + 2000); $p++) {
        try {
            $l = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $p)
            $l.Start()
            $l.Stop()
            return [int]$p
        } catch { }
    }
    throw "no free loopback port found starting at $Start"
}

# Per-session offset so two concurrent runs don't start scanning from the same
# number (the remote side still uses the fixed configured ports).
$salt = Get-Random -Minimum 0 -Maximum 1500

# --- start one relay per endpoint and build the forwards + env exports ---
$n = [guid]::NewGuid().ToString('N').Substring(0, 8)
$procs = [System.Collections.ArrayList]::new()
$logs = [System.Collections.ArrayList]::new()
$fwdArgs = [System.Collections.ArrayList]::new()
$exportParts = [System.Collections.ArrayList]::new()
$localPorts = [System.Collections.ArrayList]::new()

function Start-OneRelay {
    param($Ep, [int]$Port, [string]$Log)
    # Values are wrapped in double quotes for the Windows command line; escape
    # any embedded quote so config data can't break out of its argument.
    $u = $Ep.Url.Replace('"', '\"')
    $argsStr = '"{0}" --target "{1}" --port {2}' -f $Relay, $u, $Port
    # Pass the API key via the environment, not the command line (no --key):
    # it never shows up in process listings and can't inject extra args.
    $oldKey = $env:MSSH_KEY
    try {
        if ($Ep.Key) { $env:MSSH_KEY = $Ep.Key }
        return (Start-Process -FilePath python -ArgumentList $argsStr `
            -RedirectStandardError $Log -WindowStyle Hidden -PassThru)
    } finally {
        $env:MSSH_KEY = $oldKey
    }
}

try {
    $first = $true
    for ($i = 0; $i -lt $eps.Count; $i++) {
        $ep = $eps[$i]
        # Fixed on the remote (agents there point at these ports); only the
        # local relay port is chosen fresh for this session.
        $remotePort = [int]$ep.Port
        $localPort = Get-FreeLocalPort ($remotePort + $salt)
        [void]$localPorts.Add($localPort)

        $log = Join-Path $env:TEMP ("mssh-{0}-{1}.log" -f $ep.Name, $n)
        $p = Start-OneRelay $ep $localPort $log
        [void]$procs.Add($p); [void]$logs.Add($log)
        [void]$fwdArgs.Add('-R')
        [void]$fwdArgs.Add("127.0.0.1:${remotePort}:127.0.0.1:${localPort}")

        # The remote talks to the forwarded port (remotePort), never the
        # upstream URL.
        $envName = ($ep.Name.ToUpper() -replace '[^A-Z0-9]', '_')
        # single-quote values: they're fixed-form and config-derived
        [void]$exportParts.Add("${envName}_BASE_URL='http://127.0.0.1:${remotePort}'")
        if ($first) {
            [void]$exportParts.Add("ANTHROPIC_BASE_URL='http://127.0.0.1:${remotePort}'")
            [void]$exportParts.Add("LLM_BASE_URL='http://127.0.0.1:${remotePort}/v1'")
            $first = $false
        }
    }
} catch {
    Write-Host "failed to allocate ports / start relays: $($_.Exception.Message)" -ForegroundColor Yellow
    foreach ($p in $procs) {
        if ($p -and -not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
    }
    exit 1
}

# --- wait for every relay port to come up (the LOCAL listener) ---
$okPorts = @()
foreach ($port in $localPorts) {
    $ready = $false
    for ($i = 0; $i -lt 60; $i++) {
        try {
            $c = New-Object System.Net.Sockets.TcpClient
            $a = $c.BeginConnect('127.0.0.1', $port, $null, $null)
            if ($a.AsyncWaitHandle.WaitOne(150) -and $c.Connected) { $ready = $true }
            $c.Close()
        } catch { }
        if ($ready) { break }
        Start-Sleep -Milliseconds 150
    }
    if (-not $ready) {
        Write-Host "a relay for local port $port failed to start" -ForegroundColor Yellow
    } else {
        $okPorts += $port
    }
}
if ($okPorts.Count -eq 0) {
    Write-Host "all relays failed to start; logs:" -ForegroundColor Yellow
    foreach ($log in $logs) { Get-Content $log -ErrorAction SilentlyContinue }
    exit 1
}

$exportStr = "export " + ($exportParts -join ' ')
$sshArgsArr = @('-o', 'RemoteCommand=none') + $fwdArgs + $extra
if ($ForwardAgent) { $sshArgsArr += '-A' }

try {
    if ($Command.Count -eq 0) {
        $remoteCmd = "$exportStr; exec `$SHELL -il"
        $sshArgsArr += @('-t', $Target, $remoteCmd)
        ssh @sshArgsArr
    } else {
        $cmd = ($Command -join ' ')
        $remoteCmd = "$exportStr; $cmd"
        $sshArgsArr += @($Target, $remoteCmd)
        ssh @sshArgsArr
    }
}
finally {
    foreach ($p in $procs) {
        if ($p -and -not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
    }
}
