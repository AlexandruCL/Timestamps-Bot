import os
import logging
import datetime
import calendar
import asyncio
import re
import pathlib
import socket
import sqlite3
from functools import partial
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web as aiohttp_web
import discord
import gspread
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from database import (
    init_db, add_clock_in, update_clock_out, get_clock_times,
    get_ongoing_sessions, remove_session,
    increment_punish_count, get_punish_count, reset_punish_count,
    checkpoint_and_vacuum, db_stats, get_user_ids_for_dates,
    get_sheet_export, mark_sheet_export,
)

# --------------- Environment ---------------
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

def need(key: str) -> str:
    v = os.getenv(key)
    if not v or not v.strip():
        raise SystemExit(f"Missing env var: {key}")
    return v.strip()

def id_list(key: str) -> list[int]:
    return [int(x.strip()) for x in os.getenv(key, "").split(",") if x.strip().isdigit()]

ALLOWED_CHANNEL_ID        = int(need("ALLOWED_CHANNEL_ID"))  # both panels live in this channel
ALLOWED_PUNISH_CHANNEL_ID = int(need("ALLOWED_PUNISH_CHANNEL_ID"))
LOGS_CHANNEL_ID           = int(need("LOGS_CHANNEL_ID"))
REQUIRED_PD_ROLE_ID       = [int(r.strip()) for r in os.getenv("REQUIRED_PD_ROLE_NAME", "").split(",") if r.strip()]
REQUIRED_HR_ROLE_ID       = int(need("REQUIRED_HR_ROLE_NAME"))
CONDUCERE_ROLE_ID         = int(need("CONDUCERE_ROLE_ID"))

# Actions whose log embed is also copied to these channels
EXTRA_LOG_CHANNELS = {
    "adaugaminute-button": id_list("ADDMINUTES_LOG_CHANNEL_ID"),
    "ongoing-stop-button": id_list("ONGOING_STOP_CHANNEL_ID"),
    "stergepontaj":        id_list("DELETE_PONTAJ_CHANNEL_ID"),
}

PONTAJE_API_PORT  = int(os.getenv("PONTAJE_API_PORT", "3080"))
PONTAJE_API_TOKEN = os.getenv("PONTAJE_API_TOKEN", "")

PD_CALLSIGN_RE = re.compile(r"\[(\d{1,3})\]")

GOOGLE_SHEETS_CREDENTIALS_FILE = os.getenv("GOOGLE_SHEETS_CREDENTIALS_FILE", "credentials.json").strip()
PONTAJE_SHEET_ID = os.getenv("PONTAJE_SHEET_ID", "").strip()
PONTAJE_SHEET_TAB = "Monitorizare Activitate"
PD_SPREADSHEET_ID = os.getenv("PD_SPREADSHEET_ID", "").strip()
PD_MEMBERS_TAB = "Evidenta membrii"
PD_LEAVE_TAB = "Concedii"
SHEET_EXPORT_USER_IDS = id_list("SHEET_EXPORT_USER_IDS")  # Discord user ids allowed to use "Trimite în Sheet"

DEV_GUILD_ID_ENV = os.getenv("DEV_GUILD_ID")
DEV_GUILD_ID = int(DEV_GUILD_ID_ENV) if DEV_GUILD_ID_ENV and DEV_GUILD_ID_ENV.isdigit() else None

EOD_CONFIRM_WINDOW_SECS = int(os.getenv("EOD_CONFIRM_WINDOW_SECS", "300"))
WEEKLY_TARGET_MINUTES = int(os.getenv("WEEKLY_TARGET_MINUTES", "700"))  # week = Sunday to Saturday
DAILY_TARGET_MINUTES = int(os.getenv("DAILY_TARGET_MINUTES", "100"))    # shown for guidance; the weekly target is what counts

OWNER_ID = 286492096242909185
EOD_FALLBACK_CHANNEL_ID = 1410377156156198963  # EOD prompt is posted here when the user's DMs are closed
EOD_UNCONFIRMED_LOG_CHANNEL_ID = 1410237389321928745
EOD_CONFIRMED_LOG_CHANNEL_ID = 1410382233855856680

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

class ShortReconnectLog(logging.Filter):
    """discord.py logs a full traceback for every gateway reconnect after a host network drop; it recovers, so keep one line."""
    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info and record.getMessage().startswith("Attempting a reconnect"):
            err = record.exc_info[1]
            record.msg, record.args = f"{record.getMessage()} ({type(err).__name__}: {str(err) or 'no reply from Discord'})", ()
            record.exc_info = record.exc_text = None
            record.levelno, record.levelname = logging.WARNING, "WARNING"
        return True

logging.getLogger("discord.client").addFilter(ShortReconnectLog())

# --------------- Time ---------------
TIMEZONE = os.getenv("TIMEZONE", "Europe/Bucharest")

def local_now() -> datetime.datetime:
    return datetime.datetime.now(ZoneInfo(TIMEZONE))

def today_str() -> str:
    return local_now().strftime("%Y-%m-%d")

def parse_local(date_str: str, time_str: str) -> datetime.datetime:
    return datetime.datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo(TIMEZONE))

def valid_date(date_str: str) -> bool:
    try:
        datetime.datetime.strptime(date_str, "%Y-%m-%d")
        return True
    except ValueError:
        return False

def round_minutes(m: float) -> int:
    return round(m / 5) * 5

def session_minutes(date_str: str, clock_in: str, clock_out: str | None) -> int:
    """Rounded minutes of one session. An open session counts up to now, but only on its own day."""
    if clock_out:
        end = parse_local(date_str, clock_out)
    else:
        end = local_now()
        if end.strftime("%Y-%m-%d") != date_str:
            return 0
    return max(0, round_minutes((end - parse_local(date_str, clock_in)).total_seconds() / 60))

def day_minutes(uid: int, date_str: str) -> int:
    return sum(session_minutes(date_str, ci, co) for ci, co in get_clock_times(uid, date_str) if ci)

def week_dates(day: datetime.date) -> list[str]:
    """The Sunday-to-Saturday week containing day."""
    sunday = day - datetime.timedelta(days=(day.weekday() + 1) % 7)
    return [(sunday + datetime.timedelta(days=i)).isoformat() for i in range(7)]

def shift_date(date_str: str, days: int) -> str:
    return (datetime.date.fromisoformat(date_str) + datetime.timedelta(days=days)).isoformat()

def ddmm(date_str: str) -> str:
    return f"{date_str[8:10]}.{date_str[5:7]}"

def clock_lock_message(now: datetime.datetime) -> str | None:
    """Clock IN/OUT are locked while the end-of-day prompts run: 23:55-00:00 and 05:25-05:30."""
    if now.hour == 23 and now.minute >= 55:
        return "Butoanele sunt blocate între 23:55 și 00:00. Confirmă pontajul din mesajul primit și pornește din nou după 00:00."
    if now.hour == 5 and 25 <= now.minute < 30:
        return "Butoanele sunt blocate între 05:25 și 05:30. Confirmă pontajul din mesajul primit și pornește din nou după 05:30."
    return None

# --------------- DB ---------------
init_db()

# --------------- Helpers ---------------
def make_embed(
    title: str | None = None,
    desc: str | None = None,
    color: discord.Color = discord.Color.blurple(),
    user: discord.abc.User | None = None,
) -> discord.Embed:
    now_local = local_now()
    e = discord.Embed(title=title, description=desc, color=color, timestamp=now_local)
    if user:
        try:
            e.set_footer(text=f"{user.display_name} • {now_local.strftime('%H:%M:%S')}", icon_url=user.display_avatar.url)
        except Exception:
            e.set_footer(text=f"{user.display_name} • {now_local.strftime('%H:%M:%S')}")
    return e

async def reply(interaction: discord.Interaction, title: str, desc: str, color: discord.Color = discord.Color.red(), **kwargs):
    """Ephemeral embed answer; switches to a followup once the interaction was deferred."""
    send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
    await send(embed=make_embed(title, desc, color, interaction.user), ephemeral=True, **kwargs)

async def dm(uid: int, text: str, **kwargs) -> discord.Message | None:
    """DM a user by id; None when unreachable (DMs closed, deleted account, network)."""
    try:
        user = bot.get_user(uid) or await bot.fetch_user(uid)
        return await user.send(text, **kwargs)
    except (discord.HTTPException, OSError, aiohttp.ClientError):
        return None

def has_role(member: discord.Member, role_id: int) -> bool:
    return any(r.id == role_id for r in member.roles)

def has_any(member: discord.Member, ids: list[int]) -> bool:
    return any(r.id in ids for r in member.roles)

def is_mgmt(member: discord.Member) -> bool:
    return has_role(member, REQUIRED_HR_ROLE_ID) or has_role(member, CONDUCERE_ROLE_ID)

