"""
Hintergrundarbeiten der Zentrale (laufen nur im echten Jarvis auf Port 8340):
- Rechnungs-Anhaenge aus dem Postfach rechnung@ in den Rechnungsordner holen
- bezahlte Rechnungen nach Monatsende in den DATEV-Monatsordner verschieben
- Morgen-Briefing per Telegram
Gelesen wird nur (EXAMINE / BODY.PEEK): keine Mail wird markiert, verschoben oder geloescht.
"""

import asyncio
import email
import imaplib
import json
import os
import re
import shutil
import tempfile
from datetime import date, datetime, timedelta

import dokumente_tools
import rechnungen_tools
from mail_tools import _decode

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "zentrale-jobs.json")
DATEV_ROOT = os.path.normpath(os.path.join(HERE, "..", "..", "..", "DATEV _ Hochladen"))


def _state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(s: dict):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


# ---------- Rechnungen aus rechnung@ ----------

def _invoice_account(config: dict) -> dict | None:
    want = config.get("rechnungen_postfach", "rechnung@")
    for a in config.get("mail_accounts", []):
        if a.get("email", "").lower().startswith(want.lower().rstrip("@") + "@") and a.get("password"):
            return a
    return None


def _new_attachments(acc: dict, since: date, done: set) -> list[tuple[str, str, bytes]]:
    """(mail key, file name, bytes) of PDF/image attachments in mails not handled yet."""
    out = []
    with imaplib.IMAP4_SSL(acc["imap_host"], int(acc.get("imap_port", 993)), timeout=30) as imap:
        imap.login(acc.get("user") or acc["email"], acc["password"])
        imap.select("INBOX", readonly=True)
        _, data = imap.uid("SEARCH", None, f'(SINCE "{since.strftime("%d-%b-%Y")}")')
        for uid in data[0].split():
            key = f"{acc['email']}:{uid.decode()}"
            if key in done:
                continue
            _, msg_data = imap.uid("FETCH", uid, "(BODY.PEEK[])")
            raw = next((p for p in msg_data or [] if isinstance(p, tuple)), None)
            if not raw:
                continue
            msg = email.message_from_bytes(raw[1])
            found = False
            for part in msg.walk():
                name = _decode(part.get_filename()) if part.get_filename() else ""
                ext = os.path.splitext(name)[1].lower()
                if ext in rechnungen_tools.MEDIA:
                    payload = part.get_payload(decode=True) or b""
                    if 2000 < len(payload) < dokumente_tools.MAX_BYTES:
                        out.append((key, name, payload))
                        found = True
            if not found:
                out.append((key, "", b""))  # mail without attachment: just remember it
    return out


async def fetch_invoices(ai, config: dict) -> list[str]:
    """Saves new invoice attachments; returns the saved file names."""
    acc = _invoice_account(config)
    if not acc:
        return []
    st = _state()
    if "rechnungen_seit" not in st:  # first run: only mails from today on
        st["rechnungen_seit"] = date.today().isoformat()
        st["rechnungen_mails"] = []
        _save_state(st)
    since = date.fromisoformat(st["rechnungen_seit"])
    done = set(st.get("rechnungen_mails", []))
    items = await asyncio.to_thread(_new_attachments, acc, since, done)
    saved = []
    for key, name, data in items:
        if data:
            fd, tmp = tempfile.mkstemp(suffix=os.path.splitext(name)[1])
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            try:
                kind, _ = await dokumente_tools.classify(ai, tmp)
            except Exception:
                kind = None  # Claude not reachable: try again next time
            finally:
                os.remove(tmp)
            if kind is None:
                continue
            if kind == "rechnung":
                path = dokumente_tools.unique_path(rechnungen_tools.folder(config), dokumente_tools.safe_name(name))
                with open(path, "wb") as f:
                    f.write(data)
                saved.append(os.path.basename(path))
        done.add(key)
    st = _state()
    st["rechnungen_mails"] = sorted(done)[-2000:]
    st["rechnungen_letzter_abruf"] = datetime.now().isoformat(timespec="minutes")
    _save_state(st)
    if saved:
        print(f"[zentrale] Rechnungen aus rechnung@ abgelegt: {', '.join(saved)}", flush=True)
    return saved


# ---------- Bezahlte Rechnungen in den DATEV-Ordner ----------

def move_paid_to_datev(config: dict) -> list[str]:
    """Files named ..._Bezahlt_TTMMJJ from earlier months go to DATEV _ Hochladen/JJJJ/MM_JJJJ/."""
    folder = rechnungen_tools.folder(config)
    root = config.get("datev_ordner") or DATEV_ROOT
    if not os.path.isdir(root):
        return []
    this_month = date.today().strftime("%Y-%m")
    moved = []
    for name in rechnungen_tools.list_files(config):
        m = rechnungen_tools.PAID_RE.search(name)
        if not m:
            continue
        year, month = f"20{m.group(3)}", m.group(2)
        if f"{year}-{month}" >= this_month:
            continue
        target_dir = os.path.join(root, year, f"{month}_{year}")
        os.makedirs(target_dir, exist_ok=True)
        target = dokumente_tools.unique_path(target_dir, name)
        shutil.move(os.path.join(folder, name), target)
        moved.append(f"{name} -> {year}/{month}_{year}")
    if moved:
        print(f"[zentrale] In DATEV-Ordner verschoben: {'; '.join(moved)}", flush=True)
    return moved


# ---------- Morgen-Briefing ----------

def _money(v) -> str:
    return f"{v:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


