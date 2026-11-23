"""Encrypted RPC API for the Dashboard Phoenix web app (design §2.1, §5, §8): POST /api/web/rpc."""
import asyncio
import datetime
import logging
import os
import re
import time
import uuid
from collections import deque
from urllib.parse import parse_qsl, urlsplit

import aiohttp
import discord
from aiohttp import web

import bot2 as b  # the running bot module (bot2.py registers itself under this name when run as a script)
import database as db
import metrics
import services
import sheets
from envelope import Rejected, ReplayGuard, decode_request, load_keys, seal

KEYS = load_keys(os.getenv("WEB_API_KEY"), os.getenv("WEB_API_KEY_OLD"))
WEB_API_HOST = os.getenv("WEB_API_HOST", "0.0.0.0").strip()
WEB_API_PORT = int(os.getenv("WEB_API_PORT") or b.PONTAJE_API_PORT)
HR_ROLE_IDS = b.id_list("WEB_HR_ROLE_IDS") or [b.REQUIRED_HR_ROLE_ID]
CONDUCERE_ROLE_IDS = b.id_list("WEB_CONDUCERE_ROLE_IDS") or [b.CONDUCERE_ROLE_ID]
ROLE_CACHE_TTL_SECONDS = int(os.getenv("ROLE_CACHE_TTL_SECONDS", "300"))
RATE_LIMIT_PER_MINUTE = 240
READ_ONLY = False  # the local dev runner sets this so a test click can't DM members or write the real sheet

HR, CONDUCERE = "HR", "Conducere"
GUARD = ReplayGuard()


class ApiError(Exception):
    def __init__(self, status: int, error: str, message: str | None = None):
        super().__init__(error)
        self.status, self.body = status, {"error": error, **({"message": message} if message else {})}


def invalid(message: str) -> ApiError:
    return ApiError(422, "invalid", message)


# Access (design §5): one fetch_member per user per ROLE_CACHE_TTL_SECONDS
class WebActor:
    """Stands in for a discord.Member in logs when the user is not (or no longer) in the server."""
    def __init__(self, uid: int, name: str):
        self.id, self.name, self.display_name, self.mention = uid, name, name, f"<@{uid}>"

    def __str__(self):
        return self.name


_roles: dict[int, tuple[str | None, discord.Member | None, float]] = {}
_guild: discord.Guild | None = None


async def home_guild() -> discord.Guild:
    """The PD server. From the gateway cache when connected; over REST otherwise (dev runner)."""
    global _guild
    ch = b.bot.get_channel(b.ALLOWED_CHANNEL_ID)
    if ch:
        return ch.guild
    if _guild is None:
        ch = await b.bot.fetch_channel(b.ALLOWED_CHANNEL_ID)
        _guild = await b.bot.fetch_guild(ch.guild.id)
    return _guild


def level_of(member: discord.Member | None) -> str | None:
    if member is None:
        return None
    return CONDUCERE if b.has_any(member, CONDUCERE_ROLE_IDS) else HR if b.has_any(member, HR_ROLE_IDS) else None


async def access(uid: int) -> tuple[str | None, discord.Member | None]:
    hit = _roles.get(uid)
    if hit and time.monotonic() - hit[2] < ROLE_CACHE_TTL_SECONDS:
        return hit[0], hit[1]
    try:
        guild = await home_guild()
        try:
            member = await guild.fetch_member(uid)
        except discord.NotFound:
            member = None
    except (discord.HTTPException, aiohttp.ClientError, OSError, asyncio.TimeoutError) as e:
        # not cached, so the next call asks again; nothing here retries in a loop
        logging.warning("Web API role check failed for %s: %s", uid, e)
        raise ApiError(503, "discord_unavailable")
    level = level_of(member)
    _roles[uid] = (level, member, time.monotonic())
    return level, member


class Ctx:
    def __init__(self, actor_id: int, query: dict, body: dict, params: tuple):
        self.actor_id, self.query, self.body, self.params = actor_id, query, body, params
        self.level: str | None = None
        self.actor: discord.Member | None = None


def get_date(value, name: str = "date") -> str:
    if not isinstance(value, str) or not b.valid_date(value):
        raise invalid(f"{name}: format YYYY-MM-DD")
    return value


def get_uid(value) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not value.isdigit():
        raise invalid("uid: ID Discord invalid")
    return int(value)


