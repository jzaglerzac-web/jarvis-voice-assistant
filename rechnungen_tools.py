"""
Rechnungen fuer die Buchhaltung der Zentrale.
Liest PDFs und Bilder aus dem Rechnungsordner, laesst Claude die Rechnungsdaten
entnehmen und merkt sich das Ergebnis je Datei, damit jede Rechnung nur einmal
ausgelesen wird. Bezahlt heisst: Dateiname endet auf _Bezahlt_TTMMJJ.
"""

import asyncio
import base64
import json
import os
import re
from datetime import date, timedelta

HERE = os.path.dirname(__file__)
CACHE_FILE = os.path.join(HERE, "rechnungen-cache.json")
DEFAULT_FOLDER = os.path.normpath(os.path.join(HERE, "..", "..", "Unterlagen", "Rechnungen für Jarvis"))
# Ausgangsrechnungen (eigene Rechnungen an Kunden) fuer "zu erwartende Einnahmen"
OUT_FOLDER = os.path.normpath(os.path.join(HERE, "..", "..", "Unterlagen", "Ausgangsrechnungen für Jarvis"))
OUT_CACHE_FILE = os.path.join(HERE, "ausgangsrechnungen-cache.json")
PAID_RE = re.compile(r"_Bezahlt_(\d{2})(\d{2})(\d{2})(?=\.[^.]+$)", re.I)
MEDIA = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}

_lock = asyncio.Lock()

SCHEMA = {
    "type": "object",
    "properties": {
        "typ": {"type": "string", "enum": ["eingang", "ausgang"],
                "description": "eingang = Rechnung an ZAC (ZAC muss zahlen), ausgang = Rechnung von ZAC an einen Kunden"},
        "glaeubiger": {"type": "string", "description": "Rechnungssteller (Firmenname, kurz)"},
        "glaeubiger_adresse": {"type": "string"},
        "empfaenger": {"type": "string", "description": "Rechnungsempfaenger"},
        "nummer": {"type": "string", "description": "Rechnungsnummer"},
        "kundennummer": {"type": "string"},
        "rechnungsdatum": {"type": "string", "description": "JJJJ-MM-TT"},
        "leistungszeitraum": {"type": "string", "description": "Leistungsdatum oder -zeitraum, wie auf der Rechnung"},
        "faellig": {"type": "string", "description": "Faelligkeitsdatum JJJJ-MM-TT; aus Zahlungsziel berechnen, bei 'sofort' das Rechnungsdatum, sonst leer"},
        "zahlungsziel": {"type": "string", "description": "Zahlungsbedingung im Wortlaut, z. B. '14 Tage netto'"},
        "zahlungsart": {"type": "string", "description": "z. B. Ueberweisung, Lastschrift/Bankeinzug, bereits bezahlt"},
        "skonto": {"type": "string", "description": "Skonto-Bedingung, falls vorhanden"},
        "netto": {"type": "number"},
        "ust_satz": {"type": "string", "description": "z. B. '19 %' oder '19 % / 7 %'"},
        "ust_betrag": {"type": "number"},
        "betrag": {"type": "number", "description": "Bruttobetrag bzw. Zahlbetrag"},
        "waehrung": {"type": "string", "description": "ISO-Code, meist EUR"},
        "iban": {"type": "string", "description": "IBAN des Rechnungsstellers"},
        "bic": {"type": "string"},
        "bank": {"type": "string"},
        "verwendungszweck": {"type": "string", "description": "Angegebener Verwendungszweck, sonst Rechnungsnummer"},
        "ust_id": {"type": "string", "description": "USt-IdNr. oder Steuernummer des Rechnungsstellers"},
        "beschreibung": {"type": "string", "description": "Worum es geht, ein kurzer Satz"},
        "positionen": {"type": "array", "items": {"type": "object", "properties": {
            "text": {"type": "string"}, "menge": {"type": "string"}, "betrag": {"type": "number"}}}},
        "bereits_bezahlt": {"type": "boolean", "description": "true, wenn die Rechnung als bezahlt/abgebucht ausgewiesen ist"},
    },
    "required": ["typ", "glaeubiger", "nummer", "rechnungsdatum", "betrag", "waehrung"],
}

PROMPT = ("Entnimm dieser Rechnung alle wichtigen Daten fuer die Buchhaltung der ZAC GmbH "
          "(ZAC Sicherheitsservice). Felder, die nicht auf der Rechnung stehen, weglassen. "
          "Datumsangaben als JJJJ-MM-TT, Betraege als Zahl. Heute ist {heute}. "
          "Gib das Ergebnis ausschliesslich ueber das Werkzeug rechnung zurueck.")


def folder(config: dict, kind: str = "eingang") -> str:
    if kind == "ausgang":
        return config.get("ausgangsrechnungen_ordner") or OUT_FOLDER
    return config.get("rechnungen_ordner") or DEFAULT_FOLDER


