"""
Personal der ZAC GmbH fuer den Reiter „Personal" der Zentrale (SQLite, personal.db).

Was die Datenbank haelt:
- mitarbeiter:    Stammdaten, Stundenlohn, Festgehalt, feste Auszahlungsstunden (Arbeitszeitkonto), Staffomatic-Zuordnung
- monatswerte:    je Mitarbeiter und Monat die Werte von Hand (Spalten ohne Praefix) und aus Staffomatic (staffo_*).
                  Ein Wert von Hand geht immer vor; ist er leer, gilt der Staffomatic-Wert.
- einsaetze:      die einzelnen Schichten eines Monats aus Staffomatic mit Nacht-, Sonntags- und Feiertagsstunden
- abwesenheiten:  Urlaub und Krankheit (bezahlt) sowie unbezahlte Fehlzeiten, in Stunden
- buchungen:      Mehr- und Minderauszahlungen, Boni, Vorschuesse, Tankkarte, Barzahlungen, Stundenkorrekturen
- dokumente:      Unterlagen je Mitarbeiter, einheitlich benannt unter Unterlagen/Personal/
- einsatzorte:    Staffomatic-Bereich -> Bundesland (Feiertage) und Objektzulage je Stunde
- einstellungen:  Zuschlagssaetze, Nachtfenster

Arbeitszeitkonto wie im Excel-Blatt „Mehr & Minderstunden": gezahlt = Lohnstunden + Urlaub + Krank,
Differenz = geleistet + Urlaub + Krank - gezahlt (= geleistet - Lohnstunden) + Stundenbuchungen.
Saldo in Euro = Saldo in Stunden x Stundenlohn + Euro-Buchungen (ohne Bonus und Vorschuss, die laufen ueber die Abrechnung).
Positiv: die ZAC schuldet dem Mitarbeiter noch Geld. Negativ: der Mitarbeiter hat zu viel erhalten.
"""

import asyncio
import calendar
import html
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import time
from datetime import date, datetime, timedelta

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

import dokumente_tools
import staffo_tools

HERE = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(HERE, "personal.db")
SEED_FILE = os.path.join(HERE, "personal-startdaten.json")

SCHEMA = """
CREATE TABLE IF NOT EXISTS mitarbeiter (
  id INTEGER PRIMARY KEY, personalnr TEXT, nachname TEXT NOT NULL, vorname TEXT NOT NULL DEFAULT '',
  aktiv INTEGER NOT NULL DEFAULT 1, beschaeftigung TEXT DEFAULT '', stundenlohn REAL, festgehalt REAL,
  zeitkonto INTEGER NOT NULL DEFAULT 0, auszahlung_std REAL, soll_std REAL, urlaubstage REAL,
  std_pro_tag REAL, uebertrag_std REAL NOT NULL DEFAULT 0, uebertrag_eur REAL NOT NULL DEFAULT 0,
  eintritt TEXT, austritt TEXT, geburtsdatum TEXT, telefon TEXT, email TEXT, adresse TEXT,
  iban TEXT, steuer_id TEXT, sv_nummer TEXT, krankenkasse TEXT, bewacher_id TEXT,
  staffo_id INTEGER, staffo_name TEXT, notiz TEXT DEFAULT '', excel_saldo_eur REAL, erstellt TEXT);
CREATE TABLE IF NOT EXISTS monatswerte (
  mitarbeiter_id INTEGER NOT NULL REFERENCES mitarbeiter(id) ON DELETE CASCADE, monat TEXT NOT NULL,
  geleistet_std REAL, lohn_std REAL, nacht_std REAL, sonntag_std REAL, feiertag_std REAL,
  objektzulage_eur REAL, verpflegung_eur REAL, brutto_fest REAL, bemerkung TEXT,
  staffo_std REAL, staffo_nacht REAL, staffo_sonntag REAL, staffo_feiertag REAL, staffo_objektzulage REAL,
  staffo_tage INTEGER, staffo_einsaetze INTEGER, staffo_geplant_std REAL, staffo_stand TEXT,
  PRIMARY KEY (mitarbeiter_id, monat));
CREATE TABLE IF NOT EXISTS einsaetze (
  id INTEGER PRIMARY KEY, mitarbeiter_id INTEGER NOT NULL REFERENCES mitarbeiter(id) ON DELETE CASCADE,
  monat TEXT NOT NULL, schicht_id INTEGER, beginn TEXT, ende TEXT, bereich TEXT, standort TEXT, land TEXT,
  std REAL, nacht REAL, sonntag REAL, feiertag REAL, objektzulage REAL, geplant INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS abwesenheiten (
  id INTEGER PRIMARY KEY, mitarbeiter_id INTEGER NOT NULL REFERENCES mitarbeiter(id) ON DELETE CASCADE,
  art TEXT NOT NULL, von TEXT NOT NULL, bis TEXT NOT NULL, stunden REAL NOT NULL DEFAULT 0, tage REAL,
  notiz TEXT DEFAULT '', erstellt TEXT);
CREATE TABLE IF NOT EXISTS buchungen (
  id INTEGER PRIMARY KEY, mitarbeiter_id INTEGER NOT NULL REFERENCES mitarbeiter(id) ON DELETE CASCADE,
  monat TEXT NOT NULL, art TEXT NOT NULL, betrag REAL NOT NULL DEFAULT 0, stunden REAL NOT NULL DEFAULT 0,
  notiz TEXT DEFAULT '', erstellt TEXT);
CREATE TABLE IF NOT EXISTS dokumente (
  id INTEGER PRIMARY KEY, mitarbeiter_id INTEGER REFERENCES mitarbeiter(id) ON DELETE SET NULL,
  kategorie TEXT NOT NULL, datum TEXT, titel TEXT, pfad TEXT NOT NULL, erstellt TEXT);
CREATE TABLE IF NOT EXISTS einsatzorte (
  bereich TEXT PRIMARY KEY, standort TEXT, land TEXT, objektzulage_std REAL NOT NULL DEFAULT 0,
  geprueft INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS einstellungen (schluessel TEXT PRIMARY KEY, wert TEXT);
"""

DEFAULTS = {"nacht_von": "22:00", "nacht_bis": "06:00", "nacht_prozent": 15, "sonntag_prozent": 50,
            "feiertag_prozent": 50, "std_pro_tag": 10, "bereichsleitung": "Julian Zagler"}

ABWESENHEITEN = ["Urlaub", "Krank", "Krank ohne Lohnfortzahlung", "Unbezahlt frei"]
BEZAHLT = {"Urlaub", "Krank"}
# Buchungsarten: Vorzeichen-Vorschlag fuer die Seite; Bonus und Vorschuss stehen auf der Lohnabrechnung, alle anderen im Saldo
BUCHUNGSARTEN = {
    "Bonus": {"vorzeichen": 1, "saldo": False, "text": "Sonderzahlung, kommt auf die Lohnabrechnung (Brutto)"},
    "Vorschuss": {"vorzeichen": 1, "saldo": False, "text": "Netto-Vorschuss, steht als Hinweis auf der Lohnabrechnung"},
    "Mehrauszahlung": {"vorzeichen": -1, "saldo": True, "text": "Mitarbeiter hat mehr bekommen, als ihm zusteht"},
    "Minderauszahlung": {"vorzeichen": 1, "saldo": True, "text": "Mitarbeiter hat weniger bekommen, als ihm zusteht"},
    "Barzahlung": {"vorzeichen": -1, "saldo": True, "text": "bar ausgezahlt"},
    "Tankkarte": {"vorzeichen": -1, "saldo": True, "text": "Tankkarte privat genutzt"},
    "Auslagen": {"vorzeichen": -1, "saldo": True, "text": "Ausgaben, die mit dem Guthaben verrechnet werden"},
    "Stundenkorrektur": {"vorzeichen": 1, "saldo": True, "text": "Stunden aufs Zeitkonto buchen (+) oder abziehen (−)"},
    "Übertrag": {"vorzeichen": 1, "saldo": True, "text": "Übertrag aus altem Stand"},
}
KATEGORIEN = ["Arbeitsvertrag", "Lohnabrechnung", "Krankmeldung", "Urlaubsantrag", "Ausweis", "Bewacher-ID",
              "Qualifikation", "Abmahnung", "Kündigung", "Bescheinigung", "Sonstiges"]
