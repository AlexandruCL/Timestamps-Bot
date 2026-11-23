import sqlite3
from contextlib import closing

DB_PATH = "clock_times.db"
PUNISH_DB_PATH = "punishments.db"

def _exec(sql: str, params=(), db: str = DB_PATH) -> list:
    """Run one statement on its own connection, commit, and return the fetched rows."""
    with closing(sqlite3.connect(db)) as conn, conn:
        return conn.execute(sql, params).fetchall()

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS clock_times (
                 user_id INTEGER,
                 date TEXT,
                 clock_in TEXT,
                 clock_out TEXT)''')
    try:
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA synchronous=NORMAL;")
        c.execute("PRAGMA wal_autocheckpoint=1000;")      # checkpoint roughly every ~1k pages
        c.execute("PRAGMA journal_size_limit=10485760;")   # cap WAL to ~10 MB
        c.execute("PRAGMA temp_store=MEMORY;")             # avoid /tmp writes
        c.execute("PRAGMA auto_vacuum=INCREMENTAL;")
    except Exception:
        pass
    conn.commit()
    conn.close()
    _exec("CREATE TABLE IF NOT EXISTS punishments (user_id INTEGER PRIMARY KEY, count INTEGER)", db=PUNISH_DB_PATH)
    _exec("CREATE TABLE IF NOT EXISTS sheet_exports (date TEXT PRIMARY KEY, user_id INTEGER, sent_at INTEGER)")
    _exec("""CREATE TABLE IF NOT EXISTS logs (
             id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT, action TEXT, actor_id INTEGER, actor_name TEXT,
             target_id INTEGER, target_name TEXT, extra TEXT, changed INTEGER, success INTEGER, source TEXT)""")
    _exec("CREATE INDEX IF NOT EXISTS logs_ts ON logs (ts_utc)")
    _exec("CREATE INDEX IF NOT EXISTS logs_action ON logs (action)")

def get_sheet_export(date: str) -> tuple[int, int] | None:
    """(user_id, unix time) of the last time this day was sent to the pontaje sheet, or None."""
    rows = _exec("SELECT user_id, sent_at FROM sheet_exports WHERE date = ?", (date,))
    return rows[0] if rows else None

def mark_sheet_export(date: str, user_id: int, sent_at: int):
    _exec("INSERT OR REPLACE INTO sheet_exports (date, user_id, sent_at) VALUES (?, ?, ?)", (date, user_id, sent_at))

def checkpoint_and_vacuum() -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        conn.execute("VACUUM;")
    finally:
        conn.close()

def db_stats() -> str:
    try:
        pages = _exec("PRAGMA page_count")[0][0]
        psize = _exec("PRAGMA page_size")[0][0]
        freep = _exec("PRAGMA freelist_count")[0][0]
        return f"size={pages * psize}B free={freep * psize}B page_size={psize}"
    except Exception as e:
        return f"stats_error:{e}"

# ---------- Clock sessions ----------
def add_clock_in(user_id, date, clock_in):
    _exec("INSERT INTO clock_times (user_id, date, clock_in, clock_out) VALUES (?, ?, ?, NULL)", (user_id, date, clock_in))

def update_clock_out(user_id, date, clock_out, start_time: str | None = None):
    if start_time:
        # Close only the session that started at start_time
        _exec("UPDATE clock_times SET clock_out = ? WHERE user_id = ? AND date = ? AND clock_in = ?",
              (clock_out, user_id, date, start_time))
    else:
        # Close whatever session is still open that day
        _exec("UPDATE clock_times SET clock_out = ? WHERE user_id = ? AND date = ? AND clock_out IS NULL",
              (clock_out, user_id, date))

def get_clock_times(user_id, date):
    return _exec("SELECT clock_in, clock_out FROM clock_times WHERE user_id = ? AND date = ? ORDER BY clock_in", (user_id, date))

def get_clock_times_for_dates(dates: list[str]) -> list[tuple[int, str, str, str | None]]:
    """(user_id, date, clock_in, clock_out) for every session on the given dates."""
    if not dates:
        return []
    placeholders = ",".join("?" for _ in dates)
    return _exec(f"SELECT user_id, date, clock_in, clock_out FROM clock_times WHERE date IN ({placeholders})", dates)

def get_ongoing_sessions():
    return _exec("SELECT user_id, date, clock_in FROM clock_times WHERE clock_out IS NULL ORDER BY clock_in")

def remove_session(user_id, date, clock_in):
    _exec("DELETE FROM clock_times WHERE user_id = ? AND date = ? AND clock_in = ?", (user_id, date, clock_in))

def get_user_ids_for_dates(dates: list[str]) -> list[int]:
    """Distinct user_ids that have at least one clock entry on any of the given dates."""
    if not dates:
        return []
    placeholders = ",".join("?" for _ in dates)
    return [r[0] for r in _exec(f"SELECT DISTINCT user_id FROM clock_times WHERE date IN ({placeholders})", dates)]

# ---------- Warns ----------
def get_punish_count(user_id):
    rows = _exec("SELECT count FROM punishments WHERE user_id = ?", (user_id,), PUNISH_DB_PATH)
    return rows[0][0] if rows else 0

def reset_punish_count(user_id):
    _exec("UPDATE punishments SET count = 0 WHERE user_id = ?", (user_id,), PUNISH_DB_PATH)

def increment_punish_count(user_id):
    # One atomic statement (needs SQLite 3.24+), so two warns at once can't both read the same old count
    _exec("INSERT INTO punishments (user_id, count) VALUES (?, 1) ON CONFLICT(user_id) DO UPDATE SET count = count + 1",
          (user_id,), PUNISH_DB_PATH)
    return get_punish_count(user_id)

# ---------- Action log ----------
LOG_COLUMNS = ("id", "ts_utc", "action", "actor_id", "actor_name", "target_id", "target_name", "extra", "changed", "success", "source")

def insert_logs(rows: list[tuple]):
    """rows: (ts_utc, action, actor_id, actor_name, target_id, target_name, extra, changed, success, source)."""
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.executemany(f"INSERT INTO logs ({', '.join(LOG_COLUMNS[1:])}) VALUES ({', '.join('?' * 10)})", rows)

def query_logs(kind: str = "all", q: str = "", before_id: int | None = None, limit: int = 50) -> list[tuple]:
    """Newest first. kind: all | changes | info | failures; q matches action, names and extra."""
    where, params = [], []
    if kind == "changes":
        where.append("changed = 1")
    elif kind == "info":
        where.append("changed = 0")
    elif kind == "failures":
        where.append("success = 0")
    if q:
        like = "%" + q.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
        cols = ("action", "actor_name", "target_name", "extra", "CAST(actor_id AS TEXT)", "CAST(target_id AS TEXT)")
        where.append("(" + " OR ".join(f"{c} LIKE ? ESCAPE '!'" for c in cols) + ")")
        params += [like] * 6
    if before_id is not None:
        # by time, not id: rows imported from logs.txt get ids after rows the bot already wrote
        where.append("(ts_utc, id) < (SELECT ts_utc, id FROM logs WHERE id = ?)")
        params.append(before_id)
    sql = f"SELECT {', '.join(LOG_COLUMNS)} FROM logs" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY ts_utc DESC, id DESC LIMIT ?"
    return _exec(sql, (*params, limit))
