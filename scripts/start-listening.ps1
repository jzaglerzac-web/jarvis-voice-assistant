# Jarvis - start both triggers in the background:
# double clap (clap-trigger.py) and wake phrase (voice-trigger.ps1).
# Whichever fires first starts launch-session.ps1, which then stops both listeners:
# they only work once per Windows start, later starts go through the desktop icon.

$root = Split-Path $PSScriptRoot -Parent
$python = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $python)) { $python = "pythonw" }

# Don't start a second listener if one is already running
$running = Get-CimInstance Win32_Process -Filter "Name like 'python%' or Name = 'powershell.exe'" |
    Where-Object { $_.CommandLine -match "clap-trigger\.py|voice-trigger\.ps1" }
if ($running) { exit }

Start-Process $python -ArgumentList "`"$PSScriptRoot\clap-trigger.py`"" -WorkingDirectory $root
Start-Process "powershell.exe" -WindowStyle Hidden -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSScriptRoot\voice-trigger.ps1`""