LAENDER = {"BW": "Baden-Württemberg", "BY": "Bayern", "BE": "Berlin", "BB": "Brandenburg", "HB": "Bremen",
           "HH": "Hamburg", "HE": "Hessen", "MV": "Mecklenburg-Vorpommern", "NI": "Niedersachsen",
           "NW": "Nordrhein-Westfalen", "RP": "Rheinland-Pfalz", "SL": "Saarland", "SN": "Sachsen",
           "ST": "Sachsen-Anhalt", "SH": "Schleswig-Holstein", "TH": "Thüringen"}
MONATE = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
          "November", "Dezember"]


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    return con


def settings(con) -> dict:
    s = dict(DEFAULTS)
    for r in con.execute("SELECT schluessel, wert FROM einstellungen"):
        try:
            s[r["schluessel"]] = json.loads(r["wert"])
        except ValueError:
            s[r["schluessel"]] = r["wert"]
    return s


def personal_dir(config: dict) -> str:
    return config.get("personal_ordner") or os.path.join(dokumente_tools.UNTERLAGEN, "Personal")


# ---------- Feiertage je Bundesland ----------

def _ostern(j: int) -> date:
    a, b, c = j % 19, j // 100, j % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mon = (h + l - 7 * m + 114) // 31
    return date(j, mon, (h + l - 7 * m + 114) % 31 + 1)


_ft_cache: dict = {}


def feiertage(jahr: int, land: str) -> dict:
    """Gesetzliche Feiertage {datum: name} fuer ein Bundesland (Kuerzel wie HH, SH, BE)."""
    if (jahr, land) in _ft_cache:
        return _ft_cache[(jahr, land)]
    o = _ostern(jahr)
    t = {date(jahr, 1, 1): "Neujahr", o - timedelta(days=2): "Karfreitag", o + timedelta(days=1): "Ostermontag",
         date(jahr, 5, 1): "Tag der Arbeit", o + timedelta(days=39): "Christi Himmelfahrt",
         o + timedelta(days=50): "Pfingstmontag", date(jahr, 10, 3): "Tag der Deutschen Einheit",
         date(jahr, 12, 25): "1. Weihnachtstag", date(jahr, 12, 26): "2. Weihnachtstag"}
    if land in ("BW", "BY", "ST"):
        t[date(jahr, 1, 6)] = "Heilige Drei Könige"
    if land in ("BE", "MV"):
        t[date(jahr, 3, 8)] = "Frauentag"
    if land == "BB":
        t[o] = "Ostersonntag"
        t[o + timedelta(days=49)] = "Pfingstsonntag"
    if land in ("BW", "BY", "HE", "NW", "RP", "SL"):
        t[o + timedelta(days=60)] = "Fronleichnam"
    if land == "SL":
        t[date(jahr, 8, 15)] = "Mariä Himmelfahrt"
    if land == "TH":
        t[date(jahr, 9, 20)] = "Weltkindertag"
    if land in ("BB", "HB", "HH", "MV", "NI", "SN", "ST", "SH", "TH"):
        t[date(jahr, 10, 31)] = "Reformationstag"
    if land in ("BW", "BY", "NW", "RP", "SL"):
        t[date(jahr, 11, 1)] = "Allerheiligen"
    if land == "SN":
        d = date(jahr, 11, 22)
        while d.weekday() != 2:
            d -= timedelta(days=1)
        t[d] = "Buß- und Bettag"
    _ft_cache[(jahr, land)] = t
    return t


def _minutes(hhmm: str) -> int:
    h, m = (str(hhmm) + ":0").split(":")[:2]
    return int(h) * 60 + int(m)


def split_hours(beginn: datetime, ende: datetime, land: str, s: dict) -> dict:
    """Stunden einer Schicht, aufgeteilt nach Nacht, Sonntag und Feiertag (Ortszeit der Schicht).
    Eine Feiertagsstunde zaehlt nicht zusaetzlich als Sonntagsstunde."""
    total = (ende - beginn).total_seconds() / 60
    if total <= 0:
        return {"std": 0, "nacht": 0, "sonntag": 0, "feiertag": 0}
    wall = beginn.replace(tzinfo=None)
    nv, nb = _minutes(s["nacht_von"]), _minutes(s["nacht_bis"])
    n = so = ft = 0
    for i in range(int(round(total))):
        t = wall + timedelta(minutes=i)
        mm = t.hour * 60 + t.minute
        if (nv <= mm or mm < nb) if nv > nb else (nv <= mm < nb):
            n += 1
        if land and t.date() in feiertage(t.year, land):
            ft += 1
        elif t.weekday() == 6:
            so += 1
    return {"std": round(total / 60, 2), "nacht": round(n / 60, 2), "sonntag": round(so / 60, 2), "feiertag": round(ft / 60, 2)}


def guess_land(bereich: str, standort: str) -> str:
    b, st = (bereich or "").lower(), (standort or "").lower()
    for words, land in ((("harburg", "hamburg", "hh "), "HH"), (("oststeinbek", "schwarzenbek", "geesthacht", "mölln", "moelln", "bad.", "lübeck", "kiel"), "SH"),
                        (("leipzig", "dresden"), "SN"), (("berlin",), "BE"), (("potsdam",), "BB"), (("hannover", "lüneburg"), "NI")):
        if any(w in b for w in words):
            return land
    if "berlin" in st:
        return "BE"
    if "nord" in st or "hh" in st:
        return "HH"
    return ""


# ---------- Startdaten (Excel) ----------

def seed_if_empty(con) -> int:
    if con.execute("SELECT COUNT(*) FROM mitarbeiter").fetchone()[0] or not os.path.exists(SEED_FILE):
        return 0
    with open(SEED_FILE, encoding="utf-8") as f:
        d = json.load(f)
    ids = {}
    for m in d.get("mitarbeiter", []):
        cur = con.execute(
            "INSERT INTO mitarbeiter (personalnr, nachname, vorname, aktiv, stundenlohn, festgehalt, zeitkonto, auszahlung_std,"
            " urlaubstage, uebertrag_std, staffo_id, staffo_name, notiz, excel_saldo_eur, erstellt) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (m.get("personalnr"), m["nachname"], m.get("vorname", ""), int(bool(m.get("aktiv"))), m.get("stundenlohn"),
             m.get("festgehalt"), int(bool(m.get("zeitkonto"))), m.get("auszahlung_std"), m.get("urlaubstage"),
             m.get("uebertrag_std") or 0, m.get("staffo_id"), m.get("staffo_name"),
             ("Aus Excel übernommen. " + m["notiz"]) if m.get("notiz") else "Aus Excel übernommen.", m.get("excel_saldo_eur"), now()))
        ids[m["key"]] = cur.lastrowid
    cols = ["geleistet_std", "lohn_std", "nacht_std", "sonntag_std", "feiertag_std", "objektzulage_eur", "verpflegung_eur", "brutto_fest", "bemerkung"]
    for mv in d.get("monate", []):
        vals = [mv.get(c) for c in cols]
        con.execute(f"INSERT INTO monatswerte (mitarbeiter_id, monat, {', '.join(cols)}) VALUES (?,?,{','.join('?' * len(cols))})",
                    [ids[mv["key"]], mv["monat"], *vals])
    for a in d.get("abwesenheiten", []):
        con.execute("INSERT INTO abwesenheiten (mitarbeiter_id, art, von, bis, stunden, notiz, erstellt) VALUES (?,?,?,?,?,?,?)",
                    (ids[a["key"]], a["art"], a["von"], a["bis"], a["stunden"], a.get("notiz", ""), now()))
    for b in d.get("buchungen", []):
        con.execute("INSERT INTO buchungen (mitarbeiter_id, monat, art, betrag, stunden, notiz, erstellt) VALUES (?,?,?,?,?,?,?)",
                    (ids[b["key"]], b["monat"], b["art"], b.get("betrag") or 0, b.get("stunden") or 0, b.get("notiz", ""), now()))
    con.commit()
    return len(ids)


