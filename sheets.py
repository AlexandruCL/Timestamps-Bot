"""Google Sheets access: the client, Concedii parsing, and 5-minute cached raw reads for the web API."""
import asyncio
import datetime
import os
import re
import time

import gspread
from dotenv import load_dotenv

load_dotenv()

GOOGLE_SHEETS_CREDENTIALS_FILE = os.getenv("GOOGLE_SHEETS_CREDENTIALS_FILE", "credentials.json").strip()
PONTAJE_SHEET_ID = os.getenv("PONTAJE_SHEET_ID", "").strip()
PONTAJE_SHEET_TAB = "Monitorizare Activitate"
PD_SPREADSHEET_ID = os.getenv("PD_SPREADSHEET_ID", "").strip()
PD_MEMBERS_TAB = "Evidenta membrii"
PD_LEAVE_TAB = "Concedii"
SHEET_CACHE_TTL_SECONDS = int(os.getenv("SHEET_CACHE_TTL_SECONDS", "300"))


def sheets_client() -> gspread.Client:
    # A week send is ~40 requests against Google's ~60 reads/minute; back off and retry instead of failing mid-week.
    return gspread.service_account(filename=GOOGLE_SHEETS_CREDENTIALS_FILE, http_client=gspread.BackOffHTTPClient)


