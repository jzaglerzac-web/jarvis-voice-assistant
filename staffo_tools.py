"""
Jarvis — Staffomatic access (API v3, HTTP Basic auth with staffomatic_user /
staffomatic_password from config.json). Reading is free; changes (create a shift,
assign someone, change a note) are only prepared as a draft and run after the
user confirmed it; see pending_staffo in server.py. Credentials never appear in
answers or logs.
"""

import json
import os
import time
from datetime import date, datetime, timedelta

import httpx

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
API = "https://api.staffomaticapp.com/v3"
WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


class StaffoError(Exception):
    pass


def _settings() -> dict:
    """Read fresh each call, so credentials entered later work without a restart."""
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    s = {
        "account": cfg.get("staffomatic_account", ""),
        "user": cfg.get("staffomatic_user", ""),
        "password": cfg.get("staffomatic_password", ""),
    }
    if not s["user"] or not s["password"]:
        raise StaffoError("keine Staffomatic-Zugangsdaten in config.json eingetragen")
    if not s["account"]:
        raise StaffoError("staffomatic_account fehlt in config.json")
    return s


def _request(method: str, path: str, params: dict | None = None, body: dict | None = None):
    s = _settings()
    try:
        resp = httpx.request(
            method, f"{API}/{s['account']}/{path}", params=params, json=body,
            auth=(s["user"], s["password"]), timeout=20,
            headers={"User-Agent": f"Jarvis ZAC ({s['user']})", "Accept": "application/json"},
        )
    except httpx.HTTPError as e:
        raise StaffoError(f"Staffomatic nicht erreichbar: {type(e).__name__}") from None
    if resp.status_code == 401:
        raise StaffoError("Staffomatic-Login abgelehnt (Zugangsdaten pruefen)")
    if resp.status_code >= 400:
        raise StaffoError(f"Staffomatic-Fehler {resp.status_code}: {resp.text[:200]}")
    return resp.json() if resp.content else None


def _get_all(path: str, params: dict | None = None) -> list:
    """Collections come in pages of up to 50; read until a page is empty or short."""
    items, page = [], 1
    while page <= 40:
        batch = _request("GET", path, {**(params or {}), "page": page}) or []
        items.extend(batch)
        if len(batch) < 50:
            break
        page += 1
    return items


_locations_cache: tuple[float, list] = (0.0, [])


def locations() -> list:
    """All locations (Standorte) of the account: [{id, name}], cached for an hour."""
    global _locations_cache
    if time.time() - _locations_cache[0] > 3600 or not _locations_cache[1]:
        locs = [{"id": l["id"], "name": (l.get("name") or f"Standort {l['id']}").strip()}
                for l in _get_all("locations.json")]
        if not locs:
            raise StaffoError("keine Standorte im Staffomatic-Konto gefunden")
        _locations_cache = (time.time(), locs)
    return _locations_cache[1]


def _departments() -> list:
    """Departments of all locations, each with "standort" and "location_id"."""
    result = []
    for loc in locations():
        for d in _get_all(f"locations/{loc['id']}/departments.json"):
            result.append({**d, "location_id": loc["id"], "standort": loc["name"]})
    return result


def _users(state: str) -> dict:
    """Users of all locations by id (someone working at both appears once)."""
    found = {}
    for loc in locations():
        for u in _get_all(f"locations/{loc['id']}/users.json", {"state": state}):
            found.setdefault(u["id"], {**u, "standorte": []})["standorte"].append(loc["name"])
    return found


def _raw_shifts(von: date, bis: date) -> list:
    """Raw shifts of all locations, each with "_standort"."""
    raw = []
    for loc in locations():
        for s in _get_all(f"locations/{loc['id']}/shifts.json", {"from": _iso(von), "until": _iso(bis, end=True)}):
            raw.append({**s, "_standort": loc["name"], "location_id": s.get("location_id") or loc["id"]})
    return raw


def _iso(d: date, end: bool = False) -> str:
    t = datetime.combine(d, datetime.max.time() if end else datetime.min.time()).replace(microsecond=0)
    return t.astimezone().isoformat()


def _name(u: dict) -> str:
    return f"{u.get('first_name') or ''} {u.get('last_name') or ''}".strip() or u.get("email", "") or f"#{u.get('id')}"


