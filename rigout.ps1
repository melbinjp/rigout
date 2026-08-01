<#
.SYNOPSIS
  Rigout - convenience wrapper for running from a source checkout.

.DESCRIPTION
  DEPRECATED as of 0.3.0. Every action here now delegates to the packaged
  lifecycle CLI, which is the supported entry point:

    rigout start [--detach]   rigout status   rigout logs [--follow]   rigout stop

  Not a like-for-like alias:

    .\rigout.ps1              ->  rigout start --tunnel cloudflare
    .\rigout.ps1 -Background  ->  rigout start --detach --tunnel cloudflare
    .\rigout.ps1 stop         ->  rigout stop
    .\rigout.ps1 status       ->  rigout status

  This wrapper's historical default is --tunnel cloudflare, which puts this
  machine on a PUBLIC Cloudflare quick-tunnel URL. The installed `rigout` CLI
  defaults to --tunnel none, serving on loopback only. The default is kept here
  because changing it would break existing scripts, so the difference is stated
  instead. For a local-only server, pass -Tunnel none explicitly.

  The wrapper remains so that a checkout with no `pip install` still has a
  one-command start, and so existing scripts that call it keep working. It
  deliberately implements no lifecycle logic of its own: the previous version
  polled for a connection file the launcher writes elsewhere, timed out after
  45 seconds, and then killed the healthy server it had just started.

.PARAMETER Action
  start (default) | stop | status

.PARAMETER Background
  Run the server in the background instead of the foreground.

.PARAMETER Port
  Local bind port for the MCP server (default: 8765).

.PARAMETER Tunnel
  Tunnel provider: "cloudflare" or "none" (default: cloudflare).

.EXAMPLE
  .\rigout.ps1                     # Foreground, PUBLIC Cloudflare tunnel (Ctrl+C to stop)
  .\rigout.ps1 start               # Same as above
  .\rigout.ps1 -Tunnel none        # Foreground, loopback only - no public URL
  .\rigout.ps1 -Background         # Background, PUBLIC Cloudflare tunnel
  .\rigout.ps1 stop                # Stop background server
  .\rigout.ps1 status              # Check if server is running
#>

# Default values
$Action = "start"
$Background = $false
$Port = 8765
$Tunnel = "cloudflare"

# Manual argument parsing to support POSIX-style double-dash flags identically on all platforms
$i = 0
while ($i -lt $args.Count) {
    $arg = $args[$i]
    switch -Regex ($arg) {
        "^(start|stop|status)$" {
            $Action = $Matches[1]
            $i++
        }
        "^(--background|-b|-Background)$" {
            $Background = $true
            $i++
        }
        "^(--port|-p|-Port)$" {
            $Port = [int]$args[$i + 1]
            $i += 2
        }
        "^(--tunnel|-t|-Tunnel)$" {
            $Tunnel = $args[$i + 1]
            $i += 2
        }
        "^(--help|-h|-Help)$" {
            Get-Help $MyInvocation.MyCommand.Definition -Detailed
            exit 0
        }
        default {
            Write-Error "Unknown option: $arg"
            exit 1
        }
    }
}

$ErrorActionPreference = "Stop"
# PowerShell 7.4+ turns a non-zero native exit code into a terminating error when
# ErrorActionPreference is Stop. This script forwards exit codes deliberately
# (`rigout status` exits 1 when stopped), so that behaviour must be off.
if (Test-Path Variable:PSNativeCommandUseErrorActionPreference) {
    $PSNativeCommandUseErrorActionPreference = $false
}
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
# Pre-0.3.0 wrappers supervised the server themselves and left these behind.
# They are read for cleanup only; nothing writes them any more.
$LegacyPidFile = Join-Path $ScriptDir ".rigout.pid"
$LegacyConnectionFile = Join-Path $ScriptDir "ai_agent_connection.json"
$BinDir = Join-Path $ScriptDir "bin"
$PythonBin = if ($env:PYTHON) { $env:PYTHON } else { "python" }

# -- Helpers ---------------------------------------------------------------

function Write-Banner {
    Write-Host ""
    Write-Host "  +--------------------------------------+" -ForegroundColor Cyan
    Write-Host "  |      Rigout MCP Server                |" -ForegroundColor Cyan
    Write-Host "  |  Rig up your hardware for AI agents  |" -ForegroundColor Cyan
    Write-Host "  +--------------------------------------+" -ForegroundColor Cyan
    Write-Host ""
}

function Write-DeprecationNotice {
    Write-Warning "rigout.ps1 is deprecated; it now forwards to the packaged CLI (rigout start/status/stop)."
}