# --------------- Logging ---------------
LOG_FILE_PATH = pathlib.Path("logs.txt")

def _append_log_line(text: str):
    try:
        with LOG_FILE_PATH.open("a", encoding="utf-8") as f:
            f.write(f"[{datetime.datetime.utcnow().isoformat()}Z] {text}\n")
    except Exception:
        pass

async def log_command(
    interaction: discord.Interaction,
    action: str,
    *,
    target: discord.abc.User | None = None,
    extra: str | None = None,
    changed: bool = False,
    success: bool = True,
):
    """Write the action to logs.txt and the logs channel (plus EXTRA_LOG_CHANNELS). Never raises."""
    try:
        actor = interaction.user
        status = "SUCCESS" if success else "FAIL"
        kind = "MODIFY" if changed else "INFO"
        tgt_txt = f" | target={target}({target.id})" if target else ""
        extra_line = f" | {extra}" if extra else ""
        _append_log_line(f"[{status}] [{kind}] {action} by {actor}({actor.id}){tgt_txt}{extra_line}")

        color = (
            discord.Color.green() if success and changed else
            discord.Color.blurple() if success else
            discord.Color.red()
        )
        desc_parts = [f"Actiune: `{action}`", f"Executor: {actor.mention} (`{actor.id}`)"]
        if target:
            desc_parts.append(f"Target: {target.mention} (`{target.id}`)")
        desc_parts.append(f"Tip: {'Modificare' if changed else 'Informare'}")
        desc_parts.append(f"Status: {'Succes' if success else 'Eșec'}")
        if extra:
            desc_parts.append(f"Detalii: {extra[:500]}")
        embed = make_embed("Log Comandă", "\n".join(desc_parts), color, actor)

        for cid in dict.fromkeys([LOGS_CHANNEL_ID, *EXTRA_LOG_CHANNELS.get(action, [])]):
            ch = bot.get_channel(cid)
            if ch:
                try:
                    await ch.send(embed=embed)
                except Exception:
                    pass
    except Exception:
        logging.exception("log_command failed for %s", action)

# --------------- Members & reports ---------------
async def fetch_members(guild: discord.Guild, user_ids) -> list[discord.Member]:
    """
    Resolve ids to Members without the privileged members intent: cache first, then one gateway
    query per 100 ids (REST per id if that fails). Users who left the server are skipped.
    """
    found: dict[int, discord.Member] = {}
    missing = []
    for uid in set(user_ids):
        m = guild.get_member(uid)
        if m:
            found[uid] = m
        else:
            missing.append(uid)
    for i in range(0, len(missing), 100):
        chunk = missing[i:i + 100]
        try:
            for m in await guild.query_members(user_ids=chunk, limit=100):
                found[m.id] = m
        except Exception:
            logging.exception("query_members failed, falling back to REST")
            for uid in chunk:
                try:
                    found[uid] = await guild.fetch_member(uid)
                except discord.HTTPException:
                    pass
    return [m for m in found.values() if not m.bot]

async def resolve_display_names(guild: discord.Guild, uids) -> dict[int, str]:
    return {m.id: m.display_name for m in await fetch_members(guild, uids)}

def _callsign_sort_key(member: discord.Member) -> tuple[int, str]:
    """Sort by PD callsign "[123]" (or a bare "001 Name"); members without one go last."""
    name = member.display_name or member.name or ""
    m = PD_CALLSIGN_RE.search(name) or re.search(r"\b(\d{1,3})\b", name)
    return (int(m.group(1)) if m else 10**6, name.lower())

async def build_day_report(date_str: str, guild: discord.Guild, member: discord.Member | None = None) -> tuple[str, list[str]]:
    if member:
        members = [member]
    else:
        members = await fetch_members(guild, get_user_ids_for_dates([date_str]))
        members = sorted((m for m in members if has_any(m, REQUIRED_PD_ROLE_ID)), key=_callsign_sort_key)

    lines = []
    for mem in members:
        closed = [(ci, co, session_minutes(date_str, ci, co)) for ci, co in get_clock_times(mem.id, date_str) if ci and co]
        total = sum(mins for _, _, mins in closed)
        if total <= 0:
            continue
        if member:
            details = [f"{idx}. {ci} - {co} ({mins}min)" for idx, (ci, co, mins) in enumerate(closed, start=1) if mins > 0]
            lines.append(f"{mem.display_name} Total: ({total})\n" + "\n".join(details))
        else:
            lines.append(f"{mem.display_name}: ({total} min)")
    title = f"Raport {date_str}" + (f" - {member.display_name}" if member else " (toți)")
    return title, lines or ["Fără date."]

def sheet_day_column(date_str: str) -> str:
    """LUNI..DUMINICA live in columns F..L of the Monitorizare Activitate tab."""
    return "FGHIJKL"[datetime.date.fromisoformat(date_str).weekday()]

assert [sheet_day_column(d) for d in ("2026-11-16", "2026-11-22")] == ["F", "L"]  # Monday, Sunday

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

def concedii_on(gc: gspread.Client, dates: list[str]) -> dict[str, set[str]]:
    """Callsign -> the given dates covered by one of its concedii (Concedii tab: D callsign, E start, F end, inclusive)."""
    year = datetime.date.fromisoformat(dates[0]).year
    out: dict[str, set[str]] = {}
    for row in gc.open_by_key(PD_SPREADSHEET_ID).worksheet(PD_LEAVE_TAB).get("D:F"):
        cs = row[0].strip() if row else ""
        if len(row) < 3 or not cs.isdigit():
            continue
        start, end = parse_sheet_date(row[1], year), parse_sheet_date(row[2], year)
        if not start or not end:
            continue
        if end < start and not re.search(r"\d{4}", row[2]):  # e.g. 28.12 -> 03.01
            end = end.replace(year=end.year + 1)
        covered = {d for d in dates if start.isoformat() <= d <= end.isoformat()}
        if covered:
            out.setdefault(cs.zfill(3), set()).update(covered)
    return out

assert parse_sheet_date("16.11", 2026) == datetime.date(2026, 11, 16)
assert parse_sheet_date("21/11/2026", 2000) == datetime.date(2026, 11, 21)
assert parse_sheet_date("CS indisponibil!", 2026) is None

def sync_concedii_to_sheet() -> int:
    """Write CO for this Sunday-Saturday week's concedii into the pontaje sheet. Returns how many cells changed."""
    today = local_now().date()
    if today.weekday() == 6:
        # Sunday: the sheet still holds last week until it is cleared and sent; the new week's CO is written from Monday.
        return 0
    gc = sheets_client()
    leave = concedii_on(gc, week_dates(today))
    if not leave:
        return 0
    ws = gc.open_by_key(PONTAJE_SHEET_ID).worksheet(PONTAJE_SHEET_TAB)
    callsigns, days = ws.batch_get(["B:B", "F:L"])
    updates = []
    for i, row in enumerate(callsigns):
        cs = row[0].strip() if row else ""
        current = days[i] if i < len(days) else []
        for d in leave.get(cs, ()):
            idx = "FGHIJKL".index(sheet_day_column(d))
            if (current[idx].strip().upper() if idx < len(current) else "") != "CO":
                updates.append({"range": f"{sheet_day_column(d)}{i + 1}", "values": [["CO"]]})
    if updates:
        ws.batch_update(updates)
    return len(updates)

async def day_minutes_by_callsign(date_str: str, guild: discord.Guild) -> tuple[dict[str, int], list[str]]:
    """Closed-session minutes per 3-digit callsign for PD members, plus names that have no [callsign]."""
    minutes, no_callsign = {}, []
    for mem in await fetch_members(guild, get_user_ids_for_dates([date_str])):
        if not has_any(mem, REQUIRED_PD_ROLE_ID):
            continue
        total = sum(session_minutes(date_str, ci, co) for ci, co in get_clock_times(mem.id, date_str) if ci and co)
        if total <= 0:
            continue
        m = PD_CALLSIGN_RE.search(mem.display_name)
        if m:
            cs = m.group(1).zfill(3)
            minutes[cs] = minutes.get(cs, 0) + total
        else:
            no_callsign.append(mem.display_name)
    return minutes, no_callsign

