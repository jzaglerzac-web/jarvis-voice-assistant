# ZAC Zentrale: starts the Jarvis server if it is not running yet, then opens
# http://localhost:8340/zentrale as its own Chrome app window (no browser bar).
$jarvis = Split-Path $PSScriptRoot -Parent
$url = "http://localhost:8340/zentrale"

function Test-Server {
    try { Invoke-WebRequest "http://127.0.0.1:8340" -UseBasicParsing -TimeoutSec 2 | Out-Null; return $true } catch { return $false }
}

if (-not (Test-Server)) {
    $python = Join-Path $jarvis ".venv\Scripts\python.exe"
    if (-not (Test-Path $python)) { $python = "python" }
    # Tells the server not to open the activate_url website in an extra Chrome window
    $env:JARVIS_LAUNCH_SESSION = "1"
    Start-Process $python -ArgumentList "server.py" -WorkingDirectory $jarvis -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $jarvis "server.log") -RedirectStandardError (Join-Path $jarvis "server.err.log")
    $deadline = (Get-Date).AddSeconds(30)
    while (-not (Test-Server) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
}

# Already open? Then just bring it to the front instead of opening a second Jarvis window
if ((New-Object -ComObject WScript.Shell).AppActivate("ZAC Zentrale")) { exit }
Start-Process "chrome" -ArgumentList @("--autoplay-policy=no-user-gesture-required", "--app=$url")
