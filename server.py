"""
Jarvis V2 — Voice AI Server
FastAPI backend: receives speech text, thinks with Claude Haiku,
speaks with ElevenLabs, controls browser with Playwright.
"""

import asyncio
import base64
import json
import os
import re
import subprocess
import time
from datetime import date, datetime, timedelta

import anthropic
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

# Load config
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    config = json.load(f)

ANTHROPIC_API_KEY = config["anthropic_api_key"]
ELEVENLABS_API_KEY = config["elevenlabs_api_key"]
ELEVENLABS_VOICE_ID = config.get("elevenlabs_voice_id", "rDmv3mOhK6TnhYWckFaD")
USER_NAME = config.get("user_name", "Julian")
USER_ADDRESS = config.get("user_address", "Sir")
USER_ROLE = config.get("user_role", "KI-Berater und Automatisierungsexperte")
CITY = config.get("city", "Hamburg")
# Nur lokal erreichbar; "0.0.0.0" setzen, um Jarvis bewusst im Netzwerk freizugeben
HOST = config.get("host", "127.0.0.1")
# Programs Jarvis may launch: {"name": "Windows AppID from Get-StartApps"}
PROGRAMS = {k.lower(): v for k, v in config.get("programs", {}).items()}
TASKS_FILE = config.get("obsidian_inbox_path", "")
TODOIST_TOKEN = config.get("todoist_api_token", "")
if "YOUR_" in TODOIST_TOKEN:
    TODOIST_TOKEN = ""
# Only tasks that are overdue or due within this many hours are read out
TASK_WINDOW_HOURS = config.get("task_window_hours", 5)
# Spotify volume (0-1) while Jarvis is awake / in sleep mode
MUSIC_VOLUME = {"active": 0.15, "sleep": 0.25, **config.get("music_volume", {})}
# Website opened in its own browser window on "Jarvis activate"
ACTIVATE_URL = config.get("activate_url", "")
# launch-session.ps1 opens and places that window itself
if os.environ.get("JARVIS_LAUNCH_SESSION"):
    ACTIVATE_URL = ""

ai = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
http = httpx.AsyncClient(timeout=30)

app = FastAPI()

import browser_tools
import screen_capture


LOCATION_PS = (
    "Add-Type -AssemblyName System.Device;"
    "$w=New-Object System.Device.Location.GeoCoordinateWatcher;$w.Start();$i=0;"
    "while(($w.Status -ne 'Ready') -and $i -lt 25){Start-Sleep -Milliseconds 200;$i++};"
    "$c=$w.Position.Location;if(-not $c.IsUnknown){"
    "'{0},{1}' -f $c.Latitude.ToString([cultureinfo]::InvariantCulture),$c.Longitude.ToString([cultureinfo]::InvariantCulture)}"
)


def get_device_location():
    """Current 'lat,lon' from Windows location services, or None."""
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", LOCATION_PS],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        return out if re.fullmatch(r"-?[\d.]+,-?[\d.]+", out) else None
    except Exception:
        return None


def get_weather_sync():
    """Fetch raw weather data. city "auto" uses the device's current location."""
    import urllib.parse
    import urllib.request
    place = CITY
    if CITY.lower() == "auto":
        place = get_device_location() or ""  # empty lets wttr.in fall back to IP location
    try:
        req = urllib.request.Request(f"https://wttr.in/{urllib.parse.quote(place)}?format=j1&lang=de", headers={"User-Agent": "curl"})
        resp = urllib.request.urlopen(req, timeout=8)
        data = json.loads(resp.read())
        c = data["current_condition"][0]
        area = data.get("nearest_area", [{}])[0].get("areaName", [{}])[0].get("value", "")
        return {
            "place": area if CITY.lower() == "auto" else CITY,
            "temp": c["temp_C"],
            "feels_like": c["FeelsLikeC"],
            "description": c.get("lang_de", c["weatherDesc"])[0]["value"],
            "humidity": c["humidity"],
            "wind_kmh": c["windspeedKmph"],
        }
    except Exception:
        return None


