# Bot Pontaje: Discord Time Tracking

A Discord bot that tracks shift time (pontaje) for a Police Department (PD) roster. Members clock in and out from a button panel, HR and leadership (Conducere) manage sessions and warns from a second panel, and finished days are written to a Google Sheet.

The bot's interface is in Romanian.

## Features

### Clock panel (PD members)

- **Clock IN / Clock OUT** buttons. One open session per member per day.
- **Pontajele Mele** shows your sessions for a day, the day total against the daily target, and the Sunday to Saturday week total against the weekly target. Arrows move between days.
- Minutes are rounded to the nearest 5 per session.
- The buttons are locked from 23:55 to 00:00 and from 05:25 to 05:30 while end-of-day prompts run.

### End-of-day confirmation

- At 23:55 every session still open gets a prompt (DM, or a fallback channel if DMs are closed) proposing an end time of 23:59:59.
- At 05:25 night-shift sessions get the same prompt with an end time of 05:30:00.
- The member has `EOD_CONFIRM_WINDOW_SECS` (5 minutes by default) to press **Confirm pontaj**. Unconfirmed sessions are not saved. Confirmed and unconfirmed results are logged to separate channels.

### HR panel (HR and Conducere roles)

- **Pontaje Deschise**: today's open sessions, with a Stop button per member (asks for confirmation, then deletes the session).
- **Warn**: pick a member, add a warn with a reason or reset their count. Each warn is posted in the warn channel; the third one also pings HR and Conducere. Counts are stored in `punishments.db`.
- **Pontaje / ZI**: day report for all PD members (sorted by callsign) or for one member with per-session detail. Opens on yesterday, with day arrows and a month calendar picker for older days.
- **Adaugă Minute**: add minutes to a member on a given date.
- **Șterge Pontaj**: delete one of a member's sessions from the last 7 days, or from a date you type.

### Google Sheets export

Users listed in `SHEET_EXPORT_USER_IDS` see two extra buttons on the day report:

- **Trimite în Sheet** writes the viewed day into the weekday column (F to L, Monday to Sunday) of the `Monitorizare Activitate` tab. Each row with a callsign and a name gets that member's minutes, or 0.
- **Trimite Săptămâna** does the same for every finished day of the viewed Sunday to Saturday week.
- Members on leave (`Concedii` tab of the PD spreadsheet) get `CO`. Existing `CO` cells are never overwritten.
- Members hired that day (`DATA ANGAJARE` in `Evidenta membrii`) who are under the daily target get `/`.
- The bot remembers which days were sent, by whom and when. Sending a day again asks for confirmation first.
- The reply lists callsigns missing from the sheet, members without a `[callsign]` in their nickname, and who got `/`.
- Every 30 minutes the bot copies the current week's leave from `Concedii` into the sheet as `CO` (skipped on Sundays, before the new week is sent).

### Reminders and logging

- Every Thursday at 15:00, PD members who clocked in this week but are under `WEEKLY_TARGET_MINUTES` get a DM with their total and how many minutes they still need.
- Every action is logged to `LOGS_CHANNEL_ID` and `logs.txt`. Added minutes, stopped sessions and deleted sessions are also copied to their own channels.

### HTTP API

When `PONTAJE_API_TOKEN` is set, the bot serves a small API on `PONTAJE_API_PORT` so other bots can read sessions:

```
GET /api/pontaje?discord_id=<id>&date=<YYYY-MM-DD>
Authorization: Bearer <PONTAJE_API_TOKEN>
```

```json
{
  "discord_id": 123,
  "date": "2026-11-18",
  "sessions": [{"clock_in": "18:02:11", "clock_out": "20:15:40", "minutes": 135}],
  "total_minutes": 135
}
```

An open session has `"ongoing": true` and counts up to now. `GET /health` returns `{"status": "ok"}`.

### Commands

| Command | Who | What |
|---|---|---|
| `/clockpanel` | Owner, in the panel channel | Posts the clock panel |
| `/hrpanel` | Owner, in the panel channel | Posts the HR panel |
| `@bot dbv` | Owner | WAL checkpoint and `VACUUM` on the database |
| `@bot dbs` | Owner | Database size and free space |

The panels are persistent: their buttons keep working after a restart.

## Setup

Requires Python 3.10+ and SQLite 3.24+.

```bash
pip install discord.py python-dotenv gspread
```

Create a `.env` file:

```env
BOT_TOKEN=
ALLOWED_CHANNEL_ID=            # channel holding both panels
ALLOWED_PUNISH_CHANNEL_ID=     # warns are posted here
LOGS_CHANNEL_ID=
REQUIRED_PD_ROLE_NAME=         # comma-separated PD role ids
REQUIRED_HR_ROLE_NAME=         # HR role id
CONDUCERE_ROLE_ID=

# optional
ADDMINUTES_LOG_CHANNEL_ID=     # comma-separated extra log channels
ONGOING_STOP_CHANNEL_ID=
DELETE_PONTAJ_CHANNEL_ID=
SHEET_EXPORT_USER_IDS=         # comma-separated user ids allowed to send to the sheet
GOOGLE_SHEETS_CREDENTIALS_FILE=credentials.json
PONTAJE_SHEET_ID=
PD_SPREADSHEET_ID=
PONTAJE_API_TOKEN=             # API is off when empty
PONTAJE_API_PORT=3080
DEV_GUILD_ID=                  # sync slash commands to one guild only
EOD_CONFIRM_WINDOW_SECS=300
WEEKLY_TARGET_MINUTES=700
DAILY_TARGET_MINUTES=100
TIMEZONE=Europe/Bucharest
```

The owner id and the end-of-day channel ids are constants at the top of `pontaje.py`.

For the sheet export, create a Google service account, save its key as `credentials.json`, and share both spreadsheets with the service account's email.

Run:

```bash
python pontaje.py
```

## Files

```
pontaje.py    the bot: panels, views, scheduled tasks, Sheets export, HTTP API
database.py   SQLite access (clock_times.db, punishments.db)
```

### Database

`clock_times.db` (WAL mode):

- `clock_times(user_id, date, clock_in, clock_out)`: one row per session, `clock_out` is NULL while open.
- `sheet_exports(date, user_id, sent_at)`: last time each day was sent to the sheet.

`punishments.db`:

- `punishments(user_id, count)`: warn count per member.

## License

Proprietary, showcase only. See [LICENSE](LICENSE). Viewing is allowed; any other use needs the owner's written permission.

Also see the [Privacy Policy](Privacy-Policy.md) and [Terms of Service](TermsOfService.md).

## Author

Cristea Laur-Alexandru ([@AlexandruCL](https://github.com/AlexandruCL))