# ---------- Berechnung ----------

def _v(*xs):
    for x in xs:
        if x is not None:
            return x
    return None


def month_keys(jahr: int) -> list:
    return [f"{jahr}-{m:02d}" for m in range(1, 13)]


def compute(con, jahr: int) -> dict:
    """Alle Mitarbeiter mit Monatswerten, Saldo und Kennzahlen des Jahres."""
    s = settings(con)
    pn, ps, pf = s["nacht_prozent"] / 100, s["sonntag_prozent"] / 100, s["feiertag_prozent"] / 100
    heute = date.today().strftime("%Y-%m")
    months = month_keys(jahr)
    mvs, abw, bu = {}, {}, {}
    for r in con.execute("SELECT * FROM monatswerte WHERE monat LIKE ?", (f"{jahr}-%",)):
        mvs[(r["mitarbeiter_id"], r["monat"])] = dict(r)
    for r in con.execute("SELECT * FROM abwesenheiten WHERE von LIKE ?", (f"{jahr}-%",)):
        k = (r["mitarbeiter_id"], r["von"][:7])
        abw.setdefault(k, {}).setdefault(r["art"], 0)
        abw[k][r["art"]] += r["stunden"] or 0
    for r in con.execute("SELECT * FROM buchungen WHERE monat LIKE ?", (f"{jahr}-%",)):
        bu.setdefault((r["mitarbeiter_id"], r["monat"]), []).append(dict(r))
    # Saldo zum Jahresanfang: Endstand des Vorjahres, solange es dort Daten gibt
    erstes = con.execute("SELECT MIN(x) FROM (SELECT MIN(monat) x FROM monatswerte UNION SELECT MIN(monat) FROM buchungen"
                         " UNION SELECT MIN(substr(von,1,7)) FROM abwesenheiten)").fetchone()[0]
    start = {}
    if erstes and int(erstes[:4]) < jahr:
        for v in compute(con, jahr - 1)["mitarbeiter"]:
            start[v["id"]] = (v["saldo_std"], v["saldo_eur_extra"])
    result = []
    for m in con.execute("SELECT * FROM mitarbeiter ORDER BY aktiv DESC, nachname, vorname"):
        m = dict(m)
        lohn = m["stundenlohn"] or 0
        if m["id"] in start:
            saldo_std, saldo_eur_extra = start[m["id"]]
        else:
            saldo_std, saldo_eur_extra = (m["uebertrag_std"] or 0), (m["uebertrag_eur"] or 0)
        rows = []
        for mon in months:
            w = mvs.get((m["id"], mon), {})
            a = abw.get((m["id"], mon), {})
            b = bu.get((m["id"], mon), [])
            urlaub, krank = a.get("Urlaub", 0), a.get("Krank", 0)
            unbez = a.get("Krank ohne Lohnfortzahlung", 0) + a.get("Unbezahlt frei", 0)
            gel = _v(w.get("geleistet_std"), w.get("staffo_std"))
            lohn_std = w.get("lohn_std")
            vergangen = mon <= heute
            if lohn_std is None and (vergangen or w):
                if m["zeitkonto"]:  # feste Auszahlungsstunden; ohne Angabe wird nichts als gezahlt gerechnet
                    lohn_std = max(0.0, m["auszahlung_std"] - urlaub - krank) if (vergangen and m["auszahlung_std"]) else None
                else:
                    lohn_std = gel
            buch_std = sum(x["stunden"] for x in b if BUCHUNGSARTEN.get(x["art"], {}).get("saldo", True))
            buch_eur = sum(x["betrag"] for x in b if BUCHUNGSARTEN.get(x["art"], {}).get("saldo", True))
            bonus = sum(x["betrag"] for x in b if x["art"] == "Bonus")
            vorschuss = sum(x["betrag"] for x in b if x["art"] == "Vorschuss")
            nacht = _v(w.get("nacht_std"), w.get("staffo_nacht")) or 0
            sonntag = _v(w.get("sonntag_std"), w.get("staffo_sonntag")) or 0
            feiertag = _v(w.get("feiertag_std"), w.get("staffo_feiertag")) or 0
            objekt = _v(w.get("objektzulage_eur"), w.get("staffo_objektzulage")) or 0
            verpf = w.get("verpflegung_eur") or 0
            diff = ((gel or 0) - (lohn_std or 0) if m["zeitkonto"] else 0) + buch_std
            hat_daten = bool(w) or bool(a) or bool(b)
            if m["festgehalt"]:
                brutto = m["festgehalt"] + bonus
            else:
                brutto = ((lohn_std or 0) + urlaub + krank) * lohn + objekt + nacht * lohn * pn + sonntag * lohn * ps + feiertag * lohn * pf + bonus
            brutto = _v(w.get("brutto_fest"), round(brutto, 2) if hat_daten else None)
            if hat_daten:
                saldo_std += diff
                saldo_eur_extra += buch_eur
            rows.append({
                "monat": mon, "daten": hat_daten, "geleistet": gel, "geleistet_quelle": "hand" if w.get("geleistet_std") is not None else ("staffo" if w.get("staffo_std") is not None else ""),
                "staffo_std": w.get("staffo_std"), "staffo_geplant": w.get("staffo_geplant_std"), "staffo_stand": w.get("staffo_stand"),
                "tage": w.get("staffo_tage"), "einsaetze": w.get("staffo_einsaetze"),
                "lohn_std": lohn_std, "lohn_std_hand": w.get("lohn_std") is not None,
                "urlaub": urlaub, "krank": krank, "unbezahlt": unbez, "gezahlt": round((lohn_std or 0) + urlaub + krank, 2),
                "nacht": nacht, "sonntag": sonntag, "feiertag": feiertag, "objektzulage": objekt, "verpflegung": verpf,
                "hand": {k: w.get(k) for k in ("geleistet_std", "lohn_std", "nacht_std", "sonntag_std", "feiertag_std", "objektzulage_eur", "verpflegung_eur", "brutto_fest")},
                "staffo": {k: w.get(k) for k in ("staffo_std", "staffo_nacht", "staffo_sonntag", "staffo_feiertag", "staffo_objektzulage")},
                "bonus": bonus, "vorschuss": vorschuss, "buch_std": round(buch_std, 2), "buch_eur": round(buch_eur, 2),
                "buchungen": len(b), "diff": round(diff, 2), "saldo_std": round(saldo_std, 2),
                "saldo_eur": round(saldo_std * lohn + saldo_eur_extra, 2), "brutto": brutto,
                "bemerkung": w.get("bemerkung") or "",
            })
        tot = lambda k: round(sum((r[k] or 0) for r in rows), 2)
        urlaub_tage_genommen = round(tot("urlaub") / (m["std_pro_tag"] or s["std_pro_tag"] or 10), 1)
        result.append({**m, "monate": rows,
                       "summe": {k: tot(k) for k in ("geleistet", "lohn_std", "urlaub", "krank", "gezahlt", "nacht", "sonntag", "feiertag",
                                                     "objektzulage", "verpflegung", "bonus", "vorschuss", "buch_eur", "diff", "brutto")},
                       "saldo_std": round(saldo_std, 2), "saldo_eur": round(saldo_std * lohn + saldo_eur_extra, 2),
                       "saldo_eur_extra": round(saldo_eur_extra, 2), "urlaub_tage_genommen": urlaub_tage_genommen,
                       "urlaub_tage_rest": (round(m["urlaubstage"] - urlaub_tage_genommen, 1) if m["urlaubstage"] is not None else None)})
    return {"mitarbeiter": result, "einstellungen": s}