def is_task_urgent(due: str) -> bool:
    """True if a due date/time is overdue or within TASK_WINDOW_HOURS.
    A date without a time counts as due on that whole day."""
    now = datetime.now()
    try:
        if "T" in due:
            dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
            if dt.tzinfo:
                dt = dt.astimezone().replace(tzinfo=None)
            return dt <= now + timedelta(hours=TASK_WINDOW_HOURS)
        return date.fromisoformat(due[:10]) <= now.date()
    except ValueError:
        return False


OBSIDIAN_DUE = re.compile(r"(?:📅|due::?)\s*(\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2})?)")


def get_obsidian_tasks():
    """Open '- [ ]' tasks from Tasks.md that carry an urgent due date (📅 2026-10-07 or 📅 2026-10-07 14:00)."""
    if not TASKS_FILE:
        return []
    try:
        with open(os.path.join(TASKS_FILE, "Tasks.md"), "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    tasks = []
    for line in lines:
        line = line.strip()
        if not line.startswith("- [ ]"):
            continue
        m = OBSIDIAN_DUE.search(line)
        if m and is_task_urgent(m.group(1).replace(" ", "T")):
            tasks.append(OBSIDIAN_DUE.sub("", line.replace("- [ ]", "")).strip())
    return tasks


def get_todoist_tasks():
    """Overdue tasks and tasks due within the window from Todoist."""
    if not TODOIST_TOKEN:
        return []
    import urllib.parse
    import urllib.request
    query = urllib.parse.quote("overdue | today | tomorrow")
    req = urllib.request.Request(
        f"https://api.todoist.com/api/v1/tasks/filter?query={query}&limit=200",
        headers={"Authorization": f"Bearer {TODOIST_TOKEN}"},
    )
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=8).read())
    except Exception as e:
        print(f"[jarvis] Todoist Fehler: {e}", flush=True)
        return []
    tasks = []
    for t in data.get("results", []):
        due = t.get("due") or {}
        if due.get("date") and is_task_urgent(due["date"]):
            tasks.append(t["content"])
    return tasks


def get_tasks_sync():
    """Urgent tasks from Obsidian and Todoist (sync)."""
    return get_obsidian_tasks() + get_todoist_tasks()


def refresh_data():
    """Refresh weather and tasks."""
    global WEATHER_INFO, TASKS_INFO
    WEATHER_INFO = get_weather_sync()
    TASKS_INFO = get_tasks_sync()
    print(f"[jarvis] Wetter: {WEATHER_INFO}", flush=True)
    print(f"[jarvis] Tasks: {len(TASKS_INFO)} geladen", flush=True)

WEATHER_INFO = ""
TASKS_INFO = []
refresh_data()

# Action parsing
ACTION_PATTERN = re.compile(r'\[ACTION:(\w+)\]\s*(.*?)$', re.DOTALL | re.MULTILINE)

conversations: dict[str, list] = {}

