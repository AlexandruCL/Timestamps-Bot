"""HR actions shared by the Discord views and the web API: each writes, notifies and logs the same way from both."""
import asyncio
import datetime
import logging

import discord

import bot2 as b  # the running bot module (bot2.py registers itself under this name when run as a script)
import sheets


async def stop_session(actor: discord.abc.User, guild: discord.Guild, uid: int, date: str, ci: str, source: str = "Discord"):
    """Delete an open session (its minutes are not kept), DM the owner, log."""
    b.remove_session(uid, date, ci)
    await b.dm(uid, f"Sesiunea ta ({ci} - {date}) a fost oprită de {actor.display_name}.")
    target = (await b.fetch_members(guild, [uid]) or [None])[0]
    await b.log_action(actor, "ongoing-stop-button", target=target, changed=True, extra=f"uid={uid} start={ci}", source=source)


def added_session_bounds(uid: int, date: str, minutes: float) -> tuple[str, str]:
    """(clock_in, clock_out) of the session add_minutes would write: from the first free second after 00:00:00, capped at 23:59:59."""
    occupied = {ci for ci, _ in b.get_clock_times(uid, date)}
    start = b.parse_local(date, "00:00:00")
    while start.strftime("%H:%M:%S") in occupied:  # never collide with an existing clock_in
        start += datetime.timedelta(seconds=1)
    end = start + datetime.timedelta(minutes=minutes)
    if end.date() != start.date():
        end = start.replace(hour=23, minute=59, second=59)
    return start.strftime("%H:%M:%S"), end.strftime("%H:%M:%S")


async def add_minutes(actor: discord.abc.User, target: discord.abc.User, date: str, minutes: float, source: str = "Discord") -> tuple[str, str]:
    """Write a closed session of `minutes` on `date`, log, DM the member. Returns (clock_in, clock_out)."""
    ci, co = added_session_bounds(target.id, date, minutes)
    b.add_clock_in(target.id, date, ci)
    b.update_clock_out(target.id, date, co, ci)
    await b.log_action(actor, "adaugaminute-button", target=target, changed=True, extra=f"date={date} minutes={minutes}", source=source)
    await b.dm(target.id, f"{minutes:.0f} minute adaugate prin sesiunea {ci} -> {co}  de {actor.display_name}.")
    return ci, co


async def delete_session(actor: discord.abc.User, target: discord.abc.User, date: str, ci: str, co: str | None, source: str = "Discord"):
    b.remove_session(target.id, date, ci)
    await b.log_action(actor, "stergepontaj", target=target, changed=True, extra=f"date={date} start={ci} end={co}", source=source)
    await b.dm(target.id, f"Sesiunea ({ci} - {co or '...'} / {date}) a fost ștearsă de {actor.display_name}.")


async def add_warn(actor: discord.abc.User, guild: discord.Guild, target: discord.abc.User, reason: str, source: str = "Discord") -> int | None:
    """Public post (pings Conducere + HR at 3/3), DM, log. None when the member already has 3/3."""
    if b.get_punish_count(target.id) >= 3:
        return None
    new_count = b.increment_punish_count(target.id)
    await b.post_warn(
        guild, actor, target, "Avertisment",
        f"Avertizat de {actor.mention}\nWarn {new_count}/3\n\n{reason}",
        discord.Color.red() if new_count == 3 else discord.Color.orange(),
        ping_mgmt=new_count == 3,
    )
    await b.dm(target.id, f"Ai primit un warn ({new_count}/3) de la {actor.display_name}: {reason}")
    await b.log_action(actor, "warn-add-ui", target=target, changed=True, extra=f"new_count={new_count}", source=source)
    return new_count


async def reset_warns(actor: discord.abc.User, guild: discord.Guild, target: discord.abc.User, note: str, source: str = "Discord"):
    b.reset_punish_count(target.id)
    await b.post_warn(guild, actor, target, "Reset Warn-uri", f"Resetat de {actor.mention}.\n{note.strip()}", discord.Color.green())
    await b.dm(target.id, f"Warn-urile tale au fost resetate de {actor.display_name}.")
    await b.log_action(actor, "warn-reset-ui", target=target, changed=True, source=source)


async def push_days_to_sheet(actor: discord.abc.User, guild: discord.Guild, dates: list[str], source: str = "Discord") -> list[dict]:
    """Send each day to the pontaje sheet. One result per day; a failed day gets "error" and the rest still go."""
    results = []
    for date in dates:
        minutes, no_callsign = await b.day_minutes_by_callsign(date, guild)
        res = {"date": date, "column": b.sheet_day_column(date), "rows_written": 0, "slash": [], "not_found": [], "no_callsign": no_callsign}
        results.append(res)
        try:
            missing, slashed = await asyncio.to_thread(b.push_day_to_sheet, date, minutes)
        except Exception as e:
            logging.exception("Sheet push failed for %s", date)
            res["error"] = str(e)
            continue
        b.mark_sheet_export(date, actor.id, int(b.local_now().timestamp()))
        res.update(rows_written=len(minutes) - len(missing), slash=slashed, not_found=missing)
        await b.log_action(actor, "day-report-sheet", extra=f"date={date} rows={res['rows_written']}", source=source)
    sheets.invalidate("pontaje")
    return results
