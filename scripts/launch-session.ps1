# Jarvis — Launch Session (Windows)

# Load config
$configPath = Join-Path $PSScriptRoot "..\config.json"
$config = Get-Content $configPath -Raw -Encoding UTF8 | ConvertFrom-Json

$WORKSPACE_PATH = $config.workspace_path
$SPOTIFY_URI = $config.spotify_track
$BROWSER_URL = $config.browser_url

# Load assemblies
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class WinPos {
    [DllImport("user32.dll")]
    public static extern bool MoveWindow(IntPtr hWnd, int X, int Y, int W, int H, bool repaint);
    [DllImport("user32.dll")]
    public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
}
"@

function Snap-Window($proc, $x, $y, $w, $h) {
    if ($proc) {
        [WinPos]::ShowWindow($proc.MainWindowHandle, 9) | Out-Null
        Start-Sleep -Milliseconds 200
        [WinPos]::MoveWindow($proc.MainWindowHandle, $x, $y, $w, $h, $true) | Out-Null
    }
}

$screenW = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea.Width
$screenH = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea.Height
$halfW = [math]::Floor($screenW / 2)
$halfH = [math]::Floor($screenH / 2)

# 1. Start server + Spotify + apps
# Prefer the project's virtualenv Python if there is one
$PYTHON = Join-Path $WORKSPACE_PATH ".venv\Scripts\python.exe"
if (-not (Test-Path $PYTHON)) { $PYTHON = "python" }
Start-Process $PYTHON -ArgumentList "server.py" -WorkingDirectory $WORKSPACE_PATH -WindowStyle Minimized
if ($SPOTIFY_URI -and $SPOTIFY_URI -notmatch "YOUR_") { Start-Process $SPOTIFY_URI }
if (Get-Command code -ErrorAction SilentlyContinue) { code $WORKSPACE_PATH }
foreach ($app in $config.apps) { Start-Process $app }

# Give the server a moment before the UI connects
Start-Sleep -Seconds 4

# 2. Chrome with Jarvis + optional website
$chromeArgs = "--autoplay-policy=no-user-gesture-required http://localhost:8340"
if ($BROWSER_URL -and $BROWSER_URL -notmatch "your-website") { $chromeArgs += " $BROWSER_URL" }
Start-Process "chrome" -ArgumentList $chromeArgs

# 3. Snap all windows into quadrants
Start-Sleep -Seconds 3

$vscode = Get-Process -Name "Code" -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
Snap-Window $vscode 0 0 $halfW $halfH

$obsidian = Get-Process -Name "Obsidian" -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
Snap-Window $obsidian $halfW 0 $halfW $halfH

$chrome = Get-Process -Name "chrome" -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
Snap-Window $chrome 0 $halfH $halfW $halfH

$spotify = Get-Process -Name "Spotify" -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
Snap-Window $spotify $halfW $halfH $halfW $halfH