def build_system_prompt():
    weather_block = ""
    if WEATHER_INFO:
        w = WEATHER_INFO
        weather_block = f"\nWetter {w['place']}: {w['temp']}°C, gefuehlt {w['feels_like']}°C, {w['description']}"

    task_block = f"\nDringende Aufgaben (ueberfaellig oder in den naechsten {TASK_WINDOW_HOURS} Stunden faellig): keine"
    if TASKS_INFO:
        task_block = f"\nDringende Aufgaben (ueberfaellig oder in den naechsten {TASK_WINDOW_HOURS} Stunden faellig, {len(TASKS_INFO)}): " + ", ".join(TASKS_INFO[:8])

    return f"""Du bist Jarvis, der KI-Assistent von Tony Stark aus Iron Man. Dein Dienstherr ist {USER_NAME}, {USER_ROLE}. Du sprichst ausschliesslich Deutsch. {USER_NAME} moechte mit "{USER_ADDRESS}" angesprochen und gesiezt werden. Nutze "Sie" als Pronomen — FALSCH: "{USER_ADDRESS} planen", RICHTIG: "Sie planen, {USER_ADDRESS}". Dein Ton ist trocken, sarkastisch und britisch-hoeflich - wie ein Butler der alles gesehen hat und trotzdem loyal bleibt. Du machst subtile, trockene Bemerkungen, bist aber niemals respektlos. Wenn {USER_ADDRESS} eine offensichtliche Frage stellt, darfst du mit elegantem Sarkasmus antworten. Du bist hochintelligent, effizient und immer einen Schritt voraus. Halte dich KURZ: in der Regel ein Satz, hoechstens zwei. Keine Aufzaehlungen, keine Wiederholungen, kein Smalltalk. Nur laenger, wenn ausdruecklich danach gefragt wird. Du kommentierst fragwuerdige Entscheidungen hoeflich aber spitz.

WICHTIG: Schreibe NIEMALS Regieanweisungen, Emotionen oder Tags in eckigen Klammern wie [sarcastic] [formal] [amused] [dry] oder aehnliches. Dein Sarkasmus muss REIN durch die Wortwahl kommen. Alles was du schreibst wird laut vorgelesen.

Du hast die volle Kontrolle ueber den Browser von {USER_NAME}. Du kannst im Internet suchen, Webseiten oeffnen und den Bildschirm sehen. Wenn {USER_ADDRESS} dich bittet etwas nachzuschauen, zu recherchieren, zu googeln, eine Seite zu oeffnen, oder irgendetwas im Internet zu tun — nutze IMMER eine Aktion. Frag nicht ob du es tun sollst, tu es einfach.

AKTIONEN - Schreibe die passende Aktion ans ENDE deiner Antwort. Der Text VOR der Aktion wird vorgelesen, die Aktion selbst wird still ausgefuehrt.
[ACTION:SEARCH] suchbegriff - Internet durchsuchen und Ergebnisse zusammenfassen
[ACTION:OPEN] url - URL im Browser oeffnen
[ACTION:BROWSE] url - Webseite lesen und Inhalt zusammenfassen
[ACTION:SCREEN] - Bildschirm ansehen und beschreiben. WICHTIG: Bei SCREEN schreibe NUR die Aktion, KEINEN Text davor. Also NUR "[ACTION:SCREEN]" und sonst nichts.
[ACTION:NEWS] - Aktuelle Weltnachrichten abrufen. Nutze diese Aktion wenn nach News, Nachrichten, was in der Welt passiert, aktuelle Lage oder Weltgeschehen gefragt wird. Schreibe einen kurzen Satz davor wie "Ich schaue nach den aktuellen Nachrichten."
[ACTION:APP] programmname - Ein Programm auf dem PC starten. Erlaubte Namen (exakt so schreiben): {", ".join(PROGRAMS) or "keine"}. Fuer andere Programme sage, dass sie nicht freigegeben sind.

WENN {USER_NAME} "Jarvis activate" sagt:
- Begruesse ihn passend zur Tageszeit (aktuelle Zeit: {{time}}).
- Nenne das Wetter in wenigen Worten (Temperatur und Himmel).
- Nenne nur die Anzahl der dringenden Aufgaben und die wichtigste davon.
- Die gesamte Begruessung hat hoechstens zwei kurze Saetze.

=== AKTUELLE DATEN ==={weather_block}{task_block}
==="""


def get_system_prompt():
    weekday = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"][datetime.now().weekday()]
    return build_system_prompt().replace("{time}", f"{weekday}, {time.strftime('%d.%m.%Y %H:%M')}")


def extract_action(text: str):
    match = ACTION_PATTERN.search(text)
    if match:
        clean = text[:match.start()].strip()
        return clean, {"type": match.group(1), "payload": match.group(2).strip()}
    return text, None


