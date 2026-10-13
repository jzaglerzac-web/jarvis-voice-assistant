#!/usr/bin/env python3
"""
Jarvis — Double Clap Trigger
Listens to mic. Detects two claps within 1.5s, min 0.1s apart.
On trigger: runs scripts/launch-session.ps1, then keeps listening
(so Jarvis can be started again after "ZAC aus").
"""

import sounddevice as sd
import numpy as np
import subprocess
import time
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT_PATH = os.path.join(SCRIPT_DIR, "launch-session.ps1")
LOG_PATH = os.path.join(SCRIPT_DIR, "..", "clap.log")

SAMPLE_RATE = 44100
BLOCK_SIZE = 1024
THRESHOLD = 0.08       # RMS volume spike threshold — lower = more sensitive
MIN_GAP = 0.1          # Minimum seconds between claps
MAX_GAP = 1.5          # Maximum seconds between claps — more time for second clap
COOLDOWN = 30.0        # Seconds to ignore claps after the trigger fired (Jarvis is starting)

last_clap_time = 0.0
cooldown_until = 0.0


def log(msg):
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")


def audio_callback(indata, frames, time_info, status):
    global last_clap_time, cooldown_until

    now = time.time()
    if now < cooldown_until:
        return

    rms = float(np.sqrt(np.mean(indata ** 2)))

    if rms > THRESHOLD:
        gap = now - last_clap_time

        if gap >= MIN_GAP:
            if gap <= MAX_GAP and last_clap_time > 0:
                # Second clap — fire trigger, then ignore claps for COOLDOWN seconds
                log(f"Doppelklatschen erkannt (rms={rms:.3f}), starte Jarvis")
                last_clap_time = 0.0
                cooldown_until = now + COOLDOWN
                subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", SCRIPT_PATH],
                                 creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                # First clap
                log(f"Erstes Klatschen (rms={rms:.3f})")
                last_clap_time = now


with sd.InputStream(
    samplerate=SAMPLE_RATE,
    blocksize=BLOCK_SIZE,
    channels=1,
    dtype="float32",
    callback=audio_callback,
):
    log("Warte auf Doppelklatschen")
    while True:
        time.sleep(1)
