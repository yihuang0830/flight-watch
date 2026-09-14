"""配置加载与校验。配置错了要在抓取前就炸，不要跑一半才发现。"""
from __future__ import annotations

import dataclasses
import datetime as dt
from pathlib import Path

import yaml


@dataclasses.dataclass(frozen=True)
class Watch:
    name: str
    origin: str
    dest: str
    dates: tuple[str, ...]
    trip: str = "one-way"
    seat: str = "economy"
    currency: str = "USD"
    passengers: int = 1
    max_stops: int | None = None
    target_price: int | None = None
    keep_top: int = 10


def _require(d: dict, key: str, where: str):
    if key not in d:
        raise ValueError(f"{where}: 缺少必填字段 {key!r}")
    return d[key]


def load(path: str | Path) -> list[Watch]:
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    defaults = raw.get("defaults") or {}
    watches_raw = raw.get("watches") or []
    if not watches_raw:
        raise ValueError(f"{path}: watches 为空，没有任何要监控的航线")

    out: list[Watch] = []
    seen: set[str] = set()
    for i, w in enumerate(watches_raw):
        where = f"{path} watches[{i}]"
        name = _require(w, "name", where)
        if name in seen:
            raise ValueError(f"{where}: name {name!r} 重复，历史数据会混在一起")
        seen.add(name)

        dates = _require(w, "dates", where)
        if isinstance(dates, str):
            dates = [dates]
        for d in dates:
            try:
                dt.date.fromisoformat(str(d))
            except ValueError as e:
                raise ValueError(f"{where}: 日期 {d!r} 不是 YYYY-MM-DD") from e

        out.append(
            Watch(
                name=name,
                origin=str(_require(w, "from", where)).upper(),
                dest=str(_require(w, "to", where)).upper(),
                dates=tuple(str(d) for d in dates),
                trip=w.get("trip", defaults.get("trip", "one-way")),
                seat=w.get("seat", defaults.get("seat", "economy")),
                currency=w.get("currency", defaults.get("currency", "USD")),
                passengers=int(w.get("passengers", defaults.get("passengers", 1))),
                max_stops=w.get("max_stops", defaults.get("max_stops")),
                target_price=w.get("target_price", defaults.get("target_price")),
                keep_top=int(w.get("keep_top", defaults.get("keep_top", 10))),
            )
        )
    return out