def parse_sheet_date(text: str, year: int) -> datetime.date | None:
    """Concedii dates are typed as dd.mm, dd/mm, dd.mm.yyyy or dd/mm/yyyy; a missing year means `year`."""
    m = re.fullmatch(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{4}))?", text.strip())
    if not m:
        return None
    try:
        return datetime.date(int(m.group(3) or year), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def leave_ranges(rows: list[list[str]], year: int) -> list[tuple[str, datetime.date, datetime.date]]:
    """(3-digit callsign, start, end inclusive) from Concedii rows D:F; rows without a callsign or dates are skipped."""
    out = []
    for row in rows:
        cs = row[0].strip() if row else ""
        if len(row) < 3 or not cs.isdigit():
            continue
        start, end = parse_sheet_date(row[1], year), parse_sheet_date(row[2], year)
        if not start or not end:
            continue
        if end < start and not re.search(r"\d{4}", row[2]):  # e.g. 28.12 -> 03.01
            end = end.replace(year=end.year + 1)
        out.append((cs.zfill(3), start, end))
    return out


def leave_days(rows: list[list[str]], dates: list[str]) -> dict[str, set[str]]:
    """Callsign -> the given dates covered by one of its concedii."""
    out: dict[str, set[str]] = {}
    for cs, start, end in leave_ranges(rows, datetime.date.fromisoformat(dates[0]).year):
        covered = {d for d in dates if start.isoformat() <= d <= end.isoformat()}
        if covered:
            out.setdefault(cs, set()).update(covered)
    return out


def concedii_on(gc: gspread.Client, dates: list[str]) -> dict[str, set[str]]:
    return leave_days(gc.open_by_key(PD_SPREADSHEET_ID).worksheet(PD_LEAVE_TAB).get("D:F"), dates)


assert parse_sheet_date("16.11", 2026) == datetime.date(2026, 11, 16)
assert parse_sheet_date("21/11/2026", 2000) == datetime.date(2026, 11, 21)
assert parse_sheet_date("CS indisponibil!", 2026) is None


def renamed_rows(members: list[list[str]], callsigns: list[str], names: list[str], days: list[list]) -> tuple[list[str], list[list]]:
    """Python port of the sheet's Apps Script updateNames.
    A member whose callsign changed takes their F:L week to the row of the new callsign, every C name is rewritten
    from Evidenta membrii, and rows left without a member lose their F:L. Returns (new names, new days)."""
    pairs = [(r[0].strip(), r[1].strip()) for r in members if len(r) > 1 and r[0].strip() and r[1].strip()]
    name_to_cs, cs_to_name = {nm: cs for cs, nm in pairs}, dict(pairs)
    moves = [(i, callsigns.index(name_to_cs[nm])) for i, (cs, nm) in enumerate(zip(callsigns, names))
             if cs and name_to_cs.get(nm, cs) != cs and name_to_cs[nm] in callsigns]
    # Clear every source before filling targets so a callsign swap keeps both weeks (the Apps Script lost one).
    old, days = days, [list(d) for d in days]
    for i, _ in moves:
        days[i] = [""] * 7
    for i, j in moves:
        days[j] = list(old[i])
    new_names = [cs_to_name.get(cs, "") for cs in callsigns]
    return new_names, [d if nm else [""] * 7 for nm, d in zip(new_names, days)]


_e = [""] * 7
assert renamed_rows(
    [["101", "Ana"], ["102", "Ion"]], ["101", "102", "103"], ["Ion", "Ana", "Gone"], [[1] + _e[1:], [2] + _e[1:], [3] + _e[1:]]
) == (["Ana", "Ion", ""], [[2] + _e[1:], [1] + _e[1:], _e])  # Ion and Ana swapped callsigns; 103 left


def update_pontaje_names() -> int:
    """Run renamed_rows on Monitorizare Activitate rows 5+ and write only the rows that changed. Returns how many."""
    gc = sheets_client()
    members = gc.open_by_key(PD_SPREADSHEET_ID).worksheet(PD_MEMBERS_TAB).get("B2:C")
    ws = gc.open_by_key(PONTAJE_SHEET_ID).worksheet(PONTAJE_SHEET_TAB)
    cs_col, name_col = ws.batch_get(["B5:B", "C5:C"])
    # Unformatted so minutes go back as numbers, not "120" strings.
    day_rows = ws.get("F5:L", value_render_option=gspread.utils.ValueRenderOption.unformatted)
    n = max(len(cs_col), len(name_col), len(day_rows))
    cell = lambda vals, i: str(vals[i][0]).strip() if i < len(vals) and vals[i] else ""
    callsigns, names = [cell(cs_col, i) for i in range(n)], [cell(name_col, i) for i in range(n)]
    days = [((day_rows[i] if i < len(day_rows) else []) + [""] * 7)[:7] for i in range(n)]
    new_names, new_days = renamed_rows(members, callsigns, names, days)
    updates, rows = [], set()
    for i in range(n):
        if new_names[i] != names[i]:
            updates.append({"range": f"C{i + 5}", "values": [[new_names[i]]]})
            rows.add(i)
        if new_days[i] != days[i]:
            updates.append({"range": f"F{i + 5}:L{i + 5}", "values": [new_days[i]]})
            rows.add(i)
    if updates:
        ws.batch_update(updates)
    return len(rows)

# Cached raw reads for the web API
_cache: dict[str, tuple[float, list]] = {}
_locks: dict[str, asyncio.Lock] = {}
_client: gspread.Client | None = None
last_pull: dict = {"at": None, "by": None}  # last Google read; "by" is set only by a manual sync


def _read(sheet_id: str, tab: str, rng: str) -> list[list[str]]:
    global _client
    _client = _client or sheets_client()
    return _client.open_by_key(sheet_id).worksheet(tab).get(rng)


async def _cached(name: str, sheet_id: str, tab: str, rng: str) -> list[list[str]]:
    async with _locks.setdefault(name, asyncio.Lock()):  # one Google read per miss, however many callers wait
        hit = _cache.get(name)
        if hit and time.monotonic() - hit[0] < SHEET_CACHE_TTL_SECONDS:
            return hit[1]
        rows = await asyncio.to_thread(_read, sheet_id, tab, rng)
        _cache[name] = (time.monotonic(), rows)
        last_pull.update(at=int(time.time()), by=None)
        return rows


async def read_members() -> list[list[str]]:
    """Evidenta membrii B:T (B callsign, C name, E grade, H corp, I status, J hired, T Discord ID)."""
    return await _cached("members", PD_SPREADSHEET_ID, PD_MEMBERS_TAB, "B:T")


async def read_concedii() -> list[list[str]]:
    """Concedii D:F (callsign, start, end)."""
    return await _cached("concedii", PD_SPREADSHEET_ID, PD_LEAVE_TAB, "D:F")


async def read_pontaje_week() -> list[list[str]]:
    """Monitorizare Activitate B:R (B callsign, C name, D rank, E TOTAL/ZI, F-L Lu..Du, M TOTAL, N-R older averages)."""
    return await _cached("pontaje", PONTAJE_SHEET_ID, PONTAJE_SHEET_TAB, "B:R")


def invalidate(*names: str):
    """Drop the named cache entries, or all of them."""
    for n in names or list(_cache):
        _cache.pop(n, None)
