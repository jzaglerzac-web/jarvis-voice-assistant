# Jarvis - Voice Trigger (Windows)
# Listens offline with the Windows speech recognizer for the wake phrase
# (config.json "wake_phrases") and then runs launch-session.ps1.
# Keeps listening afterwards, so Jarvis can be started again after "ZAC aus".

$configPath = Join-Path $PSScriptRoot "..\config.json"
$config = Get-Content $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
$phrases = if ($config.wake_phrases) { @($config.wake_phrases) } else { @("Partner lass was schaffen") }
# Minimum recognizer confidence (0-1); lower = reacts more easily, higher = fewer false starts
$minConfidence = if ($config.wake_confidence) { [double]$config.wake_confidence } else { 0.2 }
$launchScript = Join-Path $PSScriptRoot "launch-session.ps1"
$logFile = Join-Path $PSScriptRoot "..\voice.log"
function Log($msg) { Add-Content -Path $logFile -Value ("{0:HH:mm:ss} {1}" -f (Get-Date), $msg) -Encoding UTF8 }

Add-Type -AssemblyName System.Speech
$culture = New-Object System.Globalization.CultureInfo("de-DE")
$engine = New-Object System.Speech.Recognition.SpeechRecognitionEngine($culture)
$engine.SetInputToDefaultAudioDevice()

# Only the wake phrases: the recognizer scores every utterance against them,
# and the confidence threshold separates the real phrase from other speech
$wake = New-Object System.Speech.Recognition.Grammar((New-Object System.Speech.Recognition.GrammarBuilder((New-Object System.Speech.Recognition.Choices([string[]]$phrases)))))
$wake.Name = "wake"
$engine.LoadGrammar($wake)

Log "Warte auf: $($phrases -join ' / ') (ab Sicherheit $minConfidence)"
while ($true) {
    $result = $engine.Recognize([TimeSpan]::FromSeconds(30))
    if (-not $result) { continue }
    Log ("Gehoert: '{0}' ({1:N2})" -f $result.Text, $result.Confidence)
    if ($result.Confidence -ge $minConfidence) {
        Log "Startphrase erkannt, starte Jarvis."
        Start-Process "powershell.exe" -WindowStyle Hidden -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$launchScript`""
        Start-Sleep -Seconds 30  # Jarvis is starting; ignore the room for a moment
    }
}
