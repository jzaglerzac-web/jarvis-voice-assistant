"""
ZAC Staffo — local MCP server (stdio), read-only access to Staffomatic through
staffo_tools.py and the staffomatic_* fields in config.json. Changes are not
offered here; they only go through Jarvis with an explicit "ja".
"""

import json
from datetime import date, timedelta

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

import staffo_tools

mcp = MCPServer("zac_staffo", instructions=(
    "Liest den Dienstplan der ZAC GmbH aus Staffomatic (nur lesend): Schichten, offene Schichten, Mitarbeiter."))

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)


def _dates(von: str, bis: str) -> tuple[date, date]:
    start = date.fromisoformat(von) if von else date.today()
    end = date.fromisoformat(bis) if bis else start
    if end < start:
        raise ValueError("bis liegt vor von")
    if (end - start).days > 62:
        raise ValueError("hoechstens 62 Tage auf einmal")
    return start, end


def _answer(fn) -> str:
    try:
        return json.dumps(fn(), ensure_ascii=False)
    except (staffo_tools.StaffoError, ValueError) as e:
        return json.dumps({"fehler": str(e)}, ensure_ascii=False)


@mcp.tool(annotations=READ_ONLY)
def staffo_shifts(von: str = "", bis: str = "") -> str:
    """Schichten im Zeitraum (Datum JJJJ-MM-TT, leer = heute; bis leer = gleicher Tag).
    Alle Standorte des Kontos. Antwort (JSON): schichten [{id, schedule_id, location_id, standort, beginn, ende, bereich, mitarbeiter, soll, offen, notiz}]."""
    return _answer(lambda: {"schichten": staffo_tools.shifts(*_dates(von, bis))})


@mcp.tool(annotations=READ_ONLY)
def staffo_open_shifts(von: str = "", bis: str = "") -> str:
    """Unbesetzte oder unterbesetzte Schichten (Datum JJJJ-MM-TT; leer = heute bis in 7 Tagen).
    Alle Standorte des Kontos. Antwort (JSON): schichten [{id, schedule_id, location_id, standort, beginn, ende, bereich, mitarbeiter, soll, offen, notiz}]."""
    if not von and not bis:
        von, bis = date.today().isoformat(), (date.today() + timedelta(days=7)).isoformat()
    return _answer(lambda: {"schichten": staffo_tools.open_shifts(*_dates(von, bis))})


@mcp.tool(annotations=READ_ONLY)
def staffo_users() -> str:
    """Aktive Mitarbeiter aller Standorte. Antwort (JSON): mitarbeiter [{id, name, rolle, bereiche, standorte}]."""
    return _answer(lambda: {"mitarbeiter": staffo_tools.users()})


if __name__ == "__main__":
    mcp.run("stdio")
