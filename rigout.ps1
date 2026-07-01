<#
.SYNOPSIS
  Rigout - Rig up your hardware for AI agents.

.DESCRIPTION
  Unified launcher for the Rigout MCP gateway server and Cloudflare tunnel.
  By default runs in the foreground. Press Ctrl+C to stop.

.PARAMETER Action
  start (default) | stop | status

.PARAMETER Background
  Run the server in the background instead of the foreground.

.PARAMETER Port
  Local bind port for the MCP server (default: 8765).

.PARAMETER Tunnel
  Tunnel provider: "cloudflare" or "none" (default: cloudflare).

.EXAMPLE
  .\rigout.ps1                # Start in foreground (Ctrl+C to stop)
  .\rigout.ps1 start          # Same as above
  .\rigout.ps1 -Background    # Start in background
  .\rigout.ps1 stop           # Stop background server
  .\rigout.ps1 status         # Check if server is running
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
        default {
            Write-Error "Unknown option: $arg"
            exit 1
        }
    }
}

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$PidFile = Join-Path $ScriptDir ".rigout.pid"
$ConnectionFile = Join-Path $ScriptDir "ai_agent_connection.json"
$BinDir = Join-Path $ScriptDir "bin"

# -- Helpers ---------------------------------------------------------------

function Write-Banner {
    Write-Host ""
    Write-Host "  +--------------------------------------+" -ForegroundColor Cyan
    Write-Host "  |      Rigout MCP Server                |" -ForegroundColor Cyan
    Write-Host "  |  Rig up your hardware for AI agents  |" -ForegroundColor Cyan
    Write-Host "  +--------------------------------------+" -ForegroundColor Cyan
    Write-Host ""
}

function Ensure-Path {
    # Add local bin/ to PATH so cloudflared is found
    if (Test-Path $BinDir) {
        if ($env:PATH -notlike "*$BinDir*") {
            $env:PATH = "$BinDir;$env:PATH"
        }
    }
    $srcPath = Join-Path $ScriptDir "src"
    if (Test-Path $srcPath) {
        if (-not $env:PYTHONPATH) {
            $env:PYTHONPATH = $srcPath
        } elseif ($env:PYTHONPATH -notlike "*$srcPath*") {
            $env:PYTHONPATH = "$srcPath;$env:PYTHONPATH"
        }
    }
}

function Get-SavedPids {
    if (Test-Path $PidFile) {
        return Get-Content $PidFile | ForEach-Object { [int]$_ }
    }
    return @()
}

function Test-ServerRunning {
    $pids = Get-SavedPids
    if ($pids.Count -eq 0) { return $false }
    foreach ($procId in $pids) {
        try {
            $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
            if ($proc -and -not $proc.HasExited) { return $true }
        } catch {}
    }
    return $false
}

function Wait-ForConnection {
    param([int]$TimeoutSeconds = 45)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Path $ConnectionFile) {
            $json = Get-Content $ConnectionFile -Raw | ConvertFrom-Json
            if ($json.mcp_server_url) {
                return $json
            }
        }
        Start-Sleep -Milliseconds 500
    }
    return $null
}

function Show-ConnectionInfo {
    param($ConnData)
    Write-Host ""
    Write-Host "  [OK] Server is running!" -ForegroundColor Green
    Write-Host ""
    Write-Host "  MCP URL:    " -NoNewline -ForegroundColor White
    Write-Host $ConnData.mcp_server_url -ForegroundColor Yellow
    Write-Host "  Health:     " -NoNewline -ForegroundColor White
    Write-Host $ConnData.mcp.health_url -ForegroundColor Yellow
    Write-Host "  Transport:  " -NoNewline -ForegroundColor White
    Write-Host $ConnData.mcp.transport -ForegroundColor DarkCyan
    Write-Host ""
    $hw = $ConnData.hardware_info
    Write-Host "  Hardware:   $($hw.platform) $($hw.architecture), $($hw.cpu_count) CPUs" -ForegroundColor DarkGray
    Write-Host "  Config:     $ConnectionFile" -ForegroundColor DarkGray
    Write-Host ""
}

# -- Actions ---------------------------------------------------------------

function Start-Foreground {
    Write-Banner
    Ensure-Path

    if (Test-ServerRunning) {
        Write-Host "  [!] Server is already running in the background." -ForegroundColor Yellow
        Write-Host "  Run .\rigout.ps1 stop first, or use .\rigout.ps1 status." -ForegroundColor Yellow
        return
    }

    # Remove stale connection file so we can detect a fresh one
    if (Test-Path $ConnectionFile) { Remove-Item $ConnectionFile -Force }

    Write-Host "  Starting MCP server (port $Port, tunnel: $Tunnel)..." -ForegroundColor Cyan
    Write-Host "  Press Ctrl+C to stop." -ForegroundColor DarkGray
    Write-Host ""

    # Run in foreground -- Ctrl+C will terminate it naturally
    $setupArgs = @(
        "-m",
        "rigout.mcp_url_launcher",
        "--tunnel", $Tunnel,
        "--port", $Port
    )
    & python @setupArgs
}

