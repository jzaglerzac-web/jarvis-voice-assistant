# Jarvis - start both triggers in the background:
# double clap (clap-trigger.py) and wake phrase (voice-trigger.ps1).
# Both keep listening; launch-session.ps1 does nothing twice if Jarvis already runs.

$root = Split-Path $PSScriptRoot -Parent
$python = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $python)) { $python = "pythonw" }

# Don't start a second listener if one is already running
$running = Get-CimInstance Win32_Process -Filter "Name like 'python%' or Name = 'powershell.exe'" |
    Where-Object { $_.CommandLine -match "clap-trigger\.py|voice-trigger\.ps1" }
if ($running) { exit }

Start-Process $python -ArgumentList "`"$PSScriptRoot\clap-trigger.py`"" -WorkingDirectory $root
Start-Process "powershell.exe" -WindowStyle Hidden -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSScriptRoot\voice-trigger.ps1`""