def push_day_to_sheet(date_str: str, minutes_by_callsign: dict[str, int]) -> tuple[list[str], list[str]]:
    """
    Fill the weekday column for every sheet row with a callsign (B) and a name (C): their minutes, or 0.
    The sheet is the roster, so no Discord member list is needed. Members on concediu get "CO"; "CO" cells are left alone.
    Returns (callsigns that had minutes but no named row in the sheet, callsigns that got "/").
    """
    gc = sheets_client()
    ws = gc.open_by_key(PONTAJE_SHEET_ID).worksheet(PONTAJE_SHEET_TAB)
    col = sheet_day_column(date_str)
    callsigns, names, day = ws.batch_get(["B:B", "C:C", f"{col}:{col}"])
    cell = lambda vals, i: vals[i][0].strip() if i < len(vals) and vals[i] else ""

    # Members hired on this day (DATA ANGAJARE, column J) get "/" instead of a pontaj under the daily target.
    pd_cs, pd_hired = gc.open_by_key(PD_SPREADSHEET_ID).worksheet(PD_MEMBERS_TAB).batch_get(["B:B", "J:J"])
    hired_day = datetime.date.fromisoformat(date_str).strftime("%d/%m/%Y")
    hired = {cell(pd_cs, i) for i in range(len(pd_cs)) if cell(pd_hired, i) == hired_day}
    on_leave = concedii_on(gc, [date_str])

    updates, found, slashed = [], set(), []
    for i in range(len(callsigns)):
        cs = cell(callsigns, i)
        if not cs.isdigit() or not cell(names, i):
            continue
        found.add(cs)
        if cell(day, i).upper() != "CO":
            mins = minutes_by_callsign.get(cs, 0)
            value = "CO" if cs in on_leave else "/" if cs in hired and mins < DAILY_TARGET_MINUTES else mins
            updates.append({"range": f"{col}{i + 1}", "values": [[value]]})
            if value == "/":
                slashed.append(cs)
    if updates:
        ws.batch_update(updates)
    return [cs for cs in minutes_by_callsign if cs not in found], slashed

async def _post_warn(interaction: discord.Interaction, target: discord.abc.User, title: str, body: str, color: discord.Color, *, ping_mgmt: bool = False):
    """Public warn message in the punish channel; ping_mgmt tags Conducere + HR (used at 3/3)."""
    guild = interaction.guild
    ch = guild.get_channel(ALLOWED_PUNISH_CHANNEL_ID) if guild else None
    if not isinstance(ch, discord.TextChannel):
        return
    content = None
    if ping_mgmt:
        roles = [r.mention for r in (guild.get_role(CONDUCERE_ROLE_ID), guild.get_role(REQUIRED_HR_ROLE_ID)) if r]
        content = "||" + " ".join(roles) + "||" if roles else None
    try:
        await ch.send(content=content, embed=make_embed(title, f"{target.mention}\n{body}", color, interaction.user))
    except Exception:
        logging.exception("Warn post failed")

# --------------- Views: shared ---------------
def _button(label: str, style: discord.ButtonStyle, row: int, callback) -> discord.ui.Button:
    b = discord.ui.Button(label=label, style=style, row=row)
    b.callback = callback
    return b

class BaseView(discord.ui.View):
    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        # A click we could not answer within Discord's 3s window (host network/DNS blip) is gone; the user retries.
        lost = isinstance(error, (aiohttp.ClientError, asyncio.TimeoutError)) or (isinstance(error, discord.NotFound) and error.code == 10062)
        if lost:
            logging.warning("Click lost on %s by %s: %s", getattr(item, "custom_id", item), interaction.user, error)
        else:
            await super().on_error(interaction, error, item)

class OwnedView(BaseView):
    """Ephemeral follow-up panel that only the member who opened it may use."""
    def __init__(self, requester_id: int, timeout: float = 180):
        super().__init__(timeout=timeout)
        self.requester_id = requester_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Nu este sesiunea ta.", ephemeral=True)
            return False
        return True

def user_picker(requester_id: int, on_pick) -> OwnedView:
    """View with one user select; calls on_pick(interaction, member)."""
    view = OwnedView(requester_id, timeout=120)
    select = discord.ui.UserSelect(placeholder="Selectează user")

    async def picked(interaction: discord.Interaction):
        await on_pick(interaction, select.values[0])

    select.callback = picked
    view.add_item(select)
    return view

def add_day_arrows(view: discord.ui.View, date_str: str, on_shift):
    """Row-0 previous/next day buttons calling on_shift(step, interaction); no stepping past today."""
    view.add_item(_button("◀ Ziua anterioară", discord.ButtonStyle.secondary, 0, partial(on_shift, -1)))
    nxt = _button("Ziua următoare ▶", discord.ButtonStyle.secondary, 0, partial(on_shift, 1))
    nxt.disabled = date_str >= today_str()
    view.add_item(nxt)