def _cache_file(kind: str) -> str:
    return OUT_CACHE_FILE if kind == "ausgang" else CACHE_FILE


def _load(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(path: str, data: dict):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def list_files(config: dict, kind: str = "eingang") -> list[str]:
    d = folder(config, kind)
    if not os.path.isdir(d):
        return []
    return sorted(n for n in os.listdir(d)
                  if os.path.splitext(n)[1].lower() in MEDIA and os.path.isfile(os.path.join(d, n)))


def file_path(config: dict, name: str, kind: str = "eingang") -> str | None:
    """Path of a file in the invoice folder, only for names that are really listed there."""
    return os.path.join(folder(config, kind), name) if name in list_files(config, kind) else None


async def _extract(ai, model: str, path: str) -> dict:
    ext = os.path.splitext(path)[1].lower()
    with open(path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode()
    kind = "document" if ext == ".pdf" else "image"
    r = await ai.messages.create(
        model=model, max_tokens=2000,
        tools=[{"name": "rechnung", "description": "Rechnungsdaten speichern", "input_schema": SCHEMA}],
        messages=[{"role": "user", "content": [
            {"type": kind, "source": {"type": "base64", "media_type": MEDIA[ext], "data": data}},
            {"type": "text", "text": PROMPT.format(heute=date.today().isoformat())},
        ]}],
    )
    for b in r.content:
        if b.type == "tool_use":
            return dict(b.input)
    text = "".join(b.text for b in r.content if b.type == "text")
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start:end + 1])
    raise ValueError("keine Daten erhalten")


def _fill_due(x: dict):
    """Due date from 'x Tage' when Claude left it empty."""
    if x.get("faellig") or not x.get("rechnungsdatum"):
        return
    m = re.search(r"(\d+)\s*Tag", x.get("zahlungsziel") or "")
    try:
        d = date.fromisoformat(x["rechnungsdatum"])
    except ValueError:
        return
    if m:
        x["faellig"] = (d + timedelta(days=int(m.group(1)))).isoformat()
    elif "sofort" in (x.get("zahlungsziel") or "").lower():
        x["faellig"] = d.isoformat()


async def invoices(ai, config: dict, kind: str = "eingang") -> dict:
    """All invoices of the folder; new or changed files are read by Claude first."""
    model = config.get("rechnungen_model", "claude-sonnet-5-5")
    async with _lock:
        cache = _load(_cache_file(kind))
        names = list_files(config, kind)
        todo = []
        for n in names:
            st = os.stat(os.path.join(folder(config, kind), n))
            key = f"{int(st.st_mtime)}:{st.st_size}"
            if cache.get(n, {}).get("_key") != key:
                todo.append((n, key))

        async def one(n, key):
            try:
                x = await _extract(ai, model, os.path.join(folder(config, kind), n))
                _fill_due(x)
                x["_key"] = key
            except Exception as e:  # keep the file listed, retry on next load
                x = {"_fehler": str(e)[:200]}
            return n, x

        for n, x in await asyncio.gather(*(one(n, k) for n, k in todo)):
            cache[n] = x
        cache = {n: v for n, v in cache.items() if n in names}
        if todo or len(cache) != len(_load(_cache_file(kind))):
            _save(_cache_file(kind), cache)

    out = []
    for n in names:
        x = {k: v for k, v in cache.get(n, {}).items() if k != "_key"}
        x["datei"] = n
        if kind == "ausgang":
            x["typ"] = "ausgang"
        x.setdefault("typ", "eingang")
        m = PAID_RE.search(n)
        if m:
            x["status"], x["bezahltAm"] = "bezahlt", f"20{m.group(3)}-{m.group(2)}-{m.group(1)}"
        elif x.get("bereits_bezahlt"):
            x["status"] = "bezahlt"
        else:
            x["status"] = "offen"
        out.append(x)
    return {"rechnungen": out, "quelle": folder(config, kind)}


async def set_paid(config: dict, name: str, paid: bool, kind: str = "eingang", paid_on: date | None = None) -> str | None:
    """Marks an invoice as paid by renaming the file to <name>_Bezahlt_TTMMJJ<ext>;
    back to open removes that suffix again. Returns the new file name."""
    if name not in list_files(config, kind):
        return None
    stem, ext = os.path.splitext(PAID_RE.sub("", name))
    new = f"{stem}_Bezahlt_{(paid_on or date.today()):%d%m%y}{ext}" if paid else stem + ext
    if new == name:
        return name
    d = folder(config, kind)
    if os.path.exists(os.path.join(d, new)):
        raise FileExistsError(new)
    async with _lock:
        os.rename(os.path.join(d, name), os.path.join(d, new))
        cache = _load(_cache_file(kind))
        if name in cache:
            cache[new] = cache.pop(name)
            _save(_cache_file(kind), cache)
    return new
