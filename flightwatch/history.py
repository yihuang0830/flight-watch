"""永久价格历史，按天分文件的 CSV。

为什么不直接把 SQLite 提交进 git：git 存的是整份快照，而 SQLite 每次写入
都会重排页面，导致整个文件在 git 眼里完全变了 —— 实测每 30 分钟提交一次
会让仓库一周涨到 4GB。

CSV 则是纯追加的文本，git 的增量压缩对它很有效。按天分文件之后，
昨天的文件写完就永远不再改动，git 只需存一次。

每轮只记录每条航线每个日期的**最低价**（40 行），而不是全部报价 ——
因为判断涨跌只需要对比上一次的最低价。明细留在 SQLite 里供报告使用，
那部分可以定期清理，但这里的历史永久保留。
"""
from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path

FIELDS = ["fetched_at", "watch", "origin", "dest", "depart_date",
          "currency", "min_price", "price_level"]


def day_file(root: str | Path, fetched_at: str) -> Path:
    """按抓取日期（UTC）分文件。写完即不再变动，对 git 友好。"""
    day = dt.datetime.fromisoformat(fetched_at).astimezone(dt.timezone.utc).date()
    return Path(root) / f"{day.isoformat()}.csv"


def append(root: str | Path, fetched_at: str, rows: list[dict]) -> Path:
    """追加本轮记录。文件不存在则先写表头。"""
    path = day_file(root, fetched_at)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})
    return path


def previous(root: str | Path, watch: str, depart_date: str,
             before: str) -> int | None:
    """某航线某出发日在 `before` 之前最近一次的最低价。

    只回溯最近两天的文件 —— 对比上一轮足够，不必扫全部历史。
    """
    root = Path(root)
    if not root.exists():
        return None
    day = dt.datetime.fromisoformat(before).astimezone(dt.timezone.utc).date()
    best: tuple[str, int] | None = None
    for d in (day, day - dt.timedelta(days=1)):
        p = root / f"{d.isoformat()}.csv"
        if not p.exists():
            continue
        with p.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if (row["watch"] == watch and row["depart_date"] == depart_date
                        and row["fetched_at"] < before):
                    if best is None or row["fetched_at"] > best[0]:
                        best = (row["fetched_at"], int(row["min_price"]))
    return best[1] if best else None


def previous_min(root: str | Path, watch: str, before: str) -> int | None:
    """某航线在 `before` 之前最近一轮的最低价（跨该航线的所有出发日）。

    权威来源是这份 CSV 而非 SQLite —— SQLite 会被定期清理，
    且在 CI 里靠缓存传递，缓存被回收就没了。CSV 在 git 里，永远都在。
    """
    root = Path(root)
    if not root.exists():
        return None
    day = dt.datetime.fromisoformat(before).astimezone(dt.timezone.utc).date()
    rounds: dict[str, int] = {}
    for d in (day - dt.timedelta(days=1), day):
        p = root / f"{d.isoformat()}.csv"
        if not p.exists():
            continue
        with p.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["watch"] != watch or row["fetched_at"] >= before:
                    continue
                t, v = row["fetched_at"], int(row["min_price"])
                rounds[t] = min(rounds.get(t, v), v)
    if not rounds:
        return None
    return rounds[max(rounds)]
