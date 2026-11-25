"""Pure calculations behind the web pages (design §4). Inputs are raw sheet rows plus DB minutes, so tests need no I/O.

minutes: {(discord_id, "YYYY-MM-DD"): rounded minutes that day} from clock_times.db.
leave:   {callsign: {dates on concediu}} from sheets.leave_days.
"""
import datetime

MEMBER_STATUSES = ("ACTIV", "CONCEDIU")
CONDUCERE_CORP = "CONDUCERE"


def _cell(row: list[str], i: int) -> str:
    return row[i].strip() if i < len(row) and row[i] else ""


def _iso_from_ddmmyyyy(text: str) -> str | None:
    try:
        return datetime.datetime.strptime(text, "%d/%m/%Y").date().isoformat()
    except ValueError:
        return None


def parse_members(rows: list[list[str]]) -> list[dict]:
    """Evidenta membrii B:T rows -> members (a name and status ACTIV/CONCEDIU), in sheet order."""
    out = []
    for r in rows:
        name, status = _cell(r, 1), _cell(r, 7).upper()
        cs = _cell(r, 0)
        if not name or status not in MEMBER_STATUSES or not cs.isdigit():
            continue
        did = _cell(r, 18)
        out.append({
            "callsign": cs.zfill(3), "name": name, "grade": _cell(r, 3), "corp": _cell(r, 6).upper(), "status": status,
            "hired": _iso_from_ddmmyyyy(_cell(r, 8)), "discord_id": did if did.isdigit() else None,
        })
    return out


def parse_pontaje(rows: list[list[str]]) -> list[dict]:
    """Monitorizare Activitate B:R rows with a callsign and a name: E avg, F..L Lu..Du, M total."""
    out = []
    for r in rows:
        cs, name = _cell(r, 0), _cell(r, 1)
        if cs.isdigit() and name:
            out.append({"callsign": cs.zfill(3), "name": name, "rank": _cell(r, 2), "avg": _cell(r, 3), "days": [_cell(r, 4 + i) for i in range(7)]})
    return out


def sheet_value(text: str):
    """A day cell as the API shows it: int minutes, "CO", "/", or None (empty / anything else)."""
    t = text.strip().upper()
    if t in ("CO", "/"):
        return t
    try:
        return int(float(t.replace(",", ".")))
    except ValueError:
        return None


def avg_value(text: str) -> str | None:
    """Column E (TOTAL / ZI); "Se calculează..." and "/" mean there is nothing to show."""
    return None if not text or text == "/" or text.lower().startswith("se calcul") else text


def _groups(members: list[dict]) -> list[str]:
    return list(dict.fromkeys(m["grade"] for m in members))


def _week_value(m: dict | None, cs: str, date: str, sheet_text: str, *, sent: set[str], today: str,
                minutes: dict, leave: dict, daily_target: int) -> dict:
    """Sent days come from the sheet; the others from concedii and the DB, like push_day_to_sheet would write them."""
    if date in sent:
        return {"value": sheet_value(sheet_text), "live": False}
    if date in leave.get(cs, ()):
        return {"value": "CO", "live": date <= today}
    if date > today or not m or not m["discord_id"]:
        return {"value": None, "live": False}
    mins = minutes.get((int(m["discord_id"]), date), 0)
    value = "/" if m["hired"] == date and mins < daily_target else mins
    return {"value": value, "live": True}


def pontaje_week(pontaje_rows, member_rows, *, week: list[str], today: str, sent: set[str], minutes: dict, leave: dict, daily_target: int) -> list[dict]:
    """The week grid, Du..Sa. Sheet columns F..L are Lu..Du, so Sunday is index 6."""
    by_cs = {m["callsign"]: m for m in parse_members(member_rows)}
    out = []
    for p in parse_pontaje(pontaje_rows):
        m = by_cs.get(p["callsign"])
        cells = [
            _week_value(m, p["callsign"], d, p["days"][datetime.date.fromisoformat(d).weekday()],
                        sent=sent, today=today, minutes=minutes, leave=leave, daily_target=daily_target)
            for d in week
        ]
        nums = [c["value"] for c in cells if isinstance(c["value"], int)]
        out.append({"callsign": p["callsign"], "name": p["name"], "grade": m["grade"] if m else p["rank"], "cells": cells, "total": sum(nums) if nums else None})
    return out


