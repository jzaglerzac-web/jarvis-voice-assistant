"""
Prueft die ElevenLabs-Verbindung mit den Werten aus config.json.

    python scripts/test-elevenlabs.py          # Key + Stimme pruefen, test.mp3 erzeugen
    python scripts/test-elevenlabs.py --voices # verfuegbare Stimmen auflisten
"""

import json
import os
import sys

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = "https://api.elevenlabs.io/v1"

with open(os.path.join(ROOT, "config.json"), "r") as f:
    config = json.load(f)

key = config.get("elevenlabs_api_key", "")
voice_id = config.get("elevenlabs_voice_id", "")
if not key or key.startswith("YOUR_"):
    sys.exit("elevenlabs_api_key fehlt in config.json")

headers = {"xi-api-key": key}

if "--voices" in sys.argv:
    resp = httpx.get(f"{API}/voices", headers=headers, timeout=20)
    resp.raise_for_status()
    for v in resp.json()["voices"]:
        labels = v.get("labels", {})
        print(f"{v['voice_id']}  {v['name']:<20} {v.get('category', ''):<12} "
              f"{labels.get('gender', '')} {labels.get('accent', '')}")
    sys.exit(0)

if not voice_id or voice_id.startswith("YOUR_"):
    sys.exit("elevenlabs_voice_id fehlt in config.json (Liste: --voices)")

resp = httpx.post(
    f"{API}/text-to-speech/{voice_id}",
    headers={**headers, "Accept": "audio/mpeg"},
    json={"text": "Guten Tag, Sir. Alle Systeme sind bereit.", "model_id": "eleven_turbo_v2_5"},
    timeout=30,
)
if resp.status_code != 200:
    sys.exit(f"Fehler {resp.status_code}: {resp.text[:300]}")

out = os.path.join(ROOT, "test.mp3")
with open(out, "wb") as f:
    f.write(resp.content)
print(f"OK: {len(resp.content)} Bytes Audio -> {out}")
