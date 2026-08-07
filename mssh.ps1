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
      * reverse-forwards each relay port to the remote via ssh -R (same port on
        both ends);
      * exports on the remote, per endpoint:  <NAME>_BASE_URL=http://127.0.0.1:<port>
        The FIRST endpoint also gets ANTHROPIC_BASE_URL (for claude) and
        LLM_BASE_URL (for OpenAI-compatible clients).

.PARAMETER Target
    SSH destination: an alias from ~/.ssh/config or user@host.

.PARAMETER Command
    Optional command to run on the remote (instead of an interactive shell).

.PARAMETER Config
    Path to the endpoints config file (default: endpoints.jsonc next to this
    script).

.PARAMETER ForwardAgent
    -A  Enable SSH agent forwarding (ssh -A).

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

# --- start one relay per endpoint and build the forwards + env exports ---
$n = [guid]::NewGuid().ToString('N').Substring(0, 8)
$procs = [System.Collections.ArrayList]::new()
$logs = [System.Collections.ArrayList]::new()
$fwdArgs = [System.Collections.ArrayList]::new()
$exportParts = [System.Collections.ArrayList]::new()
$ports = [System.Collections.ArrayList]::new()

function Start-OneRelay {
    param($Ep, [string]$Log)
    $argsStr = '"{0}" --target "{1}" --port {2}' -f $Relay, $Ep.Url, $Ep.Port
    if ($Ep.Key) { $argsStr += ' --key "{0}"' -f $Ep.Key }
    return (Start-Process -FilePath python -ArgumentList $argsStr `
        -RedirectStandardError $Log -WindowStyle Hidden -PassThru)
}

$first = $true
foreach ($ep in $eps) {
    $log = Join-Path $env:TEMP ("mssh-{0}-{1}.log" -f $ep.Name, $n)
    $p = Start-OneRelay $ep $log
    [void]$procs.Add($p); [void]$logs.Add($log); [void]$ports.Add($ep.Port)
    [void]$fwdArgs.Add('-R'); [void]$fwdArgs.Add("127.0.0.1:$($ep.Port):127.0.0.1:$($ep.Port)")

    $base = $ep.Url
    if ($base.EndsWith('/v1')) { $base = $base.Substring(0, $base.Length - 3) }
    $envName = ($ep.Name.ToUpper() -replace '[^A-Z0-9]', '_')
    [void]$exportParts.Add("${envName}_BASE_URL=$base")
    if ($first) {
        [void]$exportParts.Add("ANTHROPIC_BASE_URL=$base")
        [void]$exportParts.Add("LLM_BASE_URL=$base/v1")
        $first = $false
    }
}

# --- wait for every relay port to come up ---
$okPorts = @()
foreach ($port in $ports) {
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
        Write-Host "a relay for port $port failed to start" -ForegroundColor Yellow
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
$sshArgsArr = @() + $fwdArgs + $extra
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