def users() -> list:
    """Active staff of all locations: [{id, name, rolle, bereiche, standorte}]."""
    deps = {d["id"]: d.get("name", "").strip() for d in _departments()}
    return [{"id": u["id"], "name": _name(u), "rolle": u.get("role", ""),
             "bereiche": [deps.get(i, str(i)) for i in u.get("department_ids") or []],
             "standorte": u["standorte"]}
            for u in _users("active").values()]


def shifts(von: date, bis: date) -> list:
    """Shifts of all locations from von to bis (inclusive), sorted by start:
    [{id, schedule_id, location_id, standort, beginn, ende, bereich, mitarbeiter, soll, offen, notiz}]."""
    names = {i: _name(u) for i, u in _users("all").items()}
    deps = {d["id"]: d.get("name", "").strip() for d in _departments()}
    result = []
    for s in _raw_shifts(von, bis):
        assigned = s.get("assigned_user_ids") or []
        want = s.get("desired_coverage") or 0
        result.append({
            "id": s["id"],
            "schedule_id": s.get("schedule_id"),
            "location_id": s["location_id"],
            "standort": s["_standort"],
            "beginn": s.get("starts_at", ""),
            "ende": s.get("ends_at", "") if not s.get("open_end") else "",
            "bereich": deps.get(s.get("department_id"), ""),
            "mitarbeiter": [names.get(i, f"#{i}") for i in assigned],
            "soll": want,
            "offen": max(0, want - len(assigned)),
            "notiz": s.get("note") or "",
        })
    result.sort(key=lambda x: x["beginn"])
    return result


def open_shifts(von: date, bis: date) -> list:
    """Shifts with fewer people assigned than wanted."""
    return [s for s in shifts(von, bis) if s["offen"] > 0]


def _when(s: dict) -> str:
    try:
        b = datetime.fromisoformat(s["beginn"])
        e = datetime.fromisoformat(s["ende"]).strftime("%H:%M") if s["ende"] else "open end"
        return f"{WEEKDAYS[b.weekday()]} {b.strftime('%d.%m. %H:%M')}-{e}"
    except ValueError:
        return s["beginn"]


def _range(request: str) -> tuple[date, date]:
    today = date.today()
    if "morgen" in request:
        return today + timedelta(days=1), today + timedelta(days=1)
    if "wochenende" in request:
        sat = today + timedelta(days=(5 - today.weekday()) % 7)
        return sat, sat + timedelta(days=1)
    if "woche" in request:
        return today, today + timedelta(days=6)
    return today, today


def staffo_text(request: str = "") -> str:
    """Answer for Jarvis' STAFFO action. request: heute (default), morgen, woche, wochenende,
    'offen' for open shifts only, 'mitarbeiter' for the staff list."""
    request = request.lower()
    try:
        if "mitarbeiter" in request or "personal" in request:
            staff = users()
            return "\n".join(f"{u['name']} ({u['rolle']}; {', '.join(u['standorte'])}; {', '.join(u['bereiche']) or 'ohne Bereich'})"
                             for u in staff) or "Keine aktiven Mitarbeiter gefunden."
        von, bis = _range(request)
        rows = open_shifts(von, bis) if "offen" in request else shifts(von, bis)
    except StaffoError as e:
        return f"Staffomatic fehlgeschlagen: {e}"
    span = von.strftime("%d.%m.") + ("" if von == bis else f" bis {bis.strftime('%d.%m.')}")
    if not rows:
        return f"Keine {'offenen ' if 'offen' in request else ''}Schichten am {span}."
    return "\n".join(
        f"[Schicht {s['id']}] {_when(s)} {s['standort']} · {s['bereich']}: {', '.join(s['mitarbeiter']) or 'niemand'}"
        + (f" — {s['offen']} offen" if s["offen"] else "") + (f" — Notiz: {s['notiz']}" if s["notiz"] else "")
        for s in rows)


# ---- Changes: only ever after the user confirmed the draft -------------------------

def _find_shift(shift_id) -> dict:
    """A shift by id, looked up in the next and last 60 days at all locations."""
    today = date.today()
    for s in _raw_shifts(today - timedelta(days=60), today + timedelta(days=60)):
        if str(s["id"]) == str(shift_id):
            return s
    raise StaffoError(f"Schicht {shift_id} nicht gefunden")


def _find_by_name(items: list, name: str, label: str) -> dict:
    """Exact name first; otherwise every word of name must appear in _name
    (which may include the location, e.g. "Sondereinsatz Berlin")."""
    key = name.strip().lower()
    hits = [i for i in items if key and key == i["_name"].lower()]
    if not hits:
        words = key.replace("/", " ").split()
        hits = [i for i in items if words and all(w in i["_name"].lower() for w in words)]
    if len(hits) != 1:
        raise StaffoError(f"{label} '{name}' {'nicht gefunden' if not hits else 'nicht eindeutig'}")
    return hits[0]