def get_clock(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}:\d{2}", value):
        raise invalid("clock_in: format HH:MM:SS")
    return value


def get_minutes(value) -> float:
    """Same rule as AddMinutesModal: a number between 1 and 1440 (comma decimals accepted)."""
    try:
        m = float(value.strip().replace(",", ".")) if isinstance(value, str) else float(value)
    except (TypeError, ValueError):
        raise invalid("Introdu un număr.")
    if isinstance(value, bool) or not 0 < m <= 1440:  # also rejects nan/inf
        raise invalid("Trebuie să fie între 1 și 1440.")
    return m


def get_text(value, name: str, max_len: int, required: bool) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str) or len(value) > max_len or (required and not value.strip()):
        raise invalid(f"{name}: {'obligatoriu, ' if required else ''}maxim {max_len} caractere")
    return value


async def get_member(uid: int) -> discord.Member:
    found = await b.fetch_members(await home_guild(), [uid])
    if not found:
        raise ApiError(404, "member_not_found")
    return found[0]


def split_name(display_name: str) -> tuple[str | None, str]:
    """"[12] Ion Pop" -> ("012", "Ion Pop")."""
    m = b.PD_CALLSIGN_RE.search(display_name)
    if not m:
        return None, display_name
    return m.group(1).zfill(3), (display_name[:m.start()] + display_name[m.end():]).strip(" -|")


def db_minutes(dates: list[str]) -> dict[tuple[int, str], int]:
    out: dict[tuple[int, str], int] = {}
    for uid, d, ci, co in db.get_clock_times_for_dates(dates):
        if ci:
            out[(uid, d)] = out.get((uid, d), 0) + b.session_minutes(d, ci, co)
    return out


def week_totals(uid: int, date: str) -> dict:
    return {"day": b.day_minutes(uid, date), "week": sum(b.day_minutes(uid, d) for d in b.week_dates(datetime.date.fromisoformat(date)))}


async def sent_info(dates: list[str]) -> list[dict]:
    sent = [(d, s) for d in dates if (s := b.get_sheet_export(d))]
    names = await b.resolve_display_names(await home_guild(), {uid for _, (uid, _) in sent}) if sent else {}
    return [{"date": d, "by": names.get(uid, str(uid)), "by_id": str(uid), "at": ts} for d, (uid, ts) in sent]


async def ping(ctx):
    return "pong"


async def me(ctx):
    cs, name = split_name(ctx.actor.display_name)
    return {"id": str(ctx.actor.id), "name": name, "callsign": cs, "level": ctx.level, "can_export": ctx.actor.id in b.SHEET_EXPORT_USER_IDS}


async def login(ctx):
    """Logs web-login, or web-denied (403) for someone without an allowed role."""
    level, member = await access(ctx.actor_id)
    name = ctx.body.get("name") if isinstance(ctx.body.get("name"), str) else ""
    if level is None:
        await b.log_action(member or WebActor(ctx.actor_id, name[:100] or str(ctx.actor_id)), "web-denied", success=False, source="Web")
        raise ApiError(403, "forbidden")
    ctx.level, ctx.actor = level, member
    await b.log_action(member, "web-login", source="Web")
    return await me(ctx)


def _today_week() -> tuple[str, list[str]]:
    today = b.local_now().date()
    return today.isoformat(), b.week_dates(today)


async def _week_grid(today: str, week: list[str], extra_dates: list[str] = ()):
    members, pontaje, conc = await asyncio.gather(sheets.read_members(), sheets.read_pontaje_week(), sheets.read_concedii())
    dates = sorted({*week, *extra_dates})
    minutes, leave = db_minutes(dates), sheets.leave_days(conc, dates)
    sent = {d for d in week if b.get_sheet_export(d)}
    grid = metrics.pontaje_week(pontaje, members, week=week, today=today, sent=sent, minutes=minutes, leave=leave,
                                daily_target=b.DAILY_TARGET_MINUTES)
    return members, grid, minutes, leave


async def dashboard(ctx):
    today, week = _today_week()
    yesterday = b.shift_date(today, -1)
    members, grid, minutes, leave = await _week_grid(today, week, [yesterday])
    return metrics.dashboard(members, grid, today=today, week=week, minutes=minutes, leave=leave)


async def members_page(ctx):
    members, pontaje = await asyncio.gather(sheets.read_members(), sheets.read_pontaje_week())
    return {"groups": metrics.members_page(members, pontaje)}