# --------------- Day report UI ---------------
class DayCalendarView(OwnedView):
    """Month day picker for older days: navigation on row 0, up to 20 days (4 rows x 5) per page."""
    PAGE_SIZE = 20

    def __init__(self, requester_id: int, year: int, month: int, on_pick):
        super().__init__(requester_id)
        self.year, self.month, self.page, self.on_pick = year, month, 0, on_pick
        self.rebuild()

    def days(self) -> int:
        return calendar.monthrange(self.year, self.month)[1]

    def total_pages(self) -> int:
        return -(-self.days() // self.PAGE_SIZE)

    def embed(self, user: discord.abc.User) -> discord.Embed:
        header = f"{calendar.month_name[self.month]} {self.year} (Pagina {self.page + 1}/{self.total_pages()})"
        return make_embed("Raport Zi - Alege data", header, discord.Color.blurple(), user)

    def rebuild(self):
        self.clear_items()
        nav = discord.ButtonStyle.primary
        self.add_item(_button("◀ Luna", nav, 0, partial(self._move, months=-1)))
        self.add_item(_button("Luna ▶", nav, 0, partial(self._move, months=1)))
        if self.total_pages() > 1:
            self.add_item(_button("◀ Pg", discord.ButtonStyle.secondary, 0, partial(self._move, pages=-1)))
            self.add_item(_button("Pg ▶", discord.ButtonStyle.secondary, 0, partial(self._move, pages=1)))
        start = self.page * self.PAGE_SIZE + 1
        for day in range(start, min(self.days(), start + self.PAGE_SIZE - 1) + 1):
            self.add_item(_button(str(day), discord.ButtonStyle.secondary, 1 + (day - start) // 5, partial(self._pick_day, day)))

    async def _move(self, interaction: discord.Interaction, *, months: int = 0, pages: int = 0):
        if months:
            year, month0 = divmod(self.year * 12 + self.month - 1 + months, 12)
            self.year, self.month, self.page = year, month0 + 1, 0
        else:
            self.page = (self.page + pages) % self.total_pages()
        self.rebuild()
        await interaction.response.edit_message(embed=self.embed(interaction.user), view=self)

    async def _pick_day(self, day: int, interaction: discord.Interaction):
        await self.on_pick(interaction, f"{self.year:04d}-{self.month:02d}-{day:02d}")

class DayReportView(OwnedView):
    """Day report (everyone, or one member) with day arrows. Every step renders a fresh view."""
    def __init__(self, requester_id: int, date_str: str, member: discord.Member | None = None):
        super().__init__(requester_id, timeout=300)
        self.date_str, self.member = date_str, member
        add_day_arrows(self, date_str, self._shift)
        if member:
            self.add_item(_button("Toți", discord.ButtonStyle.success, 1, self._everyone))
        elif requester_id in SHEET_EXPORT_USER_IDS:  # OwnedView: only the requester can click it
            self.add_item(_button("Trimite în Sheet", discord.ButtonStyle.success, 1, self._to_sheet))
            self.add_item(_button("Trimite Săptămâna", discord.ButtonStyle.success, 1, self._week_to_sheet))
        self.add_item(_button("Alege User", discord.ButtonStyle.primary, 1, self._pick_user))
        self.add_item(_button("Altă dată", discord.ButtonStyle.secondary, 1, self._other_date))

    async def render(self, interaction: discord.Interaction):
        """Fill the message of an already deferred interaction with this report."""
        await interaction.edit_original_response(
            embed=make_embed("Raport Zi", f"⏳ Se generează raportul pentru {self.date_str}...", discord.Color.orange(), interaction.user),
            view=None,
        )
        title, lines = await build_day_report(self.date_str, interaction.guild, self.member)
        await interaction.edit_original_response(
            embed=make_embed(title, "\n".join(lines)[:3900], discord.Color.green(), interaction.user),
            view=self,
        )
        await log_command(interaction, "day-report-user" if self.member else "day-report-all", target=self.member, extra=f"date={self.date_str}")

    async def _open(self, interaction: discord.Interaction, date_str: str, member: discord.Member | None):
        await interaction.response.defer()
        await DayReportView(self.requester_id, date_str, member).render(interaction)

    async def _shift(self, step: int, interaction: discord.Interaction):
        await self._open(interaction, shift_date(self.date_str, step), self.member)

    async def _to_sheet(self, interaction: discord.Interaction):
        await self._ask_then_push(interaction, [self.date_str])

    async def _week_to_sheet(self, interaction: discord.Interaction):
        """The Sunday-Saturday week of the viewed day; today and later are left out since they are not finished."""
        today = today_str()
        await self._ask_then_push(interaction, [d for d in week_dates(datetime.date.fromisoformat(self.date_str)) if d < today])

    async def _ask_then_push(self, interaction: discord.Interaction, dates: list[str]):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not dates:
            return await reply(interaction, "Trimite în Sheet", "Nu există zile încheiate de trimis în această săptămână.")
        sent = [(d, s) for d in dates if (s := get_sheet_export(d))]
        if sent:
            view = OwnedView(self.requester_id)
            view.add_item(_button("Trimite din nou", discord.ButtonStyle.danger, 0, partial(self._resend_to_sheet, dates)))
            already = "\n".join(f"• {d}: trimisă de <@{uid}> pe <t:{ts}:f>" for d, (uid, ts) in sent)
            return await reply(
                interaction, "Deja în Sheet",
                f"Deja în sheet:\n{already}\n"
                "Apasă **Trimite din nou** doar dacă vrei să rescrii aceste zile (de ex. după o corectură).",
                discord.Color.orange(), view=view,
            )
        await self._push_to_sheet(interaction, dates)

    async def _resend_to_sheet(self, dates: list[str], interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._push_to_sheet(interaction, dates)

    async def _push_to_sheet(self, interaction: discord.Interaction, dates: list[str]):
        parts, ok = [], True
        for date_str in dates:
            minutes, no_callsign = await day_minutes_by_callsign(date_str, interaction.guild)
            try:
                missing, slashed = await asyncio.to_thread(push_day_to_sheet, date_str, minutes)
            except Exception as e:
                logging.exception("Sheet push failed for %s", date_str)
                parts.append(f"**{date_str}**: eroare la scrierea în sheet: {e}")
                ok = False
                continue
            mark_sheet_export(date_str, interaction.user.id, int(local_now().timestamp()))
            written = len(minutes) - len(missing)
            desc = f"**{date_str}** (coloana {sheet_day_column(date_str)}): {written} pontaje scrise, restul membrilor din sheet au primit 0."
            if slashed:
                desc += f"\nAngajați în această zi, sub {DAILY_TARGET_MINUTES} min (\"/\"): " + ", ".join(slashed)
            if missing:
                desc += "\nCallsign negăsit în sheet: " + ", ".join(missing)
            if no_callsign:
                desc += "\nFără callsign în nume: " + ", ".join(no_callsign)
            parts.append(desc)
            await log_command(interaction, "day-report-sheet", extra=f"date={date_str} rows={written}")
        await reply(interaction, "Trimite în Sheet", "\n\n".join(parts)[:3900], discord.Color.green() if ok else discord.Color.orange())

    async def _everyone(self, interaction: discord.Interaction):
        await self._open(interaction, self.date_str, None)

    async def _pick_user(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=make_embed("Raport Zi - Alege user", f"Data: {self.date_str}\nSelectează un user.", discord.Color.blurple(), interaction.user),
            view=user_picker(self.requester_id, lambda i, member: self._open(i, self.date_str, member)),
        )

    async def _other_date(self, interaction: discord.Interaction):
        d = datetime.date.fromisoformat(self.date_str)
        cal = DayCalendarView(self.requester_id, d.year, d.month, lambda i, date_str: self._open(i, date_str, self.member))
        await interaction.response.edit_message(embed=cal.embed(interaction.user), view=cal)

# --------------- My sessions ---------------
def my_day_embed(user: discord.abc.User, date_str: str) -> discord.Embed:
    lines, total = [], 0
    for idx, (ci, co) in enumerate(get_clock_times(user.id, date_str), start=1):
        if not ci:
            continue
        mins = session_minutes(date_str, ci, co)
        total += mins
        lines.append(f"{idx}. {ci} - {co} ({mins} min)" if co else f"{idx}. {ci} - ... ({mins} min, activă)")
    week = week_dates(datetime.date.fromisoformat(date_str))
    week_total = sum(day_minutes(user.id, d) for d in week)
    desc = (
        ("\n".join(lines)[:3800] or "Fără sesiuni.")
        + f"\n\n{'Azi' if date_str == today_str() else 'Total zi'}: {total} / {DAILY_TARGET_MINUTES} min"
        + f"\nSăptămâna {ddmm(week[0])} - {ddmm(week[-1])}: {week_total} / {WEEKLY_TARGET_MINUTES} min"
    )
    color = discord.Color.green() if lines else discord.Color.orange()
    return make_embed(f"Pontajele mele - {date_str}", desc, color, user)

class MyPontajeView(OwnedView):
    """A member's own sessions for one day, with day arrows."""
    def __init__(self, requester_id: int, date_str: str):
        super().__init__(requester_id)
        self.date_str = date_str
        add_day_arrows(self, date_str, self._shift)

    async def _shift(self, step: int, interaction: discord.Interaction):
        date_str = shift_date(self.date_str, step)
        await interaction.response.edit_message(
            embed=my_day_embed(interaction.user, date_str),
            view=MyPontajeView(self.requester_id, date_str),
        )

# --------------- Persistent panels ---------------
class PanelView(BaseView):
    """Persistent panel in ALLOWED_CHANNEL_ID; subclasses define who may press its buttons."""
    role_error = ""

    def __init__(self):
        super().__init__(timeout=None)

    def allowed(self, member: discord.Member) -> bool:
        raise NotImplementedError

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.channel or interaction.channel.id != ALLOWED_CHANNEL_ID:
            await reply(interaction, "Canal invalid", f"Folosește în <#{ALLOWED_CHANNEL_ID}>.")
            return False
        if not self.allowed(interaction.user):
            await reply(interaction, "Permisiune", self.role_error)
            return False
        return True

class ClockButtons(PanelView):
    role_error = "Ai nevoie de rol PD."

    def allowed(self, member):
        return has_any(member, REQUIRED_PD_ROLE_ID)

    @staticmethod
    def _open_session(user_id: int, date_str: str) -> str | None:
        return next((ci for ci, co in get_clock_times(user_id, date_str) if co is None), None)

    @discord.ui.button(label="Clock IN", style=discord.ButtonStyle.success, custom_id="clock_in_btn")
    async def clock_in_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)  # ACK first: Discord drops clicks not answered within 3s
        now = local_now()
        lock = clock_lock_message(now)
        if lock:
            return await reply(interaction, "Pontaj blocat", lock)

        uid = interaction.user.id
        date_str, time_str = now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S")
        open_ci = self._open_session(uid, date_str)
        if open_ci:
            since = int(parse_local(date_str, open_ci).timestamp())
            return await reply(interaction, "Activ", f"Deja pornit la {open_ci} (<t:{since}:R>). Apasă Clock OUT.", discord.Color.orange())
        try:
            add_clock_in(uid, date_str, time_str)
        except sqlite3.OperationalError as e:
            if "database or disk is full" not in str(e).lower():
                raise
            try:
                checkpoint_and_vacuum()
                add_clock_in(uid, date_str, time_str)
            except Exception:
                return await reply(interaction, "Stocare plină", "Nu se poate salva în DB. Rulează VACUUM sau eliberează spațiu.")
        # <t:...:R> is rendered by Discord as a live "acum X" counter
        await reply(interaction, "Clock IN", f"Pornit la {time_str} (<t:{int(now.timestamp())}:R>)", discord.Color.green())
        await log_command(interaction, "clockin-button", changed=True, extra=f"time={time_str}")

    @discord.ui.button(label="Clock OUT", style=discord.ButtonStyle.danger, custom_id="clock_out_btn")
    async def clock_out_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        now = local_now()
        lock = clock_lock_message(now)
        if lock:
            return await reply(interaction, "Pontaj blocat", lock)
        uid = interaction.user.id
        date_str, time_str = now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S")
        open_ci = self._open_session(uid, date_str)
        if not open_ci:
            return await reply(interaction, "Fără sesiune", "Nu ai sesiune activă.", discord.Color.orange())
        update_clock_out(uid, date_str, time_str, open_ci)
        mins = session_minutes(date_str, open_ci, time_str)
        await reply(
            interaction, "Clock OUT",
            f"Stop {time_str}\nDurată: {mins} minute\nAzi: {day_minutes(uid, date_str)} / {DAILY_TARGET_MINUTES} min",
            discord.Color.green(),
        )
        await log_command(interaction, "clockout-button", changed=True, extra=f"start={open_ci} end={time_str} mins={mins}")

    @discord.ui.button(label="Pontajele Mele", style=discord.ButtonStyle.grey, custom_id="my_pontaje_btn")
    async def my_pontaje_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        date_str = today_str()
        await interaction.response.send_message(
            embed=my_day_embed(interaction.user, date_str), view=MyPontajeView(interaction.user.id, date_str), ephemeral=True,
        )
        await log_command(interaction, "my-pontaje", extra=f"date={date_str}")

class HrButtons(PanelView):
    role_error = "Ai nevoie de rol HR sau Conducere."

    def allowed(self, member):
        return is_mgmt(member)

    @discord.ui.button(label="Pontaje Deschise", style=discord.ButtonStyle.grey, custom_id="clock_ongoing_btn")
    async def clock_ongoing_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        today = today_str()
        sessions = [(uid, ci) for uid, d, ci in get_ongoing_sessions() if d == today]
        if not sessions:
            return await reply(interaction, "Pontaje Deschise", "Nu există sesiuni active azi.", discord.Color.blue())
        names = await resolve_display_names(interaction.guild, [uid for uid, _ in sessions])
        view = OngoingStopView(interaction.user.id, today, sessions, names)
        await interaction.followup.send(embed=view.embed(interaction.user), view=view, ephemeral=True)
        await log_command(interaction, "ongoing-button", extra=f"count={len(sessions)} date={today}")

    @discord.ui.button(label="Warn", style=discord.ButtonStyle.grey, custom_id="warn_panel_btn")
    async def warn_panel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await reply(
            interaction, "Warn Panel", "Selectează userul pentru acțiuni (Add / Status / Reset).",
            discord.Color.blurple(), view=user_picker(interaction.user.id, _open_warn_actions),
        )
        await log_command(interaction, "warn-panel-open")

    @discord.ui.button(label="Pontaje / ZI", style=discord.ButtonStyle.grey, custom_id="day_report_btn")
    async def day_report_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await DayReportView(interaction.user.id, shift_date(today_str(), -1)).render(interaction)

    @discord.ui.button(label="Adaugă Minute", style=discord.ButtonStyle.grey, custom_id="add_minutes_btn")
    async def add_minutes_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await reply(
            interaction, "Adaugă Minute", "Selectează userul pentru care adaugi minute.",
            discord.Color.blurple(), view=user_picker(interaction.user.id, _open_add_minutes),
        )
        await log_command(interaction, "adaugaminute-panel-open")

    @discord.ui.button(label="Șterge Pontaj", style=discord.ButtonStyle.grey, custom_id="rmv_panel_btn")
    async def rmv_panel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await reply(
            interaction, "Șterge Pontaj", "Selectează userul pentru a vedea sesiunile din ultimele 7 zile.",
            discord.Color.blurple(), view=user_picker(interaction.user.id, _open_remove),
        )
        await log_command(interaction, "stergepontaj-panel-open")

# --------------- HR: stop open sessions ---------------
class OngoingStopView(OwnedView):
    """
    Today's open sessions: the list, plus 4 Stop buttons per page (rows 0-3, row 4 = page nav).
    Stop asks "Sigur?" first, then deletes the session (0 minutes).
    """
    PAGE_SIZE = 4

    def __init__(self, requester_id: int, day: str, sessions: list[tuple[int, str]], names: dict[int, str]):
        super().__init__(requester_id)
        self.day, self.sessions, self.names, self.page = day, sessions, names, 0
        self.pending: tuple[int, str] | None = None
        self.note = ""
        self.rebuild()

    def total_pages(self) -> int:
        return max(1, -(-len(self.sessions) // self.PAGE_SIZE))

    def embed(self, user: discord.abc.User) -> discord.Embed:
        if self.pending:
            uid, ci = self.pending
            return make_embed("Sigur?", f"Ștergi sesiunea lui <@{uid}> pornită la {ci}?\nMinutele NU se salvează.", discord.Color.red(), user)
        lines = "\n".join(
            f"{self.names.get(uid, uid)} - {ci} (<t:{int(parse_local(self.day, ci).timestamp())}:R>)" for uid, ci in self.sessions
        )
        desc = "\n\n".join(p for p in (self.note, lines[:3500] or "Nu mai există sesiuni active azi.") if p)
        return make_embed(f"Pontaje Deschise - Total: {len(self.sessions)}", desc, discord.Color.blurple(), user)

    def rebuild(self):
        self.clear_items()
        if self.pending:
            self.add_item(_button("Confirmă ștergerea", discord.ButtonStyle.danger, 0, self._confirm))
            self.add_item(_button("Anulează", discord.ButtonStyle.secondary, 0, self._cancel))
            return
        start = self.page * self.PAGE_SIZE
        for row, (uid, ci) in enumerate(self.sessions[start:start + self.PAGE_SIZE]):
            label = f"Stop: {self.names.get(uid, uid)} {ci}"
            self.add_item(_button(label, discord.ButtonStyle.danger, row, partial(self._ask, uid, ci)))
        if self.total_pages() > 1:
            self.add_item(_button("◀", discord.ButtonStyle.secondary, 4, partial(self._flip, -1)))
            self.add_item(_button("▶", discord.ButtonStyle.secondary, 4, partial(self._flip, 1)))

    async def _redraw(self, interaction: discord.Interaction):
        self.rebuild()
        await interaction.response.edit_message(embed=self.embed(interaction.user), view=self)

    async def _flip(self, step: int, interaction: discord.Interaction):
        self.page = (self.page + step) % self.total_pages()
        await self._redraw(interaction)

    async def _ask(self, uid: int, ci: str, interaction: discord.Interaction):
        self.pending, self.note = (uid, ci), ""
        await self._redraw(interaction)

    async def _cancel(self, interaction: discord.Interaction):
        self.pending = None
        await self._redraw(interaction)

    async def _confirm(self, interaction: discord.Interaction):
        if not is_mgmt(interaction.user):
            return await reply(interaction, "Permisiune", "Nu poți folosi acest panou.")
        if not self.pending:  # double click
            return await interaction.response.defer()
        uid, ci = self.pending
        self.pending = None
        remove_session(uid, self.day, ci)
        self.sessions = [s for s in self.sessions if s != (uid, ci)]
        self.page = min(self.page, self.total_pages() - 1)
        self.note = f"Sesiune oprită: <@{uid}> ({ci})."
        await self._redraw(interaction)
        await dm(uid, f"Sesiunea ta ({ci} - {self.day}) a fost oprită de {interaction.user.display_name}.")
        target = (await fetch_members(interaction.guild, [uid]) or [None])[0]
        await log_command(interaction, "ongoing-stop-button", target=target, changed=True, extra=f"uid={uid} start={ci}")

# --------------- HR: warns ---------------
async def _open_warn_actions(interaction: discord.Interaction, member: discord.Member):
    count = get_punish_count(member.id)
    await interaction.response.edit_message(
        embed=make_embed("Warn Panel", f"Țintă: {member.mention}\nWarn-uri: {count}/3\nAlege acțiunea.", discord.Color.orange(), interaction.user),
        view=WarnActionView(interaction.user.id, member),
    )
    await log_command(interaction, "warn-target-select", target=member, extra=f"count={count}")

class WarnActionView(OwnedView):
    def __init__(self, requester_id: int, target: discord.Member):
        super().__init__(requester_id, timeout=200)
        self.target = target

    @discord.ui.button(label="Adaugă Warn", style=discord.ButtonStyle.danger)
    async def add_warn_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(WarnReasonModal(self.target))

    @discord.ui.button(label="Reset", style=discord.ButtonStyle.success)
    async def reset_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not has_role(interaction.user, CONDUCERE_ROLE_ID):
            return await interaction.response.send_message("Doar Conducere poate reseta.", ephemeral=True)
        await interaction.response.send_modal(WarnResetModal(self.target))

class WarnReasonModal(discord.ui.Modal, title="Motiv Warn"):
    reason = discord.ui.TextInput(label="Motiv", style=discord.TextStyle.paragraph, max_length=400, required=True)

    def __init__(self, target: discord.Member):
        super().__init__(timeout=300)
        self.target = target

    async def on_submit(self, interaction: discord.Interaction):
        if not is_mgmt(interaction.user):
            return await interaction.response.send_message("Permisiune refuzată.", ephemeral=True)
        if get_punish_count(self.target.id) >= 3:
            return await reply(interaction, "Limită", f"{self.target.mention} are deja 3/3. Reset necesar.", discord.Color.orange())
        new_count = increment_punish_count(self.target.id)
        # Close the warn panel so a second click can't warn twice
        await interaction.response.edit_message(
            embed=make_embed("Warn trimis", f"{self.target.mention} -> {new_count}/3", discord.Color.green(), interaction.user), view=None,
        )
        await _post_warn(
            interaction, self.target, "Avertisment",
            f"Avertizat de {interaction.user.mention}\nWarn {new_count}/3\n\n{self.reason.value}",
            discord.Color.red() if new_count == 3 else discord.Color.orange(),
            ping_mgmt=new_count == 3,
        )
        await dm(self.target.id, f"Ai primit un warn ({new_count}/3) de la {interaction.user.display_name}: {self.reason.value}")
        await log_command(interaction, "warn-add-ui", target=self.target, changed=True, extra=f"new_count={new_count}")

class WarnResetModal(discord.ui.Modal, title="Reset Warn-uri"):
    note = discord.ui.TextInput(label="Notă (opțional)", style=discord.TextStyle.paragraph, required=False, max_length=300)

    def __init__(self, target: discord.Member):
        super().__init__(timeout=300)
        self.target = target

    async def on_submit(self, interaction: discord.Interaction):
        if not has_role(interaction.user, CONDUCERE_ROLE_ID):
            return await interaction.response.send_message("Doar Conducere poate reseta.", ephemeral=True)
        reset_punish_count(self.target.id)
        await interaction.response.edit_message(
            embed=make_embed("Reset", f"{self.target.mention} resetat la 0/3.", discord.Color.green(), interaction.user), view=None,
        )
        await _post_warn(
            interaction, self.target, "Reset Warn-uri",
            f"Resetat de {interaction.user.mention}.\n{self.note.value.strip()}", discord.Color.green(),
        )
        await dm(self.target.id, f"Warn-urile tale au fost resetate de {interaction.user.display_name}.")
        await log_command(interaction, "warn-reset-ui", target=self.target, changed=True)

# --------------- HR: add minutes ---------------
async def _open_add_minutes(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.send_modal(AddMinutesModal(member))
    await log_command(interaction, "addminutes-target-select", target=member)

class AddMinutesModal(discord.ui.Modal, title="Adaugă Minute"):
    def __init__(self, target: discord.Member):
        super().__init__(timeout=300)
        self.target = target
        today = today_str()
        self.date_input = discord.ui.TextInput(label="Data (YYYY-MM-DD)", placeholder=today, default=today, max_length=10, required=True)
        self.minutes_input = discord.ui.TextInput(label="Minute de adăugat", placeholder="Ex: 45", max_length=6, required=True)
        self.add_item(self.date_input)
        self.add_item(self.minutes_input)

    async def on_submit(self, interaction: discord.Interaction):
        if not is_mgmt(interaction.user):
            return await interaction.response.send_message("Permisiune refuzată.", ephemeral=True)
        date_str = self.date_input.value.strip()
        if not valid_date(date_str):
            return await reply(interaction, "Dată invalidă", "Format corect: YYYY-MM-DD")
        try:
            minutes_val = float(self.minutes_input.value.strip().replace(",", "."))
        except ValueError:
            return await reply(interaction, "Minute invalide", "Introdu un număr.")
        if not 0 < minutes_val <= 1440:  # also rejects nan/inf
            return await reply(interaction, "Minute invalide", "Trebuie să fie între 1 și 1440.")

        # Start at the first free second from 00:00:00 so it never collides with an existing clock_in
        occupied = {ci for ci, _ in get_clock_times(self.target.id, date_str)}
        start = parse_local(date_str, "00:00:00")
        while start.strftime("%H:%M:%S") in occupied:
            start += datetime.timedelta(seconds=1)
        end = start + datetime.timedelta(minutes=minutes_val)
        if end.date() != start.date():
            end = start.replace(hour=23, minute=59, second=59)
        ci, co = start.strftime("%H:%M:%S"), end.strftime("%H:%M:%S")

        add_clock_in(self.target.id, date_str, ci)
        update_clock_out(self.target.id, date_str, co, ci)
        await reply(interaction, "Minute adăugate", f"{self.target.mention} | Sesiune nouă {ci} -> {co} ({minutes_val:.0f}m)", discord.Color.green())
        await log_command(interaction, "adaugaminute-button", target=self.target, changed=True, extra=f"date={date_str} minutes={minutes_val}")
        await dm(self.target.id, f"{minutes_val:.0f} minute adaugate prin sesiunea {ci} -> {co}  de {interaction.user.display_name}.")

# --------------- HR: delete a session ---------------
def recent_sessions(uid: int, days: int = 7) -> list[tuple[str, str, str | None]]:
    """(date, clock_in, clock_out) for the last `days` days, newest first."""
    today = local_now().date()
    out = []
    for i in range(days):
        d = (today - datetime.timedelta(days=i)).isoformat()
        out += [(d, ci, co) for ci, co in reversed(get_clock_times(uid, d))]
    return out

async def _open_remove(interaction: discord.Interaction, member: discord.Member):
    view = RemovePontajView(interaction.user.id, member, recent_sessions(member.id), "ultimele 7 zile")
    await interaction.response.edit_message(embed=view.embed(interaction.user), view=view)
    await log_command(interaction, "stergepontaj-select-user", target=member)

class RemovePontajView(OwnedView):
    """Dropdown of a member's sessions plus "Altă dată...", then a "Sigur?" step before deleting."""
    OTHER = "other"

    def __init__(self, requester_id: int, target: discord.Member, sessions: list[tuple[str, str, str | None]], scope: str):
        super().__init__(requester_id)
        self.target, self.scope = target, scope
        unique = {(d, ci): co for d, ci, co in sessions if ci}  # select option values must be unique
        self.sessions = dict(list(unique.items())[:24])          # a select holds 25 options, one is "Altă dată..."
        self.pending: tuple[str, str] | None = None
        self.rebuild()

    def embed(self, user: discord.abc.User) -> discord.Embed:
        if self.pending:
            d, ci = self.pending
            return make_embed(
                "Sigur?", f"Ștergi sesiunea lui {self.target.mention}?\n{d}  {ci} - {self.sessions[self.pending] or '...'}",
                discord.Color.red(), user,
            )
        hint = "Alege sesiunea de șters." if self.sessions else "Nicio sesiune găsită. Poți alege «Altă dată...»."
        return make_embed("Șterge Pontaj", f"{self.target.mention} | {self.scope}\n{hint}", discord.Color.orange(), user)

    def rebuild(self):
        self.clear_items()
        if self.pending:
            self.add_item(_button("Confirmă ștergerea", discord.ButtonStyle.danger, 0, self._confirm))
            self.add_item(_button("Anulează", discord.ButtonStyle.secondary, 0, self._cancel))
            return
        options = [
            discord.SelectOption(
                label=f"{d}  {ci} - {co or '...'}",
                value=f"{d}|{ci}",
                description=f"{session_minutes(d, ci, co)} min" + ("" if co else ", activă"),
            )
            for (d, ci), co in self.sessions.items()
        ]
        options.append(discord.SelectOption(label="Altă dată...", value=self.OTHER, description="Introdu data manual"))
        select = discord.ui.Select(placeholder="Alege sesiunea", options=options)
        select.callback = partial(self._picked, select)
        self.add_item(select)

    async def _redraw(self, interaction: discord.Interaction):
        self.rebuild()
        await interaction.response.edit_message(embed=self.embed(interaction.user), view=self)

    async def _picked(self, select: discord.ui.Select, interaction: discord.Interaction):
        if select.values[0] == self.OTHER:
            return await interaction.response.send_modal(RemovePontajDateModal(self.target))
        self.pending = tuple(select.values[0].split("|", 1))
        await self._redraw(interaction)

    async def _cancel(self, interaction: discord.Interaction):
        self.pending = None
        await self._redraw(interaction)

    async def _confirm(self, interaction: discord.Interaction):
        if not is_mgmt(interaction.user):
            return await interaction.response.send_message("Permisiune refuzată.", ephemeral=True)
        if not self.pending:  # double click
            return await interaction.response.defer()
        d, ci = self.pending
        self.pending = None
        current = dict(get_clock_times(self.target.id, d))  # clock_in -> clock_out
        if ci not in current:
            return await interaction.response.edit_message(
                embed=make_embed("Șterge Pontaj", "Sesiunea nu mai există (modificată între timp).", discord.Color.orange(), interaction.user),
                view=None,
            )
        co = current[ci]
        remove_session(self.target.id, d, ci)
        await interaction.response.edit_message(
            embed=make_embed("Pontaj șters", f"{self.target.mention} | {d}\n{ci} - {co or '...'}", discord.Color.green(), interaction.user),
            view=None,
        )
        await log_command(interaction, "stergepontaj", target=self.target, changed=True, extra=f"date={d} start={ci} end={co}")
        await dm(self.target.id, f"Sesiunea ({ci} - {co or '...'} / {d}) a fost ștearsă de {interaction.user.display_name}.")

class RemovePontajDateModal(discord.ui.Modal, title="Dată Pontaj"):
    def __init__(self, target: discord.Member):
        super().__init__(timeout=180)
        self.target = target
        today = today_str()
        self.date_input = discord.ui.TextInput(label="Data (YYYY-MM-DD)", placeholder=today, default=today, max_length=10)
        self.add_item(self.date_input)

    async def on_submit(self, interaction: discord.Interaction):
        date_str = self.date_input.value.strip()
        if not valid_date(date_str):
            return await reply(interaction, "Dată invalidă", "Format corect: YYYY-MM-DD")
        sessions = [(date_str, ci, co) for ci, co in get_clock_times(self.target.id, date_str)]
        view = RemovePontajView(interaction.user.id, self.target, sessions, date_str)
        await interaction.response.edit_message(embed=view.embed(interaction.user), view=view)

# --------------- End-of-day confirmation ---------------
async def _send_eod_confirm_request(uid: int, *, date: str, start_time: str, end_time: str) -> bool:
    """Ask the user (DM, else the fallback channel) to press Confirm within the window, or the open session is dropped."""
    rounded = session_minutes(date, start_time, end_time)
    reminder = "**NU UITA SA PORNESTI PONTAJUL DUPA ORA 00:00**" if end_time == "23:59:59" else "**POTI PORNI PONTAJUL DUPA ORA 05:30**"
    text = (
        f"Confirmare pontaj PD pentru {date}\n"
        f"Start: {start_time} -> End propus: {end_time}. Total: {rounded} minute\n"
        f"Apasă **Confirm pontaj** în {EOD_CONFIRM_WINDOW_SECS // 60} minute pentru a salva.\n"
        f"Dacă nu confirmi, sesiunea NU va fi salvată.\n"
        f"{reminder}"
    )
    msg = await dm(uid, text, view=EodConfirmView())
    if msg is None:
        ch = bot.get_channel(EOD_FALLBACK_CHANNEL_ID)
        if isinstance(ch, discord.TextChannel):
            try:
                msg = await ch.send(f"<@{uid}>\n{text}", view=EodConfirmView())
            except (discord.HTTPException, OSError, aiohttp.ClientError):
                pass
    if msg is None:
        return False
    bot.pending_eod_confirms[msg.id] = {
        "uid": uid,
        "date": date,
        "ci": start_time,
        "end_time": end_time,
        "channel_id": msg.channel.id,
    }
    asyncio.create_task(_finalize_eod_confirm_after(msg.id, EOD_CONFIRM_WINDOW_SECS))
    return True

class EodConfirmView(BaseView):
    """The Confirm button on an end-of-day prompt; only the session owner can press it."""
    def __init__(self):
        super().__init__(timeout=EOD_CONFIRM_WINDOW_SECS + 60)

    @discord.ui.button(label="Confirm pontaj", style=discord.ButtonStyle.success)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = bot.pending_eod_confirms.get(interaction.message.id)
        if not data:
            return await interaction.response.send_message("Fereastra de confirmare a expirat.", ephemeral=True)
        if interaction.user.id != data["uid"]:
            return await interaction.response.send_message("Nu este pontajul tău.", ephemeral=True)
        bot.pending_eod_confirms.pop(interaction.message.id, None)
        end_time = data["end_time"]
        update_clock_out(data["uid"], data["date"], end_time)
        rounded = session_minutes(data["date"], data["ci"], end_time)
        self.stop()
        await interaction.response.edit_message(
            content=interaction.message.content + f"\n\nConfirmat – pontaj salvat.\nInterval: {data['ci']} → {end_time} ({rounded} minute)",
            view=None,
        )
        log_ch = bot.get_channel(EOD_CONFIRMED_LOG_CHANNEL_ID)
        if log_ch:
            desc = (
                f"<@{data['uid']}> (`{data['uid']}`)\n"
                f"Tip: PD\n"
                f"Dată: {data['date']}\n"
                f"Interval: {data['ci']} → {end_time} ({rounded} min)\n"
                f"Status: Confirmat"
            )
            try:
                await log_ch.send(embed=make_embed("EOD confirmat", desc, discord.Color.green()))
            except discord.HTTPException:
                pass
        _append_log_line(
            f"[EOD] CONFIRMED uid={data['uid']} type=PD date={data['date']} start={data['ci']} end={end_time} mins={rounded}"
        )

async def _edit_eod_message(channel_id: int, message_id: int, suffix: str):
    try:
        ch = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        msg = await ch.fetch_message(message_id)
        await msg.edit(content=msg.content + suffix, view=None)
    except (discord.HTTPException, OSError, aiohttp.ClientError):
        pass

async def _finalize_eod_confirm_after(message_id: int, delay: int):
    await asyncio.sleep(delay)
    data = bot.pending_eod_confirms.pop(message_id, None)
    if not data:
        return
    uid, date, ci = data["uid"], data["date"], data["ci"]
    # Only drop the session if it is still open: a manual Clock OUT inside the window keeps it
    still_open = any(s_ci == ci and s_co is None for s_ci, s_co in get_clock_times(uid, date))
    if still_open:
        remove_session(uid, date, ci)
        suffix, status = "\n\nNeconfirmat în timp – pontaj Nesalvat.", "NU a confirmat în timp – sesiunea a fost ștearsă."
    else:
        suffix, status = "\n\nÎnchis manual cu Clock OUT – pontaj păstrat.", "Închisă manual cu Clock OUT – sesiunea a fost păstrată."
    await _edit_eod_message(data["channel_id"], message_id, suffix)

    log_ch = bot.get_channel(EOD_UNCONFIRMED_LOG_CHANNEL_ID)
    if log_ch:
        text = f"<@{uid}> (`{uid}`)\nTip: PD\nDată: {date}\nStart: {ci}\nStatus: {status}"
        try:
            await log_ch.send(embed=make_embed("EOD neconfirmat", text, discord.Color.orange()))
        except discord.HTTPException:
            pass
    _append_log_line(f"[EOD] {'NOT_CONFIRMED' if still_open else 'CLOSED_MANUALLY'} uid={uid} type=PD date={date} start={ci}")

# --------------- DNS ---------------
class StickyResolver(aiohttp.ThreadedResolver):
    """System DNS, but when a lookup fails reuse the last good answer for that host."""

    def __init__(self):
        super().__init__()
        self._last: dict = {}

    async def resolve(self, host, port=0, family=socket.AF_INET):
        key = (host, port, family)
        try:
            self._last[key] = await super().resolve(host, port, family)
        except OSError:
            if key not in self._last:
                raise
            logging.warning("DNS lookup for %s failed, using the last known address", host)
        return self._last[key]

# --------------- Bot ---------------
class Bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        # chisle: no message_content intent, so prefix commands only parse as "@bot dbs" (mention content is always delivered)
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.last_auto_close_day: str | None = None
        self.last_auto_close_night: str | None = None
        self.last_week_reminder: str | None = None
        self.pending_eod_confirms: dict[int, dict] = {}

    async def login(self, token: str) -> None:
        # The host's DNS flaps ("Temporary failure in name resolution") and every failed lookup was a lost click.
        # discord.py builds its connector inside login(); the resolver needs the running loop, so set it here.
        self.http.connector = aiohttp.TCPConnector(limit=0, family=socket.AF_INET, ttl_dns_cache=300, resolver=StickyResolver())
        await super().login(token)

    async def setup_hook(self):
        self.add_view(ClockButtons())
        self.add_view(HrButtons())
        try:
            if DEV_GUILD_ID:
                synced = await self.tree.sync(guild=discord.Object(id=DEV_GUILD_ID))
            else:
                synced = await self.tree.sync()
            logging.info("Synced %d slash commands", len(synced))
        except Exception:
            logging.exception("Slash command sync failed")

        self.auto_close_today_sessions.start()
        self.sync_concedii.start()

        if PONTAJE_API_TOKEN:
            try:
                await _start_pontaje_api()
            except Exception:
                logging.exception("Failed to start Pontaje API server")

    @tasks.loop(minutes=30)
    async def sync_concedii(self):
        try:
            changed = await asyncio.to_thread(sync_concedii_to_sheet)
            if changed:
                logging.info("Concedii sync: %d CO cells written", changed)
        except Exception:
            logging.exception("Concedii sync failed")

    @tasks.loop(minutes=1)
    async def auto_close_today_sessions(self):
        now = local_now()
        day = now.strftime("%Y-%m-%d")
        if (now.hour, now.minute) == (5, 25) and self.last_auto_close_night != day:
            self.last_auto_close_night = day
            await self._send_eod_prompts(day, "05:30:00", "Night Shift Confirm (05:25)")
        elif (now.hour, now.minute) == (23, 55) and self.last_auto_close_day != day:
            self.last_auto_close_day = day
            await self._send_eod_prompts(day, "23:59:59", "EOD Confirm (23:55)")
        elif now.weekday() == 3 and (now.hour, now.minute) == (15, 0) and self.last_week_reminder != day:  # Thursday
            self.last_week_reminder = day
            await self._send_week_reminders(now.date())

    async def _log_summary(self, title: str, summary: str):
        _append_log_line(f"[{title}] {summary}")
        ch = self.get_channel(LOGS_CHANNEL_ID)
        if ch:
            try:
                await ch.send(embed=make_embed(title, summary, discord.Color.teal()))
            except discord.HTTPException:
                pass

    async def _send_eod_prompts(self, day: str, end_time: str, title: str):
        # At 05:25 every session still open today started after 00:00, i.e. it is a night-shift session
        sent = 0
        for uid, date, ci in get_ongoing_sessions():
            if date == day and await _send_eod_confirm_request(uid, date=day, start_time=ci, end_time=end_time):
                sent += 1
        await self._log_summary("EOD Confirm", f"{title} trimis pentru {day}: PD={sent} (fereastră {EOD_CONFIRM_WINDOW_SECS // 60}m)")

    async def _send_week_reminders(self, today: datetime.date):
        """DM PD members who clocked in this week but are still under the weekly target (fallback channel if DMs are closed)."""
        dates = week_dates(today)
        panel = self.get_channel(ALLOWED_CHANNEL_ID)
        if panel is None:
            return logging.warning("Week reminder skipped: panel channel not in cache")
        members = [m for m in await fetch_members(panel.guild, get_user_ids_for_dates(dates)) if has_any(m, REQUIRED_PD_ROLE_ID)]
        fallback = self.get_channel(EOD_FALLBACK_CHANNEL_ID)
        by_dm = by_channel = failed = 0
        for m in members:
            total = sum(day_minutes(m.id, d) for d in dates)
            if total >= WEEKLY_TARGET_MINUTES:
                continue
            text = (
                f"Săptămâna aceasta ({ddmm(dates[0])} - {ddmm(dates[-1])}) ai {total} / {WEEKLY_TARGET_MINUTES} minute de pontaj.\n"
                f"Mai ai nevoie de {WEEKLY_TARGET_MINUTES - total} minute până sâmbătă seara."
            )
            if await dm(m.id, text):
                by_dm += 1
                continue
            sent = False
            if isinstance(fallback, discord.TextChannel):
                try:
                    await fallback.send(f"{m.mention} {text}")
                    sent = True
                except (discord.HTTPException, OSError, aiohttp.ClientError):
                    pass
            by_channel += sent
            failed += not sent
        await self._log_summary(
            "Reminder săptămânal",
            f"Sub {WEEKLY_TARGET_MINUTES} min: DM={by_dm}, canal={by_channel}, eșuat={failed} (verificați {len(members)})",
        )

    @auto_close_today_sessions.before_loop
    async def before_auto_close_today_sessions(self):
        await self.wait_until_ready()

bot = Bot()

# ---- Slash command checks ----
def in_channel_check(channel_id: int):
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.channel or interaction.channel.id != channel_id:
            raise app_commands.CheckFailure(f"WRONG_CHANNEL:{channel_id}")
        return True
    return app_commands.check(predicate)

def hr_or_conducere_check():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not is_mgmt(interaction.user):
            raise app_commands.CheckFailure("NO_HR_OR_CONDUCERE")
        return True
    return app_commands.check(predicate)

# --------------- Events ---------------
@bot.event
async def on_ready():
    logging.info(f"Logged in as {bot.user} ({bot.user.id})")

@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    # A deleted prompt just stops waiting; the session itself is left alone
    bot.pending_eod_confirms.pop(payload.message_id, None)

@bot.event
async def on_raw_bulk_message_delete(payload: discord.RawBulkMessageDeleteEvent):
    for mid in payload.message_ids:
        bot.pending_eod_confirms.pop(mid, None)

# --------------- Error Handler ---------------
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    command = interaction.command.name if interaction.command else "unknown"
    if isinstance(error, app_commands.CheckFailure):
        key = str(error)
        if key.startswith("WRONG_CHANNEL:"):
            msg = f"Folosește comanda în <#{key.split(':', 1)[1]}>."
        elif key == "NO_HR_OR_CONDUCERE":
            msg = "Necesită HR sau Conducere."
        else:
            msg = "Nu ai permisiune."
        await reply(interaction, "Permisiune", msg)
        await log_command(interaction, command, extra=msg, success=False)
        return

    logging.error("App command error", exc_info=error)
    await reply(interaction, "Eroare", "Eroare internă.")
    await log_command(interaction, command, extra=repr(error), success=False)

# --------------- Slash Commands ---------------
async def _post_panel(interaction: discord.Interaction, title: str, desc: str, view: discord.ui.View, action: str):
    if interaction.user.id != OWNER_ID:
        return await reply(interaction, "Permisiune", "Restricționat.")
    await interaction.response.send_message(embed=make_embed(title, desc, discord.Color.blurple(), interaction.user), view=view)
    await log_command(interaction, action)

@bot.tree.command(name="clockpanel", description="Postează panoul Clock IN/OUT (HR sau Conducere)")
@in_channel_check(ALLOWED_CHANNEL_ID)
@hr_or_conducere_check()
async def clockpanel_cmd(interaction: discord.Interaction):
    await _post_panel(interaction, "Pontaj", "Folosește butoanele pentru a începe / opri pontajul.", ClockButtons(), "clockpanel")

@bot.tree.command(name="hrpanel", description="Postează panoul HR")
@in_channel_check(ALLOWED_CHANNEL_ID)
@hr_or_conducere_check()
async def hrpanel_cmd(interaction: discord.Interaction):
    await _post_panel(interaction, "HR Panel", "Folosește butoanele pentru a interactiona cu panoul HR.", HrButtons(), "hrpanel")

# --------------- Owner DB commands ("@bot dbv" / "@bot dbs") ---------------
@bot.command(name="dbv", aliases=["dbvacumm"], help="Checkpoint WAL + VACUUM (owner only)")
async def dbvacuum_prefix(ctx: commands.Context):
    if ctx.author.id != OWNER_ID:
        return await ctx.reply("Permisiune refuzată.", mention_author=False, delete_after=5)
    try:
        checkpoint_and_vacuum()
        await ctx.reply(f"VACUUM OK. {db_stats()}", mention_author=False)
    except Exception as e:
        await ctx.reply(f"Eroare: {e}", mention_author=False)

@bot.command(name="dbs", aliases=["dbstats"], help="Afișează statistici DB (owner only)")
async def db_stats_command(ctx: commands.Context):
    if ctx.author.id != OWNER_ID:
        return await ctx.reply("Permisiune refuzată.", mention_author=False, delete_after=5)
    await ctx.reply(f"DB Stats:\n{db_stats()}", mention_author=False)

# --------------- Pontaje HTTP API ---------------
async def _handle_pontaje_request(request: aiohttp_web.Request) -> aiohttp_web.Response:
    """GET /api/pontaje?discord_id=<id>&date=<YYYY-MM-DD>"""
    if request.headers.get("Authorization", "") != f"Bearer {PONTAJE_API_TOKEN}":
        return aiohttp_web.json_response({"error": "Unauthorized"}, status=401)

    discord_id_raw = request.query.get("discord_id")
    date_str = request.query.get("date")
    if not discord_id_raw or not date_str:
        return aiohttp_web.json_response({"error": "Missing required query params: discord_id, date"}, status=400)
    try:
        discord_id = int(discord_id_raw)
    except ValueError:
        return aiohttp_web.json_response({"error": "discord_id must be an integer"}, status=400)
    if not valid_date(date_str):
        return aiohttp_web.json_response({"error": "date must be in YYYY-MM-DD format"}, status=400)

    sessions = []
    for ci, co in get_clock_times(discord_id, date_str):
        if not ci:
            continue
        entry = {"clock_in": ci, "clock_out": co, "minutes": session_minutes(date_str, ci, co)}
        if not co:
            entry["ongoing"] = True
        sessions.append(entry)

    return aiohttp_web.json_response({
        "discord_id": discord_id,
        "date": date_str,
        "sessions": sessions,
        "total_minutes": sum(s["minutes"] for s in sessions),
    })

async def _handle_health(request: aiohttp_web.Request) -> aiohttp_web.Response:
    return aiohttp_web.json_response({"status": "ok"})

async def _start_pontaje_api():
    app = aiohttp_web.Application()
    app.router.add_get("/api/pontaje", _handle_pontaje_request)
    app.router.add_get("/health", _handle_health)
    runner = aiohttp_web.AppRunner(app, access_log=None)  # the port is public; scanner 404s flooded the console
    await runner.setup()
    await aiohttp_web.TCPSite(runner, "0.0.0.0", PONTAJE_API_PORT).start()
    logging.info(f"Pontaje API listening on 0.0.0.0:{PONTAJE_API_PORT}")

# --------------- Run ---------------
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("BOT_TOKEN missing (regenerate in Developer Portal).")
    # basicConfig above already prints; discord.py's default handler printed every line twice
    bot.run(TOKEN, log_handler=None)
