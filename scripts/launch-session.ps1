# Jarvis - Launch Session (Windows)
# Starts the server and apps, then spreads the windows over all connected monitors.
# -ArrangeOnly only re-arranges windows that are already open.
param([switch]$ArrangeOnly)

# Load config
$configPath = Join-Path $PSScriptRoot "..\config.json"
$config = Get-Content $configPath -Raw -Encoding UTF8 | ConvertFrom-Json

$WORKSPACE_PATH = $config.workspace_path
$SPOTIFY_URI = $config.spotify_track
$BROWSER_URL = $config.browser_url
# URLs for the separate work browser window (first one is the active tab)
$WORK_URLS = if ($config.work_browser_urls) { @($config.work_browser_urls) } else { @() }

# Load assemblies
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public class WinPos {
    public delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, StringBuilder s, int n);
    [DllImport("user32.dll")] public static extern bool MoveWindow(IntPtr hWnd, int X, int Y, int W, int H, bool repaint);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();

    // All visible top-level windows with their titles
    public static Dictionary<IntPtr, string> Windows() {
        var result = new Dictionary<IntPtr, string>();
        EnumWindows((h, l) => {
            if (IsWindowVisible(h)) {
                var sb = new StringBuilder(512);
                GetWindowText(h, sb, 512);
                if (sb.Length > 0) result[h] = sb.ToString();
            }
            return true;
        }, IntPtr.Zero);
        return result;
    }
}
"@
# Work in physical pixels so positions are right on scaled monitors
[WinPos]::SetProcessDPIAware() | Out-Null

function Find-Window($pattern) {
    foreach ($w in [WinPos]::Windows().GetEnumerator()) {
        if ($w.Value -match $pattern) { return $w.Key }
    }
    return [IntPtr]::Zero
}

# Wait until a window whose title matches appears (Chrome tabs need a moment to load)
function Wait-Window($pattern, $seconds = 25) {
    if (-not $pattern) { return [IntPtr]::Zero }
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        $h = Find-Window $pattern
        if ($h -ne [IntPtr]::Zero) { return $h }
        Start-Sleep -Milliseconds 500
    }
    return [IntPtr]::Zero
}

# Open a new Chrome window and return its handle (the window that was not there before)
function New-ChromeWindow($chromeArgs, $seconds = 20) {
    $before = @([WinPos]::Windows().Keys)
    Start-Process "chrome" -ArgumentList (@("--new-window") + $chromeArgs)
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
        foreach ($w in [WinPos]::Windows().GetEnumerator()) {
            if ($before -notcontains $w.Key -and $w.Value -match "Google Chrome$") { return $w.Key }
        }
    }
    return [IntPtr]::Zero
}

# Place a window on a monitor: half = "left", "right" or "full"
function Place-Window($hwnd, $screen, $half) {
    if ($hwnd -eq [IntPtr]::Zero -or -not $screen) { return }
    $a = $screen.WorkingArea
    $w = [math]::Floor($a.Width / 2)
    [WinPos]::ShowWindow($hwnd, 9) | Out-Null  # restore if maximized/minimized
    Start-Sleep -Milliseconds 150
    # Move onto the monitor first: on a monitor with different scaling Windows
    # resizes the window on arrival, so the real size is set afterwards
    [WinPos]::MoveWindow($hwnd, $a.X, $a.Y, $w, $a.Height, $true) | Out-Null
    Start-Sleep -Milliseconds 300
    switch ($half) {
        "left"  { [WinPos]::MoveWindow($hwnd, $a.X, $a.Y, $w, $a.Height, $true) | Out-Null }
        "right" { [WinPos]::MoveWindow($hwnd, $a.X + $w, $a.Y, $a.Width - $w, $a.Height, $true) | Out-Null }
        "full"  {
            [WinPos]::MoveWindow($hwnd, $a.X, $a.Y, $a.Width, $a.Height, $true) | Out-Null
            [WinPos]::ShowWindow($hwnd, 3) | Out-Null  # maximize on that monitor
        }
    }
}