async def synthesize_speech(text: str) -> bytes:
    if not text.strip():
        return b""

    # Split long text into chunks at sentence boundaries to avoid ElevenLabs cutoff
    chunks = []
    if len(text) > 250:
        sentences = re.split(r'(?<=[.!?])\s+', text)
        current = ""
        for s in sentences:
            if len(current) + len(s) > 250 and current:
                chunks.append(current.strip())
                current = s
            else:
                current = (current + " " + s).strip()
        if current:
            chunks.append(current.strip())
    else:
        chunks = [text]

    audio_parts = []
    for chunk in chunks:
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
        try:
            resp = await http.post(url, headers={
                "xi-api-key": ELEVENLABS_API_KEY,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            }, json={
                "text": chunk,
                "model_id": "eleven_turbo_v2_5",
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.85},
            })
            print(f"  TTS chunk status: {resp.status_code}, size: {len(resp.content)}", flush=True)
            if resp.status_code == 200:
                audio_parts.append(resp.content)
            else:
                print(f"  TTS error body: {resp.text[:200]}", flush=True)
        except Exception as e:
            print(f"  TTS EXCEPTION: {e}", flush=True)

    return b"".join(audio_parts)


async def execute_action(action: dict) -> str:
    t = action["type"]
    p = action["payload"]

    if t == "SEARCH":
        result = await browser_tools.search_and_read(p)
        if "error" not in result:
            return f"Seite: {result.get('title', '')}\nURL: {result.get('url', '')}\n\n{result.get('content', '')[:2000]}"
        return f"Suche fehlgeschlagen: {result.get('error', '')}"

    elif t == "BROWSE":
        result = await browser_tools.visit(p)
        if "error" not in result:
            return f"Seite: {result.get('title', '')}\n\n{result.get('content', '')[:2000]}"
        return f"Seite nicht erreichbar: {result.get('error', '')}"

    elif t == "OPEN":
        await browser_tools.open_url(p)
        return f"Geoeffnet: {p}"

    elif t == "SCREEN":
        return await screen_capture.describe_screen(ai)

    elif t == "NEWS":
        result = await browser_tools.fetch_news()
        return result

    elif t == "APP":
        app_id = PROGRAMS.get(p.strip().lower())
        if not app_id:
            return f"Programm fehlgeschlagen: {p} ist nicht freigegeben"
        # Launch through the Start-menu AppsFolder so Store and desktop apps both work
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app_id}"])
        return f"Geoeffnet: {p}"

    return ""


# "ZAC aus" ends Jarvis; speech recognition may also write Zack/Zak
SHUTDOWN_PATTERN = re.compile(r"\b(zac|zack|zak|z\.?\s?a\.?\s?c\.?)\s*aus\b", re.IGNORECASE)
activate_url_opened = False


def open_activate_url():
    """Open the activation website once per server run in its own Chrome window."""
    global activate_url_opened
    if not ACTIVATE_URL or activate_url_opened:
        return
    activate_url_opened = True
    subprocess.Popen(["cmd", "/c", "start", "", "chrome", "--new-window", ACTIVATE_URL])


async def shutdown_jarvis(ws: WebSocket):
    """Say goodbye, tell the UI to stop, then end the server process."""
    print("[jarvis] Shutdown-Kommando erhalten", flush=True)
    text = f"Sehr wohl, {USER_ADDRESS}. Jarvis wird beendet."
    audio = await synthesize_speech(text)
    await ws.send_json({"type": "response", "text": text,
                        "audio": base64.b64encode(audio).decode("utf-8") if audio else ""})
    await ws.send_json({"type": "shutdown"})
    asyncio.get_running_loop().call_later(2, os._exit, 0)