# ---------- Staffomatic ----------

def month_range(monat: str) -> tuple[date, date]:
    j, m = map(int, monat.split("-"))
    return date(j, m, 1), date(j, m, calendar.monthrange(j, m)[1])


def staffo_import(monat: str) -> dict:
    """Schichten des Monats aus Staffomatic: geleistete (bereits beendete) und geplante Stunden je Mitarbeiter,
    Nacht-, Sonntags- und Feiertagsstunden nach Bundesland des Einsatzorts, Objektzulage nach Einsatzort."""
    von, bis = month_range(monat)
    rows = staffo_tools.assignments(von, bis)
    con = db()
    s = settings(con)
    orte = {r["bereich"]: dict(r) for r in con.execute("SELECT * FROM einsatzorte")}
    for r in rows:
        if r["bereich"] and r["bereich"] not in orte:
            land = guess_land(r["bereich"], r["standort"])
            con.execute("INSERT INTO einsatzorte (bereich, standort, land) VALUES (?,?,?)", (r["bereich"], r["standort"], land))
            orte[r["bereich"]] = {"bereich": r["bereich"], "standort": r["standort"], "land": land, "objektzulage_std": 0, "geprueft": 0}
    by_staffo = {r["staffo_id"]: r["id"] for r in con.execute("SELECT id, staffo_id FROM mitarbeiter WHERE staffo_id IS NOT NULL")}
    jetzt = datetime.now().astimezone()
    agg, einsaetze, fremd = {}, [], {}
    for r in rows:
        try:
            b, e = datetime.fromisoformat(r["beginn"]), datetime.fromisoformat(r["ende"])
        except (TypeError, ValueError):
            continue
        ort = orte.get(r["bereich"], {})
        h = split_hours(b, e, ort.get("land") or "", s)
        geplant = e > jetzt
        mid = by_staffo.get(r["user_id"])
        if mid is None:
            f = fremd.setdefault(r["user_id"], {"staffo_id": r["user_id"], "name": r["name"], "std": 0, "geplant": 0, "einsaetze": 0})
            f["geplant" if geplant else "std"] += h["std"]
            f["einsaetze"] += 1
            continue
        a = agg.setdefault(mid, {"std": 0, "nacht": 0, "sonntag": 0, "feiertag": 0, "objekt": 0, "tage": set(), "n": 0, "geplant": 0})
        oz = round(h["std"] * (ort.get("objektzulage_std") or 0), 2)
        if geplant:
            a["geplant"] += h["std"]
        else:
            a["std"] += h["std"]; a["nacht"] += h["nacht"]; a["sonntag"] += h["sonntag"]; a["feiertag"] += h["feiertag"]
            a["objekt"] += oz; a["tage"].add(b.date()); a["n"] += 1
        einsaetze.append((mid, monat, r["shift_id"], r["beginn"], r["ende"], r["bereich"], r["standort"], ort.get("land") or "",
                          h["std"], h["nacht"], h["sonntag"], h["feiertag"], oz, int(geplant)))
    stand = now()
    con.execute("DELETE FROM einsaetze WHERE monat = ?", (monat,))
    con.executemany("INSERT INTO einsaetze (mitarbeiter_id, monat, schicht_id, beginn, ende, bereich, standort, land, std, nacht, sonntag,"
                    " feiertag, objektzulage, geplant) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", einsaetze)
    # Staffo-Spalten aller zugeordneten Mitarbeiter fuer den Monat neu setzen (wer keine Schicht hatte, bekommt 0)
    for mid in set(by_staffo.values()):
        a = agg.get(mid)
        vals = (round(a["std"], 2), round(a["nacht"], 2), round(a["sonntag"], 2), round(a["feiertag"], 2), round(a["objekt"], 2),
                len(a["tage"]), a["n"], round(a["geplant"], 2)) if a else (0, 0, 0, 0, 0, 0, 0, 0)
        con.execute("INSERT INTO monatswerte (mitarbeiter_id, monat) VALUES (?, ?) ON CONFLICT DO NOTHING", (mid, monat))
        con.execute("UPDATE monatswerte SET staffo_std=?, staffo_nacht=?, staffo_sonntag=?, staffo_feiertag=?, staffo_objektzulage=?,"
                    " staffo_tage=?, staffo_einsaetze=?, staffo_geplant_std=?, staffo_stand=? WHERE mitarbeiter_id=? AND monat=?",
                    (*vals, stand, mid, monat))
    fremd_list = sorted(({**f, "std": round(f["std"], 2), "geplant": round(f["geplant"], 2)} for f in fremd.values()), key=lambda x: -x["std"] - x["geplant"])
    con.execute("INSERT INTO einstellungen (schluessel, wert) VALUES (?, ?) ON CONFLICT(schluessel) DO UPDATE SET wert = excluded.wert",
                ("staffo_ohne_akte", json.dumps({"monat": monat, "stand": stand, "mitarbeiter": fremd_list}, ensure_ascii=False)))
    con.commit()
    con.close()
    return {"ok": True, "monat": monat, "schichten": len(rows), "zugeordnet": len(agg), "ohne_akte": fremd_list, "stand": stand}


_plan_cache: tuple[float, dict] = (0.0, {})


def staffo_vorschau(tage: int = 14) -> dict:
    """Geplante Stunden je Staffomatic-Mitarbeiter in den naechsten Tagen (fuer „fehlen Einsaetze?"), 10 Minuten zwischengespeichert."""
    global _plan_cache
    if time.time() - _plan_cache[0] < 600 and _plan_cache[1].get("tage") == tage:
        return _plan_cache[1]
    heute = date.today()
    rows = staffo_tools.assignments(heute, heute + timedelta(days=tage - 1))
    plan = {}
    for r in rows:
        try:
            h = (datetime.fromisoformat(r["ende"]) - datetime.fromisoformat(r["beginn"])).total_seconds() / 3600
        except (TypeError, ValueError):
            continue
        p = plan.setdefault(str(r["user_id"]), {"name": r["name"], "std": 0, "schichten": 0})
        p["std"] = round(p["std"] + h, 2)
        p["schichten"] += 1
    offen = sum(1 for _ in staffo_tools.open_shifts(heute, heute + timedelta(days=tage - 1)))
    d = {"tage": tage, "von": heute.isoformat(), "plan": plan, "offene_schichten": offen, "stand": now()}
    _plan_cache = (time.time(), d)
    return d


# ---------- Dateien: einheitliche Benennung ----------

def _clean(x: str) -> str:
    x = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(x or "")).strip()
    return re.sub(r"-{2,}", "-", re.sub(r"\s+", "-", x)).strip(".-_")


def ma_folder(config: dict, m: dict) -> str:
    """Unterlagen/Personal/<Nr> <Nachname> <Vorname>/"""
    nr = f"{m['personalnr']} " if m.get("personalnr") else ""
    name = dokumente_tools.safe_name(f"{nr}{m['nachname']} {m['vorname']}".strip())
    return os.path.join(personal_dir(config), name)


def doc_name(m: dict, kategorie: str, datum: str, zusatz: str, ext: str) -> str:
    """<JJJJ-MM-TT>_<Nr>_<Nachname>_<Vorname>_<Kategorie>[_<Zusatz>].<ext>"""
    parts = [datum, m.get("personalnr") or "", m["nachname"], m["vorname"], kategorie, zusatz]
    return "_".join(_clean(p) for p in parts if _clean(p)) + ext.lower()