if (-not $ArrangeOnly) {
# 1. Start server + Spotify + Outlook + apps
# Prefer the project's virtualenv Python if there is one
$PYTHON = Join-Path $WORKSPACE_PATH ".venv\Scripts\python.exe"
if (-not (Test-Path $PYTHON)) { $PYTHON = "python" }
# Run the server without a console window; its output goes to server.log.
# The flag tells the server that this script opens the activate_url window.
$env:JARVIS_LAUNCH_SESSION = "1"
Start-Process $PYTHON -ArgumentList "server.py" -WorkingDirectory $WORKSPACE_PATH -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $WORKSPACE_PATH "server.log") -RedirectStandardError (Join-Path $WORKSPACE_PATH "server.err.log")
if ($SPOTIFY_URI -and $SPOTIFY_URI -notmatch "YOUR_") { Start-Process $SPOTIFY_URI }
if ($config.programs.outlook) { Start-Process "explorer.exe" "shell:AppsFolder\$($config.programs.outlook)" }
if (Get-Command code -ErrorAction SilentlyContinue) { code $WORKSPACE_PATH }
foreach ($app in $config.apps) { Start-Process $app }

# Give the server a moment before the UI connects
Start-Sleep -Seconds 4

# 2. Three separate Chrome windows, opened one after another so no URL lands as a tab
#    in another window: website (activate_url), Jarvis, work window (e.g. WhatsApp + Staffomatic)
if ($config.activate_url) { $zac = New-ChromeWindow @($config.activate_url) }
$jarvisUrls = @("--autoplay-policy=no-user-gesture-required", "http://localhost:8340")
if ($BROWSER_URL -and $BROWSER_URL -notmatch "your-website") { $jarvisUrls += $BROWSER_URL }
$jarvis = New-ChromeWindow $jarvisUrls
if ($WORK_URLS.Count -gt 0) { $work = New-ChromeWindow $WORK_URLS }
}

# 4. Spread windows over the monitors
# Primary monitor first, then the biggest remaining ones (a laptop screen comes last)
$screens = @([System.Windows.Forms.Screen]::AllScreens | Sort-Object { -not $_.Primary }, { -($_.Bounds.Width * $_.Bounds.Height) }, { $_.Bounds.X })
$m1 = $screens[0]
$m2 = if ($screens.Count -ge 2) { $screens[1] } else { $m1 }
$m3 = if ($screens.Count -ge 3) { $screens[2] } else { $null }

# Windows opened above are already known; in -ArrangeOnly mode find them by title
if (-not $zac)    { $zac    = Wait-Window $config.activate_window_title 3 }
if (-not $jarvis) { $jarvis = Find-Window "^J\.A\.R\.V\.I\.S\." }
if (-not $work)   { $work   = Wait-Window $config.work_window_title 3 }
$outlook = Wait-Window "Outlook$" 15
# Prefer Outlook's main window over open mail windows
$olkMain = (Get-Process -Name "olk", "OUTLOOK" -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1).MainWindowHandle
if ($olkMain) { $outlook = $olkMain }
$spotify = (Get-Process -Name "Spotify" -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1).MainWindowHandle

# Main monitor: website left half, Outlook right half
Place-Window $zac $m1 "left"
Place-Window $outlook $m1 "right"

if ($screens.Count -eq 1) {
    # One monitor: the rest stacks behind the website and Outlook
    Place-Window $work $m1 "left"
    Place-Window $jarvis $m1 "right"
} elseif ($screens.Count -eq 2) {
    Place-Window $work $m2 "left"
    Place-Window $jarvis $m2 "right"
} else {
    Place-Window $work $m2 "full"
    if ($spotify) {
        Place-Window $jarvis $m3 "left"
        Place-Window $spotify $m3 "right"
    } else {
        Place-Window $jarvis $m3 "full"
    }
}