async def process_message(session_id: str, user_text: str, ws: WebSocket):
    """Process message and send responses via WebSocket."""
    if session_id not in conversations:
        conversations[session_id] = []

    # Refresh weather + tasks on activate
    if SHUTDOWN_PATTERN.search(user_text):
        await shutdown_jarvis(ws)
        return

    if "activate" in user_text.lower():
        refresh_data()
        open_activate_url()

    conversations[session_id].append({"role": "user", "content": user_text})
    history = conversations[session_id][-16:]

    # LLM call
    response = await ai.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system=get_system_prompt(),
        messages=history,
    )
    reply = response.content[0].text
    print(f"  LLM raw: {reply[:200]}", flush=True)
    spoken_text, action = extract_action(reply)

    # Speak the main response immediately
    if spoken_text:
        audio = await synthesize_speech(spoken_text)
        print(f"  Jarvis: {spoken_text[:80]}", flush=True)
        print(f"  Audio bytes: {len(audio)}", flush=True)
        conversations[session_id].append({"role": "assistant", "content": spoken_text})
        await ws.send_json({
            "type": "response",
            "text": spoken_text,
            "audio": base64.b64encode(audio).decode("utf-8") if audio else "",
        })

    # Execute action if any
    if action:
        print(f"  Action: {action['type']} -> {action['payload'][:100]}", flush=True)

        # Quick voice feedback for SCREEN so user knows Jarvis is working
        if action["type"] == "SCREEN":
            hint = "Lassen Sie mich einen Blick auf Ihren Bildschirm werfen."
            hint_audio = await synthesize_speech(hint)
            await ws.send_json({
                "type": "response",
                "text": hint,
                "audio": base64.b64encode(hint_audio).decode("utf-8") if hint_audio else "",
            })

        try:
            action_result = await execute_action(action)
            print(f"  Result: {action_result}", flush=True)
        except Exception as e:
            print(f"  Action error: {e}", flush=True)
            action_result = f"Fehler: {e}"

        if action["type"] in ("OPEN", "APP") and "fehlgeschlagen" not in action_result:
            # Just opened browser, nothing to summarize
            return

        # SEARCH, BROWSE, SCREEN — summarize results
        if action_result and "fehlgeschlagen" not in action_result:
            summary_resp = await ai.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=250,
                system=f"Du bist Jarvis. Fasse die folgenden Informationen KURZ auf Deutsch zusammen, maximal 2 kurze Saetze, im Jarvis-Stil. Sprich den Nutzer als {USER_ADDRESS} an. KEINE Tags in eckigen Klammern. KEINE ACTION-Tags.",
                messages=[{"role": "user", "content": f"Fasse zusammen:\n\n{action_result}"}],
            )
            summary = summary_resp.content[0].text
            summary, _ = extract_action(summary)
        else:
            summary = f"Das hat leider nicht funktioniert, {USER_ADDRESS}."

        audio2 = await synthesize_speech(summary)
        conversations[session_id].append({"role": "assistant", "content": summary})
        await ws.send_json({
            "type": "response",
            "text": summary,
            "audio": base64.b64encode(audio2).decode("utf-8") if audio2 else "",
        })


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    session_id = str(id(ws))
    print(f"[jarvis] Client connected", flush=True)

    try:
        while True:
            data = await ws.receive_json()
            user_text = data.get("text", "").strip()
            if not user_text:
                continue

            print(f"  You:    {user_text}", flush=True)
            await process_message(session_id, user_text, ws)

    except WebSocketDisconnect:
        conversations.pop(session_id, None)


app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "frontend")), name="static")


def set_app_volume(process_name: str, level: float) -> bool:
    """Set the Windows mixer volume (0-1) of every audio session of a program."""
    try:
        from pycaw.pycaw import AudioUtilities
    except ImportError:
        return False
    found = False
    for s in AudioUtilities.GetAllSessions():
        if s.Process and s.Process.name().lower() == process_name.lower():
            s.SimpleAudioVolume.SetMasterVolume(max(0.0, min(1.0, level)), None)
            found = True
    return found


@app.post("/api/music-volume")
async def music_volume(mode: str):
    """mode "active" while Jarvis is awake, "sleep" in sleep mode (volumes from config)."""
    level = MUSIC_VOLUME.get(mode)
    if level is None:
        return {"ok": False}
    ok = await asyncio.to_thread(set_app_volume, "Spotify.exe", level)
    return {"ok": ok, "level": level}


@app.get("/")
async def serve_index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "frontend", "index.html"))


if __name__ == "__main__":
    import uvicorn
    print("=" * 50, flush=True)
    print("  J.A.R.V.I.S. V2 Server", flush=True)
    print(f"  http://{HOST}:8340", flush=True)
    print("=" * 50, flush=True)
    uvicorn.run(app, host=HOST, port=8340)