def register_doc(con, mid: int, kategorie: str, datum: str, titel: str, pfad: str) -> int:
    con.execute("DELETE FROM dokumente WHERE pfad = ?", (pfad,))
    cur = con.execute("INSERT INTO dokumente (mitarbeiter_id, kategorie, datum, titel, pfad, erstellt) VALUES (?,?,?,?,?,?)",
                      (mid, kategorie, datum, titel, pfad, now()))
    return cur.lastrowid


# ---------- Lohnabrechnung als PDF ----------

def _eur(x) -> str:
    return "" if x in (None, "") else f"{x:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def _eur0(x) -> str:
    return _eur(x) if x else ""


def _h(x) -> str:
    return "" if not x else f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


PDF_CSS = """@page { size: A4; margin: 14mm 14mm 16mm; }
body { font-family: Arial, Helvetica, sans-serif; font-size: 9.5pt; color: #111; }
h1 { font-size: 15pt; margin: 0 0 2pt; } h2 { font-size: 11pt; margin: 14pt 0 4pt; }
.kopf { display: flex; justify-content: space-between; border-bottom: 2px solid #C8102E; padding-bottom: 6pt; margin-bottom: 10pt; }
.kopf small { color: #555; display: block; }
table { border-collapse: collapse; width: 100%; } th, td { border: 1px solid #bbb; padding: 3pt 5pt; text-align: left; }
th { background: #eee; font-size: 8.5pt; } td.n, th.n { text-align: right; white-space: nowrap; }
tr.sum td { font-weight: bold; background: #f6f6f6; }
.meta td { border: 0; padding: 1pt 10pt 1pt 0; } .meta td:first-child { color: #555; }
.hinweis { font-size: 8pt; color: #444; margin-top: 10pt; white-space: pre-line; }
.unterschrift { display: flex; gap: 40pt; margin-top: 34pt; } .unterschrift div { flex: 1; border-top: 1px solid #333; padding-top: 3pt; font-size: 8.5pt; }
.seite { page-break-after: always; } .seite:last-child { page-break-after: auto; }"""

LEGENDE = ("U = Urlaub; K = Krank; KF = Krank ohne Lohnfortzahlung; FU = Unbezahlt frei; FT = Feiertag.\n"
           "Mehrarbeit ist nur auf ausdrückliche Anweisung des Arbeitgebers zulässig und wird auch nur dann vergütet.")


def _abrechnung_html(m: dict, r: dict, einsaetze: list, abw: list, buch: list, s: dict) -> str:
    j, mo = map(int, r["monat"].split("-"))
    lohn = m["stundenlohn"] or 0
    e = html.escape
    pos = []
    if m["festgehalt"]:
        pos.append(("Festgehalt", "", "", m["festgehalt"]))
    else:
        pos.append(("Arbeitsstunden", _h(r["lohn_std"]), _eur(lohn), (r["lohn_std"] or 0) * lohn))
        if r["urlaub"]:
            pos.append(("Urlaub", _h(r["urlaub"]), _eur(lohn), r["urlaub"] * lohn))
        if r["krank"]:
            pos.append(("Krank (Lohnfortzahlung)", _h(r["krank"]), _eur(lohn), r["krank"] * lohn))
        if r["objektzulage"]:
            pos.append(("Objektzulage", "", "", r["objektzulage"]))
        for label, k, p in (("Zuschlag Nacht", "nacht", s["nacht_prozent"]), ("Zuschlag Sonntag", "sonntag", s["sonntag_prozent"]),
                            ("Zuschlag Feiertag", "feiertag", s["feiertag_prozent"])):
            if r[k]:
                pos.append((f"{label} {p:g} %", _h(r[k]), _eur(lohn * p / 100), r[k] * lohn * p / 100))
    for b in buch:
        if b["art"] == "Bonus":
            pos.append(("Bonus" + (f" – {b['notiz']}" if b["notiz"] else ""), "", "", b["betrag"]))
    summe = round(sum(p[3] for p in pos), 2)
    brutto = r["brutto"] if r["brutto"] is not None else summe
    rows = "".join(f"<tr><td>{e(p[0])}</td><td class=n>{p[1]}</td><td class=n>{p[2]}</td><td class=n>{_eur(p[3])}</td></tr>" for p in pos)
    rows += f"<tr class=sum><td>Brutto</td><td class=n>{_h((r['lohn_std'] or 0) + r['urlaub'] + r['krank'])}</td><td></td><td class=n>{_eur(brutto)}</td></tr>"
    extra = ""
    if abs(brutto - summe) > 0.01:
        extra += f"<p class=hinweis>Brutto für diesen Monat fest eingetragen ({_eur(brutto)}); aus den Positionen ergeben sich {_eur(summe)}.</p>"
    if r["verpflegung"]:
        extra += f"<p><b>Verpflegungsmehraufwand (steuerfrei):</b> {_eur(r['verpflegung'])}</p>"
    vs = [b for b in buch if b["art"] == "Vorschuss"]
    if vs:
        extra += "<p><b>Netto-Vorschüsse, bitte abziehen:</b> " + "; ".join(f"{_eur(b['betrag'])}" + (f" ({e(b['notiz'])})" if b["notiz"] else "") for b in vs) + "</p>"
    if r["bemerkung"]:
        extra += f"<p><b>Bemerkung:</b> {e(r['bemerkung'])}</p>"
    ab = ""
    if abw:
        ab = "<h2>Abwesenheiten</h2><table><tr><th>Art</th><th>Von</th><th>Bis</th><th class=n>Stunden</th><th>Notiz</th></tr>" + "".join(
            f"<tr><td>{e(a['art'])}</td><td>{_de(a['von'])}</td><td>{_de(a['bis'])}</td><td class=n>{_h(a['stunden'])}</td><td>{e(a['notiz'] or '')}</td></tr>" for a in abw) + "</table>"
    es = ""
    done = [x for x in einsaetze if not x["geplant"]]
    if done:
        body = ""
        for x in done:
            b0, e0 = datetime.fromisoformat(x["beginn"]), datetime.fromisoformat(x["ende"])
            ft = feiertage(b0.year, x["land"]).get(b0.date(), "") if x["land"] else ""
            body += (f"<tr><td>{['Mo','Di','Mi','Do','Fr','Sa','So'][b0.weekday()]} {b0.strftime('%d.%m.')}</td><td>{e(x['bereich'] or '')}"
                     f"{' · ' + x['land'] if x['land'] else ''}</td><td>{b0.strftime('%H:%M')}–{e0.strftime('%H:%M')}</td>"
                     f"<td class=n>{_h(x['std'])}</td><td class=n>{_h(x['nacht'])}</td><td class=n>{_h(x['sonntag'])}</td><td class=n>{_h(x['feiertag'])}{' FT' if ft else ''}</td></tr>")
        t = lambda k: _h(sum(x[k] for x in done))
        es = ("<h2>Einsätze laut Dienstplan (Staffomatic)</h2><table><tr><th>Tag</th><th>Objekt · Land</th><th>Zeit</th><th class=n>Std</th>"
              "<th class=n>Nacht</th><th class=n>Sonntag</th><th class=n>Feiertag</th></tr>" + body +
              f"<tr class=sum><td colspan=3>{len(done)} Einsätze</td><td class=n>{t('std')}</td><td class=n>{t('nacht')}</td><td class=n>{t('sonntag')}</td><td class=n>{t('feiertag')}</td></tr></table>")
    return f"""<section class=seite><div class=kopf><div><h1>Lohnabrechnung {MONATE[mo - 1]} {j}</h1>
<small>Aufzeichnungspflichten des Arbeitgebers nach § 2 Abs. 2a Arbeitnehmer-Entsendegesetz</small></div>
<div style="text-align:right"><b>ZAC GmbH</b><small>Bereichsleitung: {e(s.get('bereichsleitung') or '')}</small></div></div>
<table class=meta><tr><td>Personal-Nr.</td><td><b>{e(m['personalnr'] or '–')}</b></td><td>Name</td><td><b>{e(m['nachname'])}, {e(m['vorname'])}</b></td></tr>
<tr><td>Stundenlohn</td><td>{_eur(lohn) if lohn else '–'}</td><td>Beschäftigung</td><td>{e(m['beschaeftigung'] or ('Festgehalt' if m['festgehalt'] else ''))}</td></tr></table>
<h2>Lohnbestandteile</h2><table><tr><th>Position</th><th class=n>Stunden</th><th class=n>Satz</th><th class=n>Betrag</th></tr>{rows}</table>
{extra}{ab}{es}<p class=hinweis>{e(LEGENDE)}</p>
<div class=unterschrift><div>Unterschrift Mitarbeiter</div><div>Unterschrift Bereichsleitung</div></div></section>"""