async def briefing_text(ai, config: dict, todoist_get, staffo_tools, zac_mail_mcp) -> str:
    today = date.today()
    wd = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"][today.weekday()]
    lines = [f"Guten Morgen, Julian. Dein Überblick für {wd}, {today.strftime('%d.%m.')}"]

    try:
        tasks = await asyncio.to_thread(todoist_get, "tasks/filter", {"query": "today | overdue"})
        timed = sorted((t for t in tasks if "T" in ((t.get("due") or {}).get("date") or "")),
                       key=lambda t: t["due"]["date"])
        todo = [t for t in tasks if "T" not in ((t.get("due") or {}).get("date") or "")]
        lines.append("\n📅 Termine heute:" if timed else "\n📅 Heute keine Termine.")
        for t in timed:
            d = t["due"]["date"]
            late = d[:10] < today.isoformat()
            lines.append(f"• {d[11:16]} {t['content']}" + (" (überfällig)" if late else ""))
        if todo:
            lines.append(f"\n✅ Aufgaben fällig/überfällig: {len(todo)}")
            for t in sorted(todo, key=lambda t: t.get("priority", 1), reverse=True)[:6]:
                lines.append(f"• {t['content']}")
    except Exception as e:
        lines.append(f"\nTodoist nicht erreichbar ({type(e).__name__}).")

    try:
        inv = (await rechnungen_tools.invoices(ai, config))["rechnungen"]
        due = []
        for r in inv:
            if r.get("status") == "bezahlt" or not r.get("faellig"):
                continue
            days = (date.fromisoformat(r["faellig"]) - today).days
            if days <= 7:
                due.append((days, r))
        if due:
            lines.append("\n💶 Zahlungen fällig (≤ 7 Tage):")
            for days, r in sorted(due, key=lambda x: x[0]):
                when = f"{-days} T überfällig" if days < 0 else "heute" if days == 0 else f"in {days} T"
                amount = _money(r["betrag"]) if r.get("betrag") is not None else "–"
                lines.append(f"• {r.get('glaeubiger', '?')} · {amount} · {when}")
    except Exception as e:
        lines.append(f"\nRechnungen nicht lesbar ({type(e).__name__}).")

    try:
        heute = await asyncio.to_thread(staffo_tools.shifts, today, today)
        offen = await asyncio.to_thread(staffo_tools.open_shifts, today, today + timedelta(days=6))
        lines.append(f"\n👮 Heute im Dienst: {len(heute)} Schichten")
        if offen:
            lines.append(f"⚠️ Offene Schichten in den nächsten 7 Tagen: {len(offen)}")
            for s in offen[:5]:
                lines.append(f"• {staffo_tools._when(s)} {s.get('standort', '')} {s.get('bereich', '')} – {s['offen']} offen".replace("  ", " "))
    except Exception as e:
        lines.append(f"\nStaffomatic nicht erreichbar ({type(e).__name__}).")

    try:
        d = json.loads(await asyncio.to_thread(zac_mail_mcp.list_mails, "alle", 2, True, 50))
        mails = d.get("mails", [])
        if mails:
            per = {}
            for m in mails:
                per[m["konto"].split("@")[0]] = per.get(m["konto"].split("@")[0], 0) + 1
            lines.append(f"\n✉️ Ungelesene Mails (2 Tage): {len(mails)} · " + ", ".join(f"{k} {v}" for k, v in per.items()))
        else:
            lines.append("\n✉️ Keine ungelesenen Mails.")
    except Exception as e:
        lines.append(f"\nMails nicht lesbar ({type(e).__name__}).")

    return "\n".join(lines)


async def send_telegram(token: str, chat_ids: list, text: str):
    import httpx
    async with httpx.AsyncClient(timeout=20) as http:
        for cid in chat_ids:
            await http.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": cid, "text": text[:4000]})


# ---------- Taktgeber ----------

async def run(ai, config: dict, todoist_get, staffo_tools, zac_mail_mcp):
    """Runs forever inside the Jarvis server."""
    hhmm = config.get("briefing_uhrzeit", "07:00")
    token = config.get("telegram_bot_token", "")
    chats = config.get("briefing_chat_ids") or config.get("telegram_allowed_ids", [])[:1]
    every = int(config.get("rechnungen_abruf_minuten", 15)) * 60
    last_fetch = 0.0
    await asyncio.sleep(20)
    while True:
        now = datetime.now()
        try:
            if now.timestamp() - last_fetch >= every:
                last_fetch = now.timestamp()
                await fetch_invoices(ai, config)
                import buchhaltung_db
                await buchhaltung_db.match(ai, config)
        except Exception as e:
            print(f"[zentrale] Rechnungsabruf fehlgeschlagen: {e}", flush=True)
        try:
            st = _state()
            if st.get("datev_geprueft") != now.strftime("%Y-%m"):
                await asyncio.to_thread(move_paid_to_datev, config)
                st = _state(); st["datev_geprueft"] = now.strftime("%Y-%m"); _save_state(st)
        except Exception as e:
            print(f"[zentrale] DATEV-Ablage fehlgeschlagen: {e}", flush=True)
        try:
            st = _state()
            if (token and chats and now.strftime("%H:%M") >= hhmm and now.hour < 12
                    and st.get("briefing_gesendet") != now.date().isoformat()):
                text = await briefing_text(ai, config, todoist_get, staffo_tools, zac_mail_mcp)
                await send_telegram(token, chats, text)
                st = _state(); st["briefing_gesendet"] = now.date().isoformat(); _save_state(st)
                print("[zentrale] Morgen-Briefing gesendet", flush=True)
        except Exception as e:
            print(f"[zentrale] Briefing fehlgeschlagen: {e}", flush=True)
        await asyncio.sleep(60)