function Initialize-Environment {
    # Add local bin/ to PATH so cloudflared is found
    if (Test-Path $BinDir) {
        if ($env:PATH -notlike "*$BinDir*") {
            $env:PATH = "$BinDir;$env:PATH"
        }
    }
    # Let `python -m rigout...` resolve from the checkout when the package is not installed.
    $srcPath = Join-Path $ScriptDir "src"
    if (Test-Path $srcPath) {
        if (-not $env:PYTHONPATH) {
            $env:PYTHONPATH = $srcPath
        } elseif ($env:PYTHONPATH -notlike "*$srcPath*") {
            $env:PYTHONPATH = "$srcPath;$env:PYTHONPATH"
        }
    }
}

# Fail with an instruction rather than a ModuleNotFoundError traceback.
function Test-RigoutImportable {
    & $PythonBin -c "import rigout" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  [ERROR] Could not import rigout using '$PythonBin'." -ForegroundColor Red
        Write-Host "  Install it first:  $PythonBin -m pip install -e `"$ScriptDir`"" -ForegroundColor Yellow
        exit 1
    }
}

function Get-LegacyPids {
    if (-not (Test-Path $LegacyPidFile)) { return @() }
    return Get-Content $LegacyPidFile | Where-Object { $_ -match '^\s*\d+\s*$' } | ForEach-Object { [int]$_.Trim() }
}

function Test-LegacyProcessAlive {
    param([int]$ProcessId)
    try {
        $proc = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
        return ($null -ne $proc -and -not $proc.HasExited)
    } catch {
        return $false
    }
}

# A server started by a pre-0.3.0 wrapper is not in the managed state directory,
# so `rigout stop` cannot see it. Clean it up here or it becomes unstoppable.
function Stop-LegacyProcesses {
    $pids = Get-LegacyPids
    if ($pids.Count -eq 0) {
        if (Test-Path $LegacyPidFile) { Remove-Item $LegacyPidFile -Force -ErrorAction SilentlyContinue }
        return
    }
    foreach ($procId in $pids) {
        if (Test-LegacyProcessAlive $procId) {
            # Also kill child processes such as cloudflared.
            Get-CimInstance Win32_Process | Where-Object { $_.ParentProcessId -eq $procId } | ForEach-Object {
                try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch {}
            }
            try {
                Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
                Write-Host "  [OK] Stopped legacy background process $procId (started by an older rigout.ps1)." -ForegroundColor Green
            } catch {
                Write-Host "  [!] Could not stop legacy process ${procId}: $_" -ForegroundColor Yellow
            }
        }
    }
    Remove-Item $LegacyPidFile -Force -ErrorAction SilentlyContinue
}

# Never start over the top of a live legacy server: refuse and let the user
# decide, rather than killing something healthy on their behalf.
function Assert-NoLegacyServerRunning {
    $pids = Get-LegacyPids
    foreach ($procId in $pids) {
        if (Test-LegacyProcessAlive $procId) {
            Write-Host "  [!] A Rigout server started by an older rigout.ps1 is still running (PID $procId)." -ForegroundColor Yellow
            Write-Host "  Stop it first:  .\rigout.ps1 stop" -ForegroundColor Yellow
            exit 1
        }
    }
    # Only the file is left; no live process. Safe to clear.
    if (Test-Path $LegacyPidFile) { Remove-Item $LegacyPidFile -Force -ErrorAction SilentlyContinue }
}

function Show-LegacyLeftovers {
    if (Test-Path $LegacyPidFile) {
        Write-Warning "$LegacyPidFile is left over from an older rigout.ps1. Run .\rigout.ps1 stop to clear it."
    }
    if (Test-Path $LegacyConnectionFile) {
        Write-Warning "$LegacyConnectionFile is stale and no longer used; the live one is shown by 'rigout status'."
    }
}

# -- Main ------------------------------------------------------------------
# The launcher is invoked inline rather than through a helper function: a
# PowerShell function returns everything written to the success stream, so
# wrapping the call would capture the launcher's output instead of letting it
# reach the caller's console, pipeline or redirection.

Write-Banner
Write-DeprecationNotice
Initialize-Environment
Test-RigoutImportable

# --port and --tunnel are always passed explicitly so this wrapper keeps its own
# historical defaults (tunnel=cloudflare) rather than inheriting the CLI's.
switch ($Action) {
    "start" {
        Assert-NoLegacyServerRunning
        $startArgs = @("start", "--tunnel", $Tunnel, "--port", "$Port")
        if ($Background) { $startArgs += "--detach" }
        & $PythonBin -m rigout.mcp_url_launcher @startArgs
        exit $LASTEXITCODE
    }
    "stop" {
        Stop-LegacyProcesses
        & $PythonBin -m rigout.mcp_url_launcher stop
        exit $LASTEXITCODE
    }
    "status" {
        Show-LegacyLeftovers
        & $PythonBin -m rigout.mcp_url_launcher status
        exit $LASTEXITCODE
    }
}