def prepare_draft(payload: str) -> tuple[dict | None, str]:
    """Check a STAFFO_DRAFT payload and describe it. Returns (draft or None, text for the user).
    aktion "anlegen": bereich, beginn, ende (YYYY-MM-DDTHH:MM), anzahl, notiz
    aktion "zuweisen": schicht_id, mitarbeiter (Name)
    aktion "notiz": schicht_id, notiz"""
    try:
        d = json.loads(payload)
        action = d.get("aktion", "")
        if action == "anlegen":
            deps = [{**x, "_name": f"{x.get('name', '').strip()} ({x['standort']})"} for x in _departments()]
            dep = _find_by_name(deps, d.get("bereich", ""), "Bereich")
            start, end = datetime.fromisoformat(d["beginn"]), datetime.fromisoformat(d["ende"])
            count = int(d.get("anzahl", 1))
            draft = {"aktion": action, "department_id": dep["id"], "location_id": dep["location_id"],
                     "beginn": start, "ende": end, "anzahl": count, "notiz": d.get("notiz", "")}
            text = (f"Neue Schicht: {dep['_name']}, {start.strftime('%d.%m. %H:%M')}-{end.strftime('%H:%M')}, "
                    f"{count} Person(en)" + (f", Notiz: {draft['notiz']}" if draft["notiz"] else ""))
        elif action == "zuweisen":
            shift = _find_shift(d["schicht_id"])
            people = [{**u, "_name": _name(u)} for u in _users("active").values()]
            user = _find_by_name(people, d.get("mitarbeiter", ""), "Mitarbeiter")
            draft = {"aktion": action, "shift": shift, "user_id": user["id"]}
            text = f"{user['_name']} in Schicht {shift['id']} ({_when({'beginn': shift['starts_at'], 'ende': shift['ends_at']})}) eintragen"
        elif action == "notiz":
            shift = _find_shift(d["schicht_id"])
            draft = {"aktion": action, "shift": shift, "notiz": d.get("notiz", "")}
            text = f"Notiz von Schicht {shift['id']} auf „{draft['notiz']}“ setzen"
        else:
            return None, "Staffomatic fehlgeschlagen: unbekannte Aenderung"
    except StaffoError as e:
        return None, f"Staffomatic fehlgeschlagen: {e}"
    except (ValueError, KeyError, TypeError):
        return None, "Staffomatic fehlgeschlagen: Entwurf nicht lesbar"
    return draft, f"Entwurf Staffomatic\n{text}\n\nSoll ich das so eintragen? Antworten Sie mit „ja“. Jede andere Antwort verwirft den Entwurf."


def execute_draft(draft: dict) -> str:
    """Carry out a confirmed draft."""
    if draft["aktion"] == "anlegen":
        start, end = draft["beginn"].astimezone(), draft["ende"].astimezone()
        schedules = _get_all(f"locations/{draft['location_id']}/schedules.json",
                             {"from": _iso(start.date()), "until": _iso(start.date(), end=True)})
        sched = next((s for s in schedules if not s.get("template")
                      and s.get("bop", "") <= start.date().isoformat() <= s.get("eop", "")), None)
        if not sched:
            raise StaffoError(f"kein Dienstplan fuer den {start.strftime('%d.%m.')} angelegt")
        shift = _request("POST", f"schedules/{sched['id']}/shifts.json", body={
            "starts_at": start.isoformat(), "ends_at": end.isoformat(), "location_id": draft["location_id"],
            "department_id": draft["department_id"], "desired_coverage": draft["anzahl"],
            "open_end": False, "note": draft["notiz"]})
        return f"Schicht {shift.get('id', '')} angelegt"
    shift = draft["shift"]
    path = f"schedules/{shift['schedule_id']}/shifts/{shift['id']}.json"
    if draft["aktion"] == "zuweisen":
        ids = list(dict.fromkeys((shift.get("assigned_user_ids") or []) + [draft["user_id"]]))
        _request("PUT", path, body={"assigned_user_ids": ids})
        return f"In Schicht {shift['id']} eingetragen"
    _request("PUT", path, body={"note": draft["notiz"]})
    return f"Notiz von Schicht {shift['id']} geaendert"