async def pontaje(ctx):
    today, week = _today_week()
    _, grid, _, _ = await _week_grid(today, week)
    return {"week_start": week[0], "days": week, "daily_target": b.DAILY_TARGET_MINUTES, "rows": grid}


async def concedii(ctx):
    today, week = _today_week()
    members, conc = await asyncio.gather(sheets.read_members(), sheets.read_concedii())
    ranges = sheets.leave_ranges(conc, int(today[:4]))
    return {"week_start": week[0], "rows": metrics.concedii_week(ranges, members, week=week, today=today)}


async def sessions_open(ctx):
    today = b.today_str()
    rows = [(uid, ci) for uid, d, ci in b.get_ongoing_sessions() if d == today]
    if not rows:
        return {"rows": []}
    names = await b.resolve_display_names(await home_guild(), [uid for uid, _ in rows])
    by_id = {m["discord_id"]: m for m in metrics.parse_members(await sheets.read_members())}
    return {"rows": [{"uid": str(uid), "name": names.get(uid, str(uid)), "callsign": by_id.get(str(uid), {}).get("callsign"),
                      "grade": by_id.get(str(uid), {}).get("grade"), "date": today, "clock_in": ci,
                      "minutes": b.session_minutes(today, ci, None)} for uid, ci in rows]}


async def sessions_stop(ctx):
    uid, date, ci = get_uid(ctx.body.get("uid")), get_date(ctx.body.get("date")), get_clock(ctx.body.get("clock_in"))
    if not any(s_ci == ci and s_co is None for s_ci, s_co in b.get_clock_times(uid, date)):
        raise ApiError(404, "session_not_found", "Sesiunea nu mai este deschisă.")
    await services.stop_session(ctx.actor, await home_guild(), uid, date, ci, source="Web")
    return {"ok": True}


async def day_report(ctx):
    date = get_date(ctx.query.get("date"))
    member = await get_member(get_uid(ctx.query["uid"])) if ctx.query.get("uid") else None
    rows = []
    for mem, closed, total in await b.day_report_rows(date, await home_guild(), member):
        cs, _ = split_name(mem.display_name)
        rows.append({"uid": str(mem.id), "name": mem.display_name, "callsign": cs, "total": total,
                     "sessions": [{"clock_in": ci, "clock_out": co, "minutes": m} for ci, co, m in closed]})
    sent = await sent_info([date])
    return {"date": date, "today": b.today_str(), "rows": rows,
            "sheet": {"column": b.sheet_day_column(date), "sent": sent[0] if sent else None}}


def _minutes_args(src: dict) -> tuple[int, str, float]:
    return get_uid(src.get("uid")), get_date(src.get("date")), get_minutes(src.get("minutes"))


async def minutes_preview(ctx):
    uid, date, minutes = _minutes_args(ctx.query)
    before = week_totals(uid, date)
    added = b.session_minutes(date, *services.added_session_bounds(uid, date, minutes))
    return {"before": before, "after": {"day": before["day"] + added, "week": before["week"] + added}}


async def minutes_add(ctx):
    uid, date, minutes = _minutes_args(ctx.body)
    target = await get_member(uid)
    before = week_totals(uid, date)
    await services.add_minutes(ctx.actor, target, date, minutes, source="Web")
    return {"before": before, "after": week_totals(uid, date)}


async def sessions_list(ctx):
    uid = get_uid(ctx.query.get("uid"))
    if ctx.query.get("date"):
        date = get_date(ctx.query["date"])
        found = [(date, ci, co) for ci, co in b.get_clock_times(uid, date)]
    else:
        found = b.recent_sessions(uid)
    return {"rows": [{"date": d, "clock_in": ci, "clock_out": co, "minutes": b.session_minutes(d, ci, co)} for d, ci, co in found if ci]}


async def sessions_delete(ctx):
    uid, date, ci = get_uid(ctx.body.get("uid")), get_date(ctx.body.get("date")), get_clock(ctx.body.get("clock_in"))
    target = await get_member(uid)
    current = dict(b.get_clock_times(uid, date))
    if ci not in current:
        raise ApiError(404, "session_not_found", "Sesiunea nu mai există (modificată între timp).")
    await services.delete_session(ctx.actor, target, date, ci, current[ci], source="Web")
    return {"ok": True}


