"""SQLite 存储。没有历史价格，就无法判断"这个价到底算不算便宜"。"""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS offers (
    id            INTEGER PRIMARY KEY,
    fetched_at    TEXT    NOT NULL,   -- UTC ISO8601，本次抓取时刻
    watch         TEXT    NOT NULL,   -- config 里的 watch name
    origin        TEXT    NOT NULL,
    dest          TEXT    NOT NULL,
    depart_date   TEXT    NOT NULL,   -- 出发日 YYYY-MM-DD
    price         INTEGER NOT NULL,
    currency      TEXT    NOT NULL,
    airlines      TEXT    NOT NULL,   -- 逗号分隔
    stops         INTEGER NOT NULL,
    duration_min  INTEGER,           -- 各航段飞行时间之和，不含中转等待
    dep_time      TEXT,               -- HH:MM
    arr_time      TEXT,
    route         TEXT                -- STL>CLT>DFW>PVG，用于人工核对中转
);
CREATE INDEX IF NOT EXISTS idx_offers_watch_date
    ON offers (watch, depart_date, fetched_at);

-- 抓取健康日志。最坏的失败不是抓错价，是静默死掉而你以为还在监控。
CREATE TABLE IF NOT EXISTS fetch_log (
    id          INTEGER PRIMARY KEY,
    fetched_at  TEXT    NOT NULL,
    watch       TEXT    NOT NULL,
    depart_date TEXT    NOT NULL,
    ok          INTEGER NOT NULL,
    n_offers    INTEGER NOT NULL DEFAULT 0,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_fetchlog_watch ON fetch_log (watch, fetched_at);

-- Google 自己给出的价格水平（low/typical/high）。它背后是我们拿不到的全量历史，
-- 所以在自有历史还很薄的头几周，这是唯一可信的"贵贱"参考。
CREATE TABLE IF NOT EXISTS insights (
    id          INTEGER PRIMARY KEY,
    fetched_at  TEXT NOT NULL,
    watch       TEXT NOT NULL,
    depart_date TEXT NOT NULL,
    price_level TEXT NOT NULL       -- low / typical / high
);
CREATE INDEX IF NOT EXISTS idx_insights_watch ON insights (watch, fetched_at);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def save_offers(conn, fetched_at: str, watch, depart_date: str, offers: list[dict]) -> None:
    conn.executemany(
        """INSERT INTO offers
           (fetched_at, watch, origin, dest, depart_date, price, currency,
            airlines, stops, duration_min, dep_time, arr_time, route)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                fetched_at, watch.name, watch.origin, watch.dest, depart_date,
                o["price"], watch.currency, ",".join(o["airlines"]), o["stops"],
                o["duration_min"], o["dep_time"], o["arr_time"], o["route"],
            )
            for o in offers
        ],
    )
    conn.commit()


def save_insight(conn, fetched_at, watch_name, depart_date, level):
    if not level:
        return
    conn.execute(
        "INSERT INTO insights (fetched_at, watch, depart_date, price_level) VALUES (?,?,?,?)",
        (fetched_at, watch_name, depart_date, level),
    )
    conn.commit()


def latest_levels(conn, watch_name: str) -> dict[str, str]:
    """最近一次抓取里，每个出发日对应的 Google 价格水平。"""
    row = conn.execute(
        "SELECT MAX(fetched_at) AS t FROM insights WHERE watch=?", (watch_name,)
    ).fetchone()
    if not row or not row["t"]:
        return {}
    rows = conn.execute(
        "SELECT depart_date, price_level FROM insights WHERE watch=? AND fetched_at=?",
        (watch_name, row["t"]),
    ).fetchall()
    return {r["depart_date"]: r["price_level"] for r in rows}


def log_fetch(conn, fetched_at, watch_name, depart_date, ok, n_offers=0, error=None):
    conn.execute(
        "INSERT INTO fetch_log (fetched_at, watch, depart_date, ok, n_offers, error)"
        " VALUES (?,?,?,?,?,?)",
        (fetched_at, watch_name, depart_date, int(ok), n_offers, error),
    )
    conn.commit()


def consecutive_failures(conn, watch_name: str) -> int:
    """最近连续失败了几次。用来判断监控是不是已经死了。"""
    rows = conn.execute(
        "SELECT ok FROM fetch_log WHERE watch=? ORDER BY id DESC LIMIT 50",
        (watch_name,),
    ).fetchall()
    n = 0
    for r in rows:
        if r["ok"]:
            break
        n += 1
    return n


def best_price_history(conn, watch_name: str) -> list[tuple[str, int]]:
    """每次抓取的最低价序列 [(fetched_at, price)]，跨该 watch 的所有日期取最小。"""
    rows = conn.execute(
        "SELECT fetched_at, MIN(price) AS p FROM offers WHERE watch=?"
        " GROUP BY fetched_at ORDER BY fetched_at",
        (watch_name,),
    ).fetchall()
    return [(r["fetched_at"], r["p"]) for r in rows]


def latest_offers(conn, watch_name: str, limit: int = 5) -> list[sqlite3.Row]:
    row = conn.execute(
        "SELECT MAX(fetched_at) AS t FROM offers WHERE watch=?", (watch_name,)
    ).fetchone()
    if not row or not row["t"]:
        return []
    return conn.execute(
        "SELECT * FROM offers WHERE watch=? AND fetched_at=? ORDER BY price LIMIT ?",
        (watch_name, row["t"], limit),
    ).fetchall()
