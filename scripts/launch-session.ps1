# Jarvis - Launch Session (Windows)
# Starts the server and apps, then spreads the windows over all connected monitors.
# -ArrangeOnly only re-arranges windows that are already open.
param([switch]$ArrangeOnly)

# Everything this script does and every error goes to launch.log
Start-Transcript -Path (Join-Path $PSScriptRoot "..\launch.log") | Out-Null

# Load config
$configPath = Join-Path $PSScriptRoot "..\config.json"
$config = Get-Content $configPath -Raw -Encoding UTF8 | ConvertFrom-Json

$WORKSPACE_PATH = $config.workspace_path
$SPOTIFY_URI = $config.spotify_track

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
    [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr hWnd, uint msg, IntPtr w, IntPtr l);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);

    // Main window of one of these processes, including one hidden in the tray
    // (apps like Todoist only hide their window when closed). Visible windows win.
    public static IntPtr AppWindow(int[] pids) {
        var set = new HashSet<int>(pids);
        var skip = new HashSet<string> { "Default IME", "MSCTFIME UI", "DDE Server Window" };
        IntPtr visible = IntPtr.Zero, hidden = IntPtr.Zero;
        EnumWindows((h, l) => {
            uint pid; GetWindowThreadProcessId(h, out pid);
            if (!set.Contains((int)pid)) return true;
            var sb = new StringBuilder(256); GetWindowText(h, sb, 256);
            if (sb.Length == 0 || skip.Contains(sb.ToString())) return true;
            if (IsWindowVisible(h)) { if (visible == IntPtr.Zero) visible = h; }
            else if (hidden == IntPtr.Zero) hidden = h;
            return true;
        }, IntPtr.Zero);
        return visible != IntPtr.Zero ? visible : hidden;
    }

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
    if (-not $pattern) { return [IntPtr]::Zero }
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

# Only one launch at a time (voice, clap and a double click can overlap)
$mutex = New-Object System.Threading.Mutex($false, "JarvisLaunchSession")
if (-not $mutex.WaitOne(0)) { Stop-Transcript | Out-Null; exit }

# Is the Jarvis server already running? Then it is not started a second time.
$serverRunning = $false
try {
    Invoke-WebRequest "http://127.0.0.1:8340" -UseBasicParsing -TimeoutSec 2 | Out-Null
    $serverRunning = $true
} catch {}

if (-not $ArrangeOnly) {
# 1. Start whatever is not running yet: server, Spotify, Outlook, apps
if (-not $serverRunning) {
    # Prefer the project's virtualenv Python if there is one
    $PYTHON = Join-Path $WORKSPACE_PATH ".venv\Scripts\python.exe"
    if (-not (Test-Path $PYTHON)) { $PYTHON = "python" }
    # Run the server without a console window; its output goes to server.log.
    # The flag tells the server that the layout below opens the website windows.
    $env:JARVIS_LAUNCH_SESSION = "1"
    Start-Process $PYTHON -ArgumentList "server.py" -WorkingDirectory $WORKSPACE_PATH -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $WORKSPACE_PATH "server.log") -RedirectStandardError (Join-Path $WORKSPACE_PATH "server.err.log")
}
if ($SPOTIFY_URI -and $SPOTIFY_URI -notmatch "YOUR_" -and -not (Get-Process Spotify -ErrorAction SilentlyContinue)) { Start-Process $SPOTIFY_URI }
if ($config.programs.outlook -and -not (Get-Process olk, OUTLOOK -ErrorAction SilentlyContinue)) { Start-Process "explorer.exe" "shell:AppsFolder\$($config.programs.outlook)" }
if (Get-Command code -ErrorAction SilentlyContinue) { code $WORKSPACE_PATH }
foreach ($app in $config.apps) { Start-Process $app }

# Give the server a moment before the UI connects
if (-not $serverRunning) { Start-Sleep -Seconds 4 }
}

# 2. Monitors: 1 = primary, then the biggest remaining ones (a laptop screen comes last).
#    A layout entry for a monitor that is not connected goes to the last one.
$screens = @([System.Windows.Forms.Screen]::AllScreens | Sort-Object { -not $_.Primary }, { -($_.Bounds.Width * $_.Bounds.Height) }, { $_.Bounds.X })
function Get-Monitor($n) { $screens[[math]::Min([int]$n, $screens.Count) - 1] }

function Get-ProcessWindow($names, $seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    do {
        $p = Get-Process -Name $names -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
        if ($p) { return $p.MainWindowHandle }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    return [IntPtr]::Zero
}

# 3. Open and place every window from config.json "layout".
#    Each entry: {"url": [...] or "app": "outlook"/"spotify", "monitor": 1-3,
#                 "position": "left"/"right"/"full"/"minimized", "title": regex for -ArrangeOnly}
#    Chrome windows open one after another so no URL lands as a tab in another window.
foreach ($entry in $config.layout) {
    $hwnd = [IntPtr]::Zero
    if ($entry.url) {
        $urls = @($entry.url)
        if ($urls -match "localhost:8340") { $urls = @("--autoplay-policy=no-user-gesture-required") + $urls }
        # Reuse a window that is already open instead of opening it twice
        $hwnd = Find-Window $entry.title
        $isJarvis = [bool]($urls -match "localhost:8340")
        if ($isJarvis -and $hwnd -ne [IntPtr]::Zero -and -not $serverRunning -and -not $ArrangeOnly) {
            # Old Jarvis page from before "ZAC aus": close it, the new server needs a fresh one
            [WinPos]::PostMessage($hwnd, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero) | Out-Null  # WM_CLOSE
            Start-Sleep -Milliseconds 500
            $hwnd = [IntPtr]::Zero
        }
        if ($hwnd -eq [IntPtr]::Zero -and -not $ArrangeOnly) { $hwnd = New-ChromeWindow $urls }
    } elseif ($entry.app -eq "outlook") {
        # Outlook's main window, not an open mail window
        $hwnd = Get-ProcessWindow @("olk", "OUTLOOK") 20
    } elseif ($entry.app -eq "spotify") {
        $hwnd = Get-ProcessWindow @("Spotify") 15
    } elseif ($entry.app -and $entry.process) {
        # Any other program from "programs" (e.g. Todoist): start it if it is not running.
        # A window hidden in the tray is shown again by Place-Window.
        if (-not $ArrangeOnly -and -not (Get-Process -Name $entry.process -ErrorAction SilentlyContinue)) {
            $appId = $config.programs.($entry.app)
            if ($appId) { Start-Process "explorer.exe" "shell:AppsFolder\$appId" }
        }
        $deadline = (Get-Date).AddSeconds(20)
        do {
            $pids = [int[]]@(Get-Process -Name $entry.process -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
            if ($pids.Count -gt 0) { $hwnd = [WinPos]::AppWindow($pids) }
            if ($hwnd -ne [IntPtr]::Zero) { break }
            Start-Sleep -Milliseconds 500
        } while ((Get-Date) -lt $deadline)
    }
    if ($hwnd -eq [IntPtr]::Zero) { continue }
    if ($entry.position -eq "minimized") { [WinPos]::ShowWindow($hwnd, 6) | Out-Null; continue }
    Place-Window $hwnd (Get-Monitor $entry.monitor) $entry.position
}