async def warns_get(ctx):
    return {"count": b.get_punish_count(get_uid(ctx.params[0])), "max": 3}


async def warns_add(ctx):
    reason = get_text(ctx.body.get("reason"), "reason", 400, required=True)
    target = await get_member(get_uid(ctx.params[0]))
    count = await services.add_warn(ctx.actor, await home_guild(), target, reason, source="Web")
    if count is None:
        raise ApiError(409, "warn_limit", "Are deja 3/3. Reset necesar.")
    return {"count": count}


async def warns_reset(ctx):
    note = get_text(ctx.body.get("note"), "note", 300, required=False)
    target = await get_member(get_uid(ctx.params[0]))
    await services.reset_warns(ctx.actor, await home_guild(), target, note, source="Web")
    return {"count": 0}


# Sheet sends run in the background (a week is ~40 Google requests); the app polls /jobs/{id}.
_jobs: dict[str, dict] = {}
_tasks: set[asyncio.Task] = set()
JOB_KEEP_SECONDS = 3600


async def _run_job(job: dict, actor, dates: list[str]):
    try:
        job["results"] = await services.push_days_to_sheet(actor, await home_guild(), dates, source="Web")
        job["status"] = "done"
    except Exception as e:
        logging.exception("Web sheet job failed")
        job.update(status="error", error=str(e))


async def sheet_days(ctx):
    if ctx.actor_id not in b.SHEET_EXPORT_USER_IDS:
        raise ApiError(403, "not_allowed", "Nu ai acces la Trimite în Sheet.")
    dates, today = ctx.body.get("dates"), b.today_str()
    if not isinstance(dates, list) or not 1 <= len(dates) <= 7:
        raise invalid("dates: între 1 și 7 zile")
    dates = sorted({get_date(d) for d in dates})
    if dates[-1] > today:
        raise invalid("Nu poți trimite zile din viitor.")
    if ctx.body.get("force") is not True and (sent := await sent_info(dates)):
        raise _already_sent(sent)
    now = time.time()
    for jid in [j for j, job in _jobs.items() if now - job["created"] > JOB_KEEP_SECONDS]:
        del _jobs[jid]
    job_id = str(uuid.uuid4())
    _jobs[job_id] = job = {"status": "running", "results": [], "created": now}
    task = asyncio.create_task(_run_job(job, ctx.actor, dates))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return 202, {"job_id": job_id}


def _already_sent(sent: list[dict]) -> ApiError:
    e = ApiError(409, "already_sent")
    e.body["sent"] = sent
    return e


async def job_status(ctx):
    job = _jobs.get(ctx.params[0])
    if not job:
        raise ApiError(404, "job_not_found")
    return {k: v for k, v in job.items() if k != "created"}


async def logs(ctx):
    kind = ctx.query.get("type") or "all"
    if kind not in ("all", "changes", "info", "failures"):
        raise invalid("type: all | changes | info | failures")
    cursor = ctx.query.get("cursor")
    if cursor and not cursor.isdigit():
        raise invalid("cursor invalid")
    limit = 50
    rows = db.query_logs(kind, (ctx.query.get("q") or "")[:100], int(cursor) if cursor else None, limit + 1)
    out = []
    for r in rows[:limit]:
        row = dict(zip(db.LOG_COLUMNS, r))
        row.update(
            id=str(row["id"]), changed=bool(row["changed"]), success=bool(row["success"]),
            actor_id=str(row["actor_id"]) if row["actor_id"] is not None else None,
            target_id=str(row["target_id"]) if row["target_id"] is not None else None,
        )
        out.append(row)
    return {"rows": out, "next_cursor": out[-1]["id"] if len(rows) > limit else None}


async def sync(ctx):
    sheets.invalidate()
    await asyncio.gather(sheets.read_members(), sheets.read_concedii(), sheets.read_pontaje_week())  # pull now so "at" is a real read
    sheets.last_pull.update(at=int(time.time()), by=ctx.actor.display_name)
    await b.log_action(ctx.actor, "web-sync", source="Web")
    return {"ok": True, **sheets.last_pull}


async def update_names(ctx):
    rows = await asyncio.to_thread(sheets.update_pontaje_names)
    sheets.invalidate("pontaje")
    await b.log_action(ctx.actor, "web-update-names", extra=f"rows={rows}", source="Web")
    return {"rows": rows}