def _de(iso: str) -> str:
    try:
        return date.fromisoformat(iso[:10]).strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return iso or ""


def html_to_pdf(page: str, target: str) -> bool:
    fd, src = tempfile.mkstemp(suffix=".html")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(page)
    chrome = next((c for c in dokumente_tools.CHROME + [os.environ.get("CHROME_PATH", "")] if c and os.path.exists(c)), None)
    ok = False
    if chrome:
        with tempfile.TemporaryDirectory() as profile:
            try:
                subprocess.run([chrome, "--headless=new", "--disable-gpu", "--no-pdf-header-footer", "--no-sandbox",
                                f"--user-data-dir={profile}", f"--print-to-pdf={target}",
                                "file:///" + src.replace("\\", "/").lstrip("/")], timeout=90, capture_output=True)
                ok = os.path.exists(target) and os.path.getsize(target) > 0
            except (OSError, subprocess.TimeoutExpired):
                ok = False
    os.remove(src)
    return ok


def _page(title: str, body: str) -> str:
    return f"<!doctype html><html lang=de><head><meta charset=utf-8><title>{html.escape(title)}</title><style>{PDF_CSS}</style></head><body>{body}</body></html>"


def lohnabrechnungen(config: dict, monat: str, ids: list | None = None) -> dict:
    """PDF je Mitarbeiter: Unterlagen/Personal/<Ordner>/Lohnabrechnung/<JJJJ-MM>_<Nr>_<Name>_Lohnabrechnung.pdf,
    dazu eine Monatsuebersicht fuer das Lohnbuero unter Unterlagen/Personal/Lohnabrechnungen/."""
    con = db()
    data = compute(con, int(monat[:4]))
    s = data["einstellungen"]
    done, pages, uebersicht = [], [], []
    for m in data["mitarbeiter"]:
        if ids and m["id"] not in ids:
            continue
        r = next(x for x in m["monate"] if x["monat"] == monat)
        if not ids and not (m["aktiv"] and (m["festgehalt"] or r["lohn_std"] or r["urlaub"] or r["krank"] or r["brutto"])):
            continue
        ein = [dict(x) for x in con.execute("SELECT * FROM einsaetze WHERE mitarbeiter_id = ? AND monat = ? ORDER BY beginn", (m["id"], monat))]
        abw = [dict(x) for x in con.execute("SELECT * FROM abwesenheiten WHERE mitarbeiter_id = ? AND substr(von,1,7) = ? ORDER BY von", (m["id"], monat))]
        buch = [dict(x) for x in con.execute("SELECT * FROM buchungen WHERE mitarbeiter_id = ? AND monat = ? ORDER BY id", (m["id"], monat))]
        body = _abrechnung_html(m, r, ein, abw, buch, s)
        pages.append(body)
        uebersicht.append((m, r))
        folder = os.path.join(ma_folder(config, m), "Lohnabrechnung")
        os.makedirs(folder, exist_ok=True)
        target = os.path.join(folder, doc_name(m, "Lohnabrechnung", monat, "", ".pdf"))
        ok = html_to_pdf(_page(f"Lohnabrechnung {monat} {m['nachname']}", body), target)
        if not ok:
            target = target[:-4] + ".html"
            with open(target, "w", encoding="utf-8") as f:
                f.write(_page(f"Lohnabrechnung {monat} {m['nachname']}", body))
        did = register_doc(con, m["id"], "Lohnabrechnung", monat, f"Lohnabrechnung {MONATE[int(monat[5:]) - 1]} {monat[:4]}", target)
        done.append({"mitarbeiter_id": m["id"], "name": f"{m['nachname']}, {m['vorname']}", "datei": os.path.basename(target), "dokument_id": did, "pdf": ok})
    sammel = None
    if uebersicht and not ids:
        j, mo = map(int, monat.split("-"))
        rows = "".join(
            f"<tr><td>{html.escape(m['personalnr'] or '')}</td><td>{html.escape(m['nachname'])}, {html.escape(m['vorname'])}</td>"
            f"<td class=n>{_eur(m['stundenlohn'])}</td><td class=n>{_h(r['lohn_std'])}</td><td class=n>{_eur0(r['verpflegung'])}</td>"
            f"<td class=n>{_eur0(r['objektzulage'])}</td><td class=n>{_h(r['nacht'])}</td><td class=n>{_h(r['sonntag'])}</td><td class=n>{_h(r['feiertag'])}</td>"
            f"<td class=n>{_h(r['krank'])}</td><td class=n>{_h(r['urlaub'])}</td><td class=n>{_eur0(r['bonus'])}</td><td class=n><b>{_eur(r['brutto'])}</b></td>"
            f"<td>{html.escape(r['bemerkung'])}{' · Vorschuss ' + _eur(r['vorschuss']) if r['vorschuss'] else ''}</td></tr>"
            for m, r in uebersicht)
        total = sum(r["brutto"] or 0 for _, r in uebersicht)
        body = (f"<div class=kopf><div><h1>Lohnübersicht {MONATE[mo - 1]} {j}</h1><small>Aufzeichnungspflichten des Arbeitgebers nach § 2 Abs. 2a "
                f"Arbeitnehmer-Entsendegesetz</small></div><div style='text-align:right'><b>ZAC GmbH</b><small>Bereichsleitung: {html.escape(s.get('bereichsleitung') or '')}</small></div></div>"
                "<table><tr><th>Nr.</th><th>Name</th><th class=n>Std.-Lohn</th><th class=n>Stunden</th><th class=n>Verpfl.</th><th class=n>Objektzul.</th>"
                f"<th class=n>Nacht {s['nacht_prozent']:g} %</th><th class=n>Sonntag {s['sonntag_prozent']:g} %</th><th class=n>Feiertag {s['feiertag_prozent']:g} %</th>"
                f"<th class=n>Krank</th><th class=n>Urlaub</th><th class=n>Bonus</th><th class=n>Brutto</th><th>Bemerkung</th></tr>{rows}"
                f"<tr class=sum><td colspan=12>Gesamt</td><td class=n>{_eur(total)}</td><td></td></tr></table><p class=hinweis>{html.escape(LEGENDE)}</p>"
                "<div class=unterschrift><div>Datum</div><div>Unterschrift Bereichsleitung</div></div>")
        folder = os.path.join(personal_dir(config), "Lohnabrechnungen")
        os.makedirs(folder, exist_ok=True)
        target = os.path.join(folder, f"{monat}_Lohnuebersicht_ZAC.pdf")
        page = _page(f"Lohnübersicht {monat}", body).replace("size: A4;", "size: A4 landscape;")
        if not html_to_pdf(page, target):
            target = target[:-4] + ".html"
            with open(target, "w", encoding="utf-8") as f:
                f.write(page)
        sammel = {"datei": os.path.basename(target), "dokument_id": register_doc(con, None, "Lohnübersicht", monat, f"Lohnübersicht {monat}", target)}
    con.commit()
    con.close()
    return {"ok": True, "monat": monat, "erstellt": done, "uebersicht": sammel, "ordner": personal_dir(config)}