def members_page(member_rows, pontaje_rows) -> list[dict]:
    avg = {p["callsign"]: avg_value(p["avg"]) for p in parse_pontaje(pontaje_rows)}
    members = parse_members(member_rows)
    return [
        {"grade": g, "members": [
            {"discord_id": m["discord_id"], "callsign": m["callsign"], "name": m["name"], "grade": m["grade"],
             "hired": m["hired"], "avg": avg.get(m["callsign"]), "on_leave": m["status"] == "CONCEDIU"}
            for m in members if m["grade"] == g
        ]}
        for g in _groups(members)
    ]


def _pct(num, den):
    return round(num * 100 / den, 1) if den else None


def _active_counts(members: list[dict], date: str, minutes: dict, leave: dict) -> tuple[int, int]:
    """(active, eligible) that day: eligible = hired by then, has a Discord ID, not on concediu; active = >= 1 minute."""
    eligible = [m for m in members if m["discord_id"] and (not m["hired"] or m["hired"] <= date) and date not in leave.get(m["callsign"], ())]
    return sum(minutes.get((int(m["discord_id"]), date), 0) >= 1 for m in eligible), len(eligible)


def dashboard(member_rows, grid: list[dict], *, today: str, week: list[str], minutes: dict, leave: dict) -> dict:
    """grid = pontaje_week(...) for the current week; leave must cover the week and yesterday."""
    members = parse_members(member_rows)
    conducere = [m for m in members if m["corp"] == CONDUCERE_CORP]
    others = [m for m in members if m["corp"] != CONDUCERE_CORP]
    count_by = lambda ms: [{"grade": g, "count": sum(m["grade"] == g for m in ms)} for g in _groups(ms)]

    yesterday = (datetime.date.fromisoformat(today) - datetime.timedelta(days=1)).isoformat()
    y_active, y_eligible = _active_counts(others, yesterday, minutes, leave)

    elapsed = [d for d in week if d < today]
    daily = [_active_counts(others, d, minutes, leave) for d in elapsed]
    daily = [(a, e) for a, e in daily if e]
    n = len(daily)

    grade_activity = []
    for g in _groups(others):
        ms = [m for m in others if m["grade"] == g]
        counts = [_active_counts(ms, d, minutes, leave) for d in elapsed]
        grade_activity.append({"grade": g, "pct": _pct(sum(a for a, _ in counts), sum(e for _, e in counts))})

    ranked = sorted((r for r in grid if r["total"]), key=lambda r: -r["total"])[:10]
    return {
        "conducere": {"count": len(conducere), "by_grade": count_by(conducere)},
        "efectiv": {"total": len(members), "on_leave": sum(m["status"] == "CONCEDIU" for m in members)},
        "grades": count_by(others),
        "active_yesterday": {"pct": _pct(y_active, y_eligible), "active": y_active, "eligible": y_eligible},
        "active_week": {
            "pct": round(sum(a * 100 / e for a, e in daily) / n, 1) if n else None,
            "avg_active": round(sum(a for a, _ in daily) / n, 1) if n else None,
            "avg_eligible": round(sum(e for _, e in daily) / n, 1) if n else None,
            "days": n,
        },
        "top5": [{"callsign": r["callsign"], "name": r["name"], "grade": r["grade"], "total": r["total"]} for r in ranked],
        "grade_activity": grade_activity,
    }


def concedii_week(ranges: list[tuple[str, datetime.date, datetime.date]], member_rows, *, week: list[str], today: str) -> list[dict]:
    """Leaves overlapping the week, by start date. ranges = sheets.leave_ranges(...)."""
    by_cs = {m["callsign"]: m for m in parse_members(member_rows)}
    out = []
    for cs, start, end in sorted(ranges, key=lambda r: (r[1], r[0])):
        s, e = start.isoformat(), end.isoformat()
        if e < week[0] or s > week[-1]:
            continue
        status = "ended" if e < today else "scheduled" if s > today else "ongoing"
        m = by_cs.get(cs, {})
        out.append({"callsign": cs, "name": m.get("name"), "grade": m.get("grade"), "start": s, "end": e, "status": status, "days": [s <= d <= e for d in week]})
    return out