function Start-Background {
    Write-Banner
    Ensure-Path

    if (Test-ServerRunning) {
        Write-Host "  [!] Server is already running." -ForegroundColor Yellow
        Write-Host "  Run .\rigout.ps1 stop first, or use .\rigout.ps1 status." -ForegroundColor Yellow
        return
    }

    # Remove stale connection file so we can detect a fresh one
    if (Test-Path $ConnectionFile) { Remove-Item $ConnectionFile -Force }

    Write-Host "  Starting MCP server in background (port $Port, tunnel: $Tunnel)..." -ForegroundColor Cyan

    $logFile = Join-Path $ScriptDir ".rigout.log"

    # Ensure bin/ is in PATH for the child process (cloudflared lives there)
    Ensure-Path

    $proc = Start-Process -FilePath python `
        -ArgumentList "-m rigout.mcp_url_launcher --tunnel $Tunnel --port $Port" `
        -WorkingDirectory $ScriptDir `
        -WindowStyle Hidden `
        -PassThru

    # Save PID
    $proc.Id | Out-File -FilePath $PidFile -Encoding ascii

    Write-Host "  Waiting for server to become ready..." -ForegroundColor DarkGray

    $connData = Wait-ForConnection -TimeoutSeconds 45
    if ($connData) {
        Show-ConnectionInfo $connData
        Write-Host "  To stop:    .\rigout.ps1 stop" -ForegroundColor DarkGray
        Write-Host "  To check:   .\rigout.ps1 status" -ForegroundColor DarkGray
        Write-Host ""
    } else {
        Write-Host "  [ERROR] Server did not start within 45 seconds." -ForegroundColor Red
        Write-Host "  Check logs or try running in foreground: .\rigout.ps1" -ForegroundColor Yellow
        # Clean up
        try { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue } catch {}
        if (Test-Path $PidFile) { Remove-Item $PidFile -Force }
    }
}

function Stop-Server {
    Write-Banner

    $pids = Get-SavedPids
    if ($pids.Count -eq 0) {
        Write-Host "  [i] No background server found (no .rigout.pid file)." -ForegroundColor Yellow
        Write-Host "  If running in foreground, press Ctrl+C in that terminal." -ForegroundColor DarkGray
        return
    }

    $stopped = 0
    foreach ($procId in $pids) {
        try {
            $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
            if ($proc -and -not $proc.HasExited) {
                # Also kill child processes such as cloudflared.
                Get-CimInstance Win32_Process | Where-Object { $_.ParentProcessId -eq $procId } | ForEach-Object {
                    try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch {}
                }
                Stop-Process -Id $procId -Force
                $stopped++
                Write-Host "  [OK] Stopped process $procId" -ForegroundColor Green
            } else {
                Write-Host "  [i] Process $procId already exited" -ForegroundColor DarkGray
            }
        } catch {
            Write-Host "  [!] Could not stop process ${procId}: $_" -ForegroundColor Yellow
        }
    }

    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    Write-Host ""
    if ($stopped -gt 0) {
        Write-Host "  Server stopped." -ForegroundColor Green
    } else {
        Write-Host "  No running processes found." -ForegroundColor DarkGray
    }
    Write-Host ""
}

function Show-Status {
    Write-Banner

    # Check background processes
    $isRunning = Test-ServerRunning
    if ($isRunning) {
        Write-Host "  [RUNNING] Background server is active" -ForegroundColor Green
        $pids = Get-SavedPids
        $pidList = $pids -join ", "
        Write-Host "     PIDs: $pidList" -ForegroundColor DarkGray
    } else {
        Write-Host "  [STOPPED] No background server detected" -ForegroundColor Red
    }

    # Check health endpoint
    Write-Host ""
    try {
        $response = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3 -ErrorAction Stop
        Write-Host "  [HEALTHY] Health check: $($response.status)" -ForegroundColor Green
        Write-Host "     Server:    $($response.server)" -ForegroundColor DarkGray
        Write-Host "     Transport: $($response.transport)" -ForegroundColor DarkGray
        Write-Host "     MCP URL:   $($response.mcp_url)" -ForegroundColor DarkGray
    } catch {
        Write-Host "  [DOWN] Health check: server not responding on port $Port" -ForegroundColor Red
    }

    # Show connection file info
    if (Test-Path $ConnectionFile) {
        Write-Host ""
        $connData = Get-Content $ConnectionFile -Raw | ConvertFrom-Json
        Write-Host "  Connection file: $ConnectionFile" -ForegroundColor DarkGray
        Write-Host "  Public URL: $($connData.mcp_server_url)" -ForegroundColor Yellow
    }

    Write-Host ""
}

# -- Main ------------------------------------------------------------------

switch ($Action) {
    "start" {
        if ($Background) {
            Start-Background
        } else {
            Start-Foreground
        }
    }
    "stop"   { Stop-Server }
    "status" { Show-Status }
}
