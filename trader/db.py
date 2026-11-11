import json
import os
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, mode TEXT, started TEXT, source TEXT, config TEXT, summary TEXT);
CREATE TABLE IF NOT EXISTS trades (run_id INT, ticker TEXT, entry_time TEXT, exit_time TEXT, qty REAL,
    entry_price REAL, exit_price REAL, pnl REAL, ret_pct REAL, reason TEXT);
CREATE TABLE IF NOT EXISTS equity (run_id INT, ts TEXT, equity REAL, benchmark REAL);
CREATE TABLE IF NOT EXISTS events (run_id INT, ts TEXT, kind TEXT, ticker TEXT, detail TEXT);
"""


def connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path, check_same_thread=False)
    con.executescript(SCHEMA)
    cols = [r[1] for r in con.execute("PRAGMA table_info(trades)")]
    if "sleeve" not in cols:
        con.execute("ALTER TABLE trades ADD COLUMN sleeve TEXT")
    con.execute("CREATE TABLE IF NOT EXISTS swing_positions (symbol TEXT PRIMARY KEY, sleeve TEXT, "
                "entry_price REAL, entry_date TEXT, qty REAL)")
    con.commit()
    return con


def new_run(con, mode: str, source: str, cfg: dict) -> int:
    cur = con.execute("INSERT INTO runs (mode, started, source, config) VALUES (?,?,?,?)",
                      (mode, datetime.now(timezone.utc).isoformat(), source,
                       json.dumps({**cfg, "db_path": os.path.basename(cfg.get("db_path", "trader.db"))})))
    con.commit()
    return cur.lastrowid


def log_event(con, run_id, kind, ticker=None, detail=None):
    con.execute("INSERT INTO events VALUES (?,?,?,?,?)",
                (run_id, datetime.now(timezone.utc).isoformat(), kind, ticker,
                 json.dumps(detail, default=str) if detail is not None else None))
    con.commit()