# ---------- Eingaben pruefen ----------

def _num(x, allow_none=True):
    if x in (None, "") and allow_none:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    t = str(x).strip().replace("€", "").replace(" ", "")
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    return float(t)


def _monat(x) -> str:
    x = str(x or "")
    if not re.fullmatch(r"\d{4}-\d{2}", x) or not 1 <= int(x[5:]) <= 12:
        raise ValueError("Monat bitte als JJJJ-MM")
    return x


def _datum(x) -> str:
    return date.fromisoformat(str(x)[:10]).isoformat()


MA_TEXT = ["personalnr", "nachname", "vorname", "beschaeftigung", "eintritt", "austritt", "geburtsdatum", "telefon", "email",
           "adresse", "iban", "steuer_id", "sv_nummer", "krankenkasse", "bewacher_id", "staffo_name", "notiz"]
MA_NUM = ["stundenlohn", "festgehalt", "auszahlung_std", "soll_std", "urlaubstage", "std_pro_tag", "uebertrag_std", "uebertrag_eur"]
MV_NUM = ["geleistet_std", "lohn_std", "nacht_std", "sonntag_std", "feiertag_std", "objektzulage_eur", "verpflegung_eur", "brutto_fest"]


# ---------- Routen ----------

def make_router(check_local, config: dict) -> APIRouter:
    r = APIRouter(prefix="/api/zentrale/personal")
    con = db()
    seed_if_empty(con)
    con.close()

    def run(fn):
        """Kleine DB-Schreibaktion mit Fehlern als {"ok": False, "fehler": ...}."""
        con = db()
        try:
            out = fn(con)
            con.commit()
            return out if isinstance(out, dict) else {"ok": True}
        except (ValueError, TypeError, KeyError) as e:
            return {"ok": False, "fehler": str(e) or "Ungültige Angaben."}
        finally:
            con.close()

    @r.get("/daten")
    async def daten(request: Request, jahr: int = 0):
        check_local(request)
        jahr = jahr or date.today().year

        def load():
            con = db()
            d = compute(con, jahr)
            d["jahr"] = jahr
            d["einsatzorte"] = [dict(x) for x in con.execute("SELECT * FROM einsatzorte ORDER BY standort, bereich")]
            d["abwesenheiten"] = [dict(x) for x in con.execute("SELECT * FROM abwesenheiten WHERE von LIKE ? OR bis LIKE ? ORDER BY von DESC", (f"{jahr}-%", f"{jahr}-%"))]
            d["buchungen"] = [dict(x) for x in con.execute("SELECT * FROM buchungen WHERE monat LIKE ? ORDER BY monat DESC, id DESC", (f"{jahr}-%",))]
            d["dokumente"] = [{**dict(x), "datei": os.path.basename(x["pfad"]), "vorhanden": os.path.exists(x["pfad"])}
                              for x in con.execute("SELECT * FROM dokumente ORDER BY datum DESC, id DESC")]
            d["staffo_ohne_akte"] = d["einstellungen"].pop("staffo_ohne_akte", None)
            d["arten"] = {"abwesenheit": ABWESENHEITEN, "buchung": BUCHUNGSARTEN, "dokument": KATEGORIEN, "laender": LAENDER}
            d["ordner"] = personal_dir(config)
            con.close()
            return d
        return await asyncio.to_thread(load)

    @r.post("/mitarbeiter")
    async def mitarbeiter(request: Request):
        check_local(request, write=True)
        b = await request.json()

        def save(con):
            if b.get("loeschen"):
                mid = int(b["id"])
                n = con.execute("SELECT (SELECT COUNT(*) FROM monatswerte WHERE mitarbeiter_id=?) + (SELECT COUNT(*) FROM buchungen WHERE mitarbeiter_id=?)"
                                " + (SELECT COUNT(*) FROM dokumente WHERE mitarbeiter_id=?)", (mid, mid, mid)).fetchone()[0]
                if n:
                    raise ValueError("Mitarbeiter hat schon Daten. Bitte auf „inaktiv“ setzen statt löschen.")
                con.execute("DELETE FROM mitarbeiter WHERE id = ?", (mid,))
                return {"ok": True}
            vals = {k: (str(b[k]).strip()[:500] if b.get(k) not in (None, "") else None) for k in MA_TEXT if k in b}
            vals.update({k: _num(b[k]) for k in MA_NUM if k in b})
            for k in ("aktiv", "zeitkonto"):
                if k in b:
                    vals[k] = int(bool(b[k]))
            if "staffo_id" in b:
                vals["staffo_id"] = int(b["staffo_id"]) if str(b["staffo_id"] or "").strip() else None
            if b.get("id"):
                if "nachname" in vals and not vals["nachname"]:
                    raise ValueError("Nachname fehlt.")
                if vals:
                    con.execute(f"UPDATE mitarbeiter SET {', '.join(k + ' = ?' for k in vals)} WHERE id = ?", (*vals.values(), int(b["id"])))
                return {"ok": True, "id": int(b["id"])}
            if not vals.get("nachname"):
                raise ValueError("Bitte mindestens den Nachnamen angeben.")
            vals.setdefault("vorname", "")
            vals["erstellt"] = now()
            cur = con.execute(f"INSERT INTO mitarbeiter ({', '.join(vals)}) VALUES ({','.join('?' * len(vals))})", tuple(vals.values()))
            return {"ok": True, "id": cur.lastrowid}
        return run(save)

    @r.post("/monat")
    async def monatswert(request: Request):
        """Ein Monatswert von Hand; leerer Wert = wieder den Staffomatic-Wert nehmen."""
        check_local(request, write=True)
        b = await request.json()

        def save(con):
            mid, mon = int(b["mitarbeiter_id"]), _monat(b["monat"])
            con.execute("INSERT INTO monatswerte (mitarbeiter_id, monat) VALUES (?, ?) ON CONFLICT DO NOTHING", (mid, mon))
            vals = {k: _num(b[k]) for k in MV_NUM if k in b}
            if "bemerkung" in b:
                vals["bemerkung"] = str(b["bemerkung"] or "").strip()[:1000]
            if not vals:
                raise ValueError("Kein Feld angegeben.")
            con.execute(f"UPDATE monatswerte SET {', '.join(k + ' = ?' for k in vals)} WHERE mitarbeiter_id = ? AND monat = ?", (*vals.values(), mid, mon))
        return run(save)

    @r.post("/abwesenheit")
    async def abwesenheit(request: Request):
        check_local(request, write=True)
        b = await request.json()

        def save(con):
            if b.get("loeschen"):
                con.execute("DELETE FROM abwesenheiten WHERE id = ?", (int(b["id"]),))
                return
            art = str(b.get("art", ""))
            if art not in ABWESENHEITEN:
                raise ValueError("Unbekannte Art.")
            von, bis = _datum(b["von"]), _datum(b.get("bis") or b["von"])
            if bis < von:
                raise ValueError("Bis liegt vor Von.")
            mid = int(b["mitarbeiter_id"])
            m = con.execute("SELECT std_pro_tag FROM mitarbeiter WHERE id = ?", (mid,)).fetchone()
            spt = (m and m["std_pro_tag"]) or settings(con)["std_pro_tag"] or 10
            tage = _num(b.get("tage"))
            if tage is None:  # Werktage Mo-Fr im Zeitraum
                d0, d1 = date.fromisoformat(von), date.fromisoformat(bis)
                tage = sum(1 for i in range((d1 - d0).days + 1) if (d0 + timedelta(days=i)).weekday() < 5) or 1
            std = _num(b.get("stunden"))
            std = round(tage * spt, 2) if std is None else std
            con.execute("INSERT INTO abwesenheiten (mitarbeiter_id, art, von, bis, stunden, tage, notiz, erstellt) VALUES (?,?,?,?,?,?,?,?)",
                        (mid, art, von, bis, std, tage, str(b.get("notiz") or "")[:500], now()))
        return run(save)

    @r.post("/buchung")
    async def buchung(request: Request):
        check_local(request, write=True)
        b = await request.json()

        def save(con):
            if b.get("loeschen"):
                con.execute("DELETE FROM buchungen WHERE id = ?", (int(b["id"]),))
                return
            art = str(b.get("art", ""))
            if art not in BUCHUNGSARTEN:
                raise ValueError("Unbekannte Art.")
            betrag, std = _num(b.get("betrag")) or 0, _num(b.get("stunden")) or 0
            if not betrag and not std:
                raise ValueError("Bitte Betrag oder Stunden angeben.")
            con.execute("INSERT INTO buchungen (mitarbeiter_id, monat, art, betrag, stunden, notiz, erstellt) VALUES (?,?,?,?,?,?,?)",
                        (int(b["mitarbeiter_id"]), _monat(b["monat"]), art, betrag, std, str(b.get("notiz") or "")[:500], now()))
        return run(save)

    @r.post("/einsatzort")
    async def einsatzort(request: Request):
        check_local(request, write=True)
        b = await request.json()

        def save(con):
            land = str(b.get("land") or "")
            if land and land not in LAENDER:
                raise ValueError("Unbekanntes Bundesland.")
            con.execute("INSERT INTO einsatzorte (bereich, land, objektzulage_std, geprueft) VALUES (?,?,?,1) ON CONFLICT(bereich) DO UPDATE SET "
                        "land = excluded.land, objektzulage_std = excluded.objektzulage_std, geprueft = 1",
                        (str(b["bereich"]), land, _num(b.get("objektzulage_std")) or 0))
        return run(save)

    @r.post("/einstellungen")
    async def einstellungen(request: Request):
        check_local(request, write=True)
        b = await request.json()

        def save(con):
            for k in DEFAULTS:
                if k in b:
                    v = b[k]
                    if k in ("nacht_von", "nacht_bis"):
                        if not re.fullmatch(r"\d{1,2}:\d{2}", str(v)):
                            raise ValueError("Uhrzeit bitte als HH:MM")
                    elif k != "bereichsleitung":
                        v = _num(v, False)
                    con.execute("INSERT INTO einstellungen (schluessel, wert) VALUES (?, ?) ON CONFLICT(schluessel) DO UPDATE SET wert = excluded.wert",
                                (k, json.dumps(v, ensure_ascii=False)))
        return run(save)

    @r.post("/staffo")
    async def staffo(request: Request):
        check_local(request, write=True)
        b = await request.json()
        try:
            return await asyncio.to_thread(staffo_import, _monat(b.get("monat")))
        except staffo_tools.StaffoError as e:
            return {"ok": False, "fehler": str(e)}
        except ValueError as e:
            return {"ok": False, "fehler": str(e)}

    @r.get("/einsaetze")
    async def einsaetze(request: Request, mitarbeiter_id: int, monat: str):
        check_local(request)
        con = db()
        rows = [dict(x) for x in con.execute("SELECT * FROM einsaetze WHERE mitarbeiter_id = ? AND monat = ? ORDER BY beginn", (mitarbeiter_id, monat))]
        con.close()
        return {"einsaetze": rows}

    @r.get("/staffo-mitarbeiter")
    async def staffo_mitarbeiter(request: Request):
        """Alle aktiven Staffomatic-Mitarbeiter zum Zuordnen in der Personalakte."""
        check_local(request)
        try:
            return {"mitarbeiter": [{"id": u["id"], "name": u["name"]} for u in await asyncio.to_thread(staffo_tools.users)]}
        except staffo_tools.StaffoError as e:
            return {"mitarbeiter": [], "fehler": str(e)}

    @r.get("/vorschau")
    async def vorschau(request: Request, tage: int = 14):
        check_local(request)
        try:
            return await asyncio.to_thread(staffo_vorschau, max(1, min(tage, 31)))
        except staffo_tools.StaffoError as e:
            return {"fehler": str(e)}

    @r.post("/lohnabrechnung")
    async def lohnabrechnung(request: Request):
        check_local(request, write=True)
        b = await request.json()
        try:
            ids = [int(x) for x in b.get("mitarbeiter") or []] or None
            return await asyncio.to_thread(lohnabrechnungen, config, _monat(b.get("monat")), ids)
        except (ValueError, StopIteration) as e:
            return {"ok": False, "fehler": str(e) or "Monat nicht gefunden."}
        except OSError as e:
            return {"ok": False, "fehler": f"Datei konnte nicht gespeichert werden: {e}"}

    @r.post("/dokument")
    async def dokument(request: Request, datei: UploadFile = File(...), mitarbeiter_id: int = Form(...),
                       kategorie: str = Form("Sonstiges"), datum: str = Form(""), zusatz: str = Form("")):
        """Datei hochladen: wird einheitlich benannt und im Ordner des Mitarbeiters abgelegt."""
        check_local(request, write=True)
        if kategorie not in KATEGORIEN:
            raise HTTPException(400)
        data = await datei.read(dokumente_tools.MAX_BYTES + 1)
        if len(data) > dokumente_tools.MAX_BYTES:
            return {"ok": False, "fehler": "Datei ist größer als 30 MB."}
        try:
            datum = _datum(datum) if datum else date.today().isoformat()
        except ValueError:
            return {"ok": False, "fehler": "Datum bitte als JJJJ-MM-TT."}

        def save():
            con = db()
            m = con.execute("SELECT * FROM mitarbeiter WHERE id = ?", (mitarbeiter_id,)).fetchone()
            if not m:
                con.close()
                return {"ok": False, "fehler": "Mitarbeiter nicht gefunden."}
            folder = os.path.join(ma_folder(config, dict(m)), kategorie)
            ext = os.path.splitext(datei.filename or "")[1][:10] or ".pdf"
            path = dokumente_tools.unique_path(folder, doc_name(dict(m), kategorie, datum, zusatz, ext))
            with open(path, "wb") as f:
                f.write(data)
            did = register_doc(con, mitarbeiter_id, kategorie, datum, zusatz or kategorie, path)
            con.commit()
            con.close()
            return {"ok": True, "id": did, "datei": os.path.basename(path)}
        return await asyncio.to_thread(save)

    @r.get("/dokument/datei")
    async def dokument_datei(request: Request, id: int):
        check_local(request)
        con = db()
        d = con.execute("SELECT pfad FROM dokumente WHERE id = ?", (id,)).fetchone()
        con.close()
        if not d or not os.path.exists(d["pfad"]):
            raise HTTPException(404)
        return FileResponse(d["pfad"], content_disposition_type="inline", filename=os.path.basename(d["pfad"]))

    @r.post("/dokument/loeschen")
    async def dokument_loeschen(request: Request):
        """Nur der Eintrag verschwindet; die Datei wandert in den Unterordner _Papierkorb."""
        check_local(request, write=True)
        b = await request.json()

        def drop(con):
            d = con.execute("SELECT * FROM dokumente WHERE id = ?", (int(b["id"]),)).fetchone()
            if not d:
                raise ValueError("Dokument nicht gefunden.")
            if os.path.exists(d["pfad"]):
                trash = os.path.join(personal_dir(config), "_Papierkorb")
                os.makedirs(trash, exist_ok=True)
                os.replace(d["pfad"], dokumente_tools.unique_path(trash, os.path.basename(d["pfad"])))
            con.execute("DELETE FROM dokumente WHERE id = ?", (d["id"],))
        return run(drop)

    return r