async def sync_status(ctx):
    return sheets.last_pull


# (method, path regex, handler, required level: None = anyone, "login" = handled inside)
ROUTES = [
    ("GET", r"/ping", ping, None),
    ("POST", r"/login", login, "login"),
    ("GET", r"/me", me, HR),
    ("GET", r"/dashboard", dashboard, HR),
    ("GET", r"/members", members_page, HR),
    ("GET", r"/pontaje", pontaje, HR),
    ("POST", r"/pontaje/names", update_names, HR),
    ("GET", r"/concedii", concedii, HR),
    ("GET", r"/sessions/open", sessions_open, HR),
    ("POST", r"/sessions/stop", sessions_stop, HR),
    ("GET", r"/day-report", day_report, HR),
    ("GET", r"/minutes/preview", minutes_preview, HR),
    ("POST", r"/minutes", minutes_add, HR),
    ("GET", r"/sessions", sessions_list, HR),
    ("POST", r"/sessions/delete", sessions_delete, HR),
    ("GET", r"/warns/(\d+)", warns_get, HR),
    ("POST", r"/warns/(\d+)/add", warns_add, HR),
    ("POST", r"/warns/(\d+)/reset", warns_reset, CONDUCERE),
    ("POST", r"/sheet/days", sheet_days, HR),
    ("GET", r"/jobs/([\w-]+)", job_status, HR),
    ("GET", r"/logs", logs, CONDUCERE),
    ("GET", r"/sync", sync_status, HR),
    ("POST", r"/sync", sync, HR),
]
WRITES_ALLOWED_READ_ONLY = {"/login", "/sync"}


async def dispatch(req: dict) -> tuple[int, object]:
    actor_id, method, path, body = req.get("actor_id"), req.get("method"), req.get("path"), req.get("body")
    if not isinstance(actor_id, str) or not actor_id.isdigit():
        raise ApiError(401, "unauthorized")
    if method not in ("GET", "POST") or not isinstance(path, str) or not path.startswith("/") or not (body is None or isinstance(body, dict)):
        raise invalid("method, path sau body invalid")
    url = urlsplit(path)
    for r_method, pattern, handler, need in ROUTES:
        m = re.fullmatch(pattern, url.path)
        if m and r_method == method:
            break
    else:
        raise ApiError(404, "not_found")
    if READ_ONLY and method == "POST" and url.path not in WRITES_ALLOWED_READ_ONLY:
        raise ApiError(403, "read_only", "Serverul local de dezvoltare nu face modificări.")
    ctx = Ctx(int(actor_id), dict(parse_qsl(url.query)), body or {}, m.groups())
    if need in (HR, CONDUCERE):
        ctx.level, ctx.actor = await access(ctx.actor_id)
        if ctx.level is None:
            raise ApiError(403, "forbidden")
        if need == CONDUCERE and ctx.level != CONDUCERE:
            raise ApiError(403, "not_allowed", "Doar Conducere.")
    result = await handler(ctx)
    return result if isinstance(result, tuple) else (200, result)


_hits: dict[str, deque] = {}


def rate_ok(ip: str, now: float | None = None) -> bool:
    now = time.monotonic() if now is None else now
    if len(_hits) > 10_000:
        for k in [k for k, q in _hits.items() if not q or now - q[-1] > 60]:
            del _hits[k]
    q = _hits.setdefault(ip, deque())
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE_LIMIT_PER_MINUTE:
        return False
    q.append(now)
    return True


async def rpc(request: web.Request) -> web.Response:
    if not rate_ok(request.remote or "?"):
        return web.Response(status=429)
    try:
        key, req = decode_request(KEYS, GUARD, await request.read())
    except (Rejected, web.HTTPException):  # bad envelope, or a body over client_max_size
        return web.Response(status=400)
    try:
        status, body = await dispatch(req)
    except ApiError as e:
        status, body = e.status, e.body
    except Exception:
        logging.exception("Web API %s %s failed", req.get("method"), req.get("path"))
        status, body = 500, {"error": "internal"}
    return web.Response(text=seal(key, {"req_id": req["req_id"], "status": status, "body": body}), content_type="text/plain")


def add_routes(app: web.Application) -> bool:
    """Adds POST /api/web/rpc when WEB_API_KEY is set."""
    if not KEYS:
        return False
    app.router.add_post("/api/web/rpc", rpc)
    return True
