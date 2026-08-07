<#
.SYNOPSIS
    mssh.ps1 - SSH into a machine while bridging THIS opencode session's model
    endpoints to it, so remote tools (claude shell, opencode) use the same models.

.DESCRIPTION
    Behaves like `ssh` but:
      * launches local relays (oc-relay.py) that forward to the active provider
        (deepseek2) and, unless -NoQwen, the qwen vLLM provider, injecting their
        API keys locally (keys never leave this machine);
      * reverse-forwards those relay ports to the remote via SSH;
      * exports on the remote:
          ANTHROPIC_BASE_URL = http://127.0.0.1:<RemotePort>        (claude shell / deepseek)
          LLM_BASE_URL       = http://127.0.0.1:<RemotePort>/v1     (opencode deepseek)
          QWEN_BASE_URL      = http://127.0.0.1:<QwenRemotePort>/v1 (qwen vLLM)

.PARAMETER Target
    SSH destination: an alias from ~/.ssh/config or user@host.

.PARAMETER Command
    Optional command to run on the remote (instead of an interactive shell).

.PARAMETER LocalPort
    DeepSeek relay local listen port (default 18080).

.PARAMETER RemotePort
    DeepSeek port exposed on the remote (default 18080).

.PARAMETER QwenLocalPort
    Qwen vLLM relay local listen port (default 18081).

.PARAMETER QwenRemotePort
    Qwen port exposed on the remote (default 18081).

.PARAMETER NoQwen
    Skip the qwen vLLM bridge.

.PARAMETER SshArgs
    Extra ssh options as a single string, e.g. "-p 2222 -i ~/.ssh/id_ed25519".

.EXAMPLE
    .\mssh.ps1 mybox
    # interactive remote shell with deepseek (claude) + qwen bridged

.EXAMPLE
    .\mssh.ps1 user@host -SshArgs "-p 2222" "claude"
#>
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Target,

    [Parameter(Position = 1, ValueFromRemainingArguments = $true)]
    [string[]]$Command = @(),

    [int]$LocalPort  = 18080,
    [int]$RemotePort = 18080,

    [int]$QwenLocalPort  = 18081,
    [int]$QwenRemotePort = 18081,

    [switch]$NoQwen,
    [string]$SshArgs = ""
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Relay = Join-Path $ScriptDir 'oc-relay.py'
if (-not (Test-Path $Relay)) { throw "relay not found: $Relay" }

$extra = @()
foreach ($t in ($SshArgs -split '\s+')) { if ($t) { $extra += $t } }

function Start-MsshRelay {
    param([string]$Model, [int]$Port, [string]$Log)
    $argsStr = '"{0}" --port {1}' -f $Relay, $Port
    if ($Model) { $argsStr += ' --model {0}' -f $Model }
    return (Start-Process -FilePath python -ArgumentList $argsStr `
        -RedirectStandardError $Log -WindowStyle Hidden -PassThru)
}

function Wait-MsshPort {
    param([int]$Port, [int]$Seconds)
    $end = [DateTime]::UtcNow.AddSeconds($Seconds)
    while ([DateTime]::UtcNow -lt $end) {
        try {
            $c = New-Object System.Net.Sockets.TcpClient
            $a = $c.BeginConnect('127.0.0.1', $Port, $null, $null)
            if ($a.AsyncWaitHandle.WaitOne(150) -and $c.Connected) { $c.Close(); return $true }
            $c.Close()
        } catch { }
        Start-Sleep -Milliseconds 150
    }
    return $false
}

$n = [guid]::NewGuid().ToString('N').Substring(0, 8)
$deepLog = Join-Path $env:TEMP "mssh-deepseek-$n.log"
$relayDeep = Start-MsshRelay $null $LocalPort $deepLog
if (-not (Wait-MsshPort $LocalPort 6)) {
    Write-Host "deepseek relay failed; log: $deepLog" -ForegroundColor Yellow
    Get-Content $deepLog -ErrorAction SilentlyContinue
    Stop-Process -Id $relayDeep.Id -Force -ErrorAction SilentlyContinue
    exit 1
}

$relayQwen = $null
$qwenLog = $null
if (-not $NoQwen) {
    $qwenLog = Join-Path $env:TEMP "mssh-qwen-$n.log"
    $relayQwen = Start-MsshRelay 'vllm' $QwenLocalPort $qwenLog
    if (-not (Wait-MsshPort $QwenLocalPort 6)) {
        Write-Host "qwen relay failed to start; skipping it (log: $qwenLog)" -ForegroundColor Yellow
        Get-Content $qwenLog -ErrorAction SilentlyContinue
        Stop-Process -Id $relayQwen.Id -Force -ErrorAction SilentlyContinue
        $relayQwen = $null
    }
}

# --- build forwards + env ---
$fwdDeep = "-R 127.0.0.1:${RemotePort}:127.0.0.1:${LocalPort}"
$exportStr = "export ANTHROPIC_BASE_URL=http://127.0.0.1:$RemotePort ANTHROPIC_API_KEY=local-bridge LLM_BASE_URL=http://127.0.0.1:$RemotePort/v1"
$sshArgsArr = @($fwdDeep) + $extra

if ($relayQwen) {
    $fwdQwen = "-R 127.0.0.1:${QwenRemotePort}:127.0.0.1:${QwenLocalPort}"
    $sshArgsArr += @($fwdQwen)
    $exportStr += " QWEN_BASE_URL=http://127.0.0.1:$QwenRemotePort/v1"
}

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
    if ($relayDeep -and -not $relayDeep.HasExited) { Stop-Process -Id $relayDeep.Id -Force -ErrorAction SilentlyContinue }
    if ($relayQwen -and -not $relayQwen.HasExited) { Stop-Process -Id $relayQwen.Id -Force -ErrorAction SilentlyContinue }
}
