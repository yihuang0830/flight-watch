"""抓取层：把 fast-flights 的返回归一化成一行行可入库的报价。

这一层是整个系统最脆弱的地方 —— Google 改版会让解析静默失败。
所以：失败要显式抛出并记账，绝不能返回空列表假装一切正常。
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import re
import time

from fast_flights import FlightQuery, Passengers, create_query, fetch_flights_html
from fast_flights.parser import LexborHTMLParser, parse, parse_js


class FetchError(RuntimeError):
    pass


# 请求间隔。20 条航线 × 多个日期意味着每轮几十个请求，
# 不限速很容易被 Google 限流，届时整轮数据全废。
DEFAULT_DELAY = 3.0


def polite_sleep(delay: float = DEFAULT_DELAY) -> None:
    """请求之间的固定间隔，加一点抖动避免规律性太强。"""
    import random
    time.sleep(delay + random.uniform(0, 1.5))


@dataclasses.dataclass
class FetchResult:
    offers: list[dict]
    price_level: str | None   # low / typical / high，来自 Google 自己的历史判断


# Google 页面上的「Prices are currently typical」。不锚定 class 名 —— 那串混淆
# class（gOatQ T9AX1b）随时会变；文案结构稳定得多。代价是必须锁定英文界面。
_LEVEL_RE = re.compile(r"Prices are currently\s*<[^>]*>([^<]{1,20})</", re.I)
_LEVELS = {"low", "typical", "high"}


def _has_price(entry) -> bool:
    """上游 parse_js 里 `price = k[1][0][1]`，个别行程缺这段结构。"""
    try:
        entry[1][0][1]
        return True
    except (IndexError, TypeError, KeyError):
        return False


def parse_tolerant(html: str):
    """容错解析。

    上游 parse_js 读价格时不做保护，**一条没有价格的行程会让整页结果全部丢失**
    （实测 40 次查询里有 3 次因此归零）。这里先把坏条目从原始 payload 里剔掉，
    再把清洗后的数据交回上游解析 —— 解析逻辑仍然是上游的，我们只负责过滤，
    避免把那 60 行结构解析抄过来后和上游版本各走各路。
    """
    script = LexborHTMLParser(html).css_first(r"script.ds\:1")
    if script is None:
        raise FetchError("页面结构变化：找不到数据脚本")

    js = script.text()
    payload = json.loads(js.split("data:", 1)[1].rsplit(",", 1)[0])

    entries = payload[3][0] or []
    good = [k for k in entries if _has_price(k)]
    if not good:
        raise FetchError("该航线所有行程条目都缺少价格")
    payload[3][0] = good

    # 重新拼成 parse_js 期望的 `data:<json>,` 形式
    return parse_js("data:" + json.dumps(payload) + ",")


def _price_level(html: str) -> str | None:
    m = _LEVEL_RE.search(html)
    if not m:
        return None
    v = m.group(1).strip().lower()
    return v if v in _LEVELS else None


def _hhmm(sdt) -> str | None:
    """SimpleDatetime.time 是 (时, 分) 元组。"""
    t = getattr(sdt, "time", None)
    if not t:
        return None
    return f"{int(t[0]):02d}:{int(t[1]):02d}"


def _date(sdt):
    """SimpleDatetime.date 是 (年, 月, 日) 元组。"""
    d = getattr(sdt, "date", None)
    if not d:
        return None
    return dt.date(int(d[0]), int(d[1]), int(d[2]))


def _arrival_label(first_seg, last_seg) -> str | None:
    """到达时刻。跨天必须标出 +Nd，否则 16:36→16:50 会被读成 14 分钟飞到上海。"""
    hhmm = _hhmm(last_seg.arrival)
    if hhmm is None:
        return None
    d0, d1 = _date(first_seg.departure), _date(last_seg.arrival)
    if d0 and d1 and (delta := (d1 - d0).days):
        return f"{hhmm}+{delta}d"
    return hhmm


def _normalize(result) -> list[dict]:
    offers: list[dict] = []
    for f in result:
        segs = list(f.flights or [])
        if not segs:
            continue  # 没有航段信息的条目无法核对，丢弃
        codes = [segs[0].from_airport.code] + [s.to_airport.code for s in segs]
        offers.append(
            {
                "price": int(f.price),
                "airlines": list(f.airlines or []),
                "stops": len(segs) - 1,
                # 各航段飞行时间之和；不含中转等待（跨时区无法从本地时间可靠推算）
                "duration_min": sum(int(s.duration or 0) for s in segs) or None,
                "dep_time": _hhmm(segs[0].departure),
                "arr_time": _arrival_label(segs[0], segs[-1]),
                "route": ">".join(codes),
            }
        )
    offers.sort(key=lambda o: o["price"])

    # Google 会把同一个行程同时放进 "best" 和 "cheapest" 分组，原样入库会重复计数
    deduped, seen = [], set()
    for o in offers:
        key = (o["price"], o["route"], o["dep_time"], o["arr_time"], tuple(o["airlines"]))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(o)
    return deduped


def fetch_one(watch, depart_date: str, *, retries: int = 2, proxy: str | None = None) -> FetchResult:
    """抓一条航线的一个日期。失败抛 FetchError，由调用方记账。"""
    q = create_query(
        flights=[
            FlightQuery(
                date=depart_date,
                from_airport=watch.origin,
                to_airport=watch.dest,
                max_stops=watch.max_stops,
            )
        ],
        seat=watch.seat,
        trip=watch.trip,
        passengers=Passengers(adults=watch.passengers),
        currency=watch.currency,  # 必须钉死，否则价格单位会随地区推断漂移
        language="en-US",        # 价格水平靠英文文案定位，语言不能浮动
    )

    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            html = fetch_flights_html(q, proxy=proxy)   # 一次请求，两份数据
            try:
                result = parse(html)
            except (IndexError, TypeError):
                # 上游解析被个别坏条目噎住，剔掉它们重来
                result = parse_tolerant(html)
        except Exception as e:  # 网络错误、解析错误、被限流都可能在这里
            last_err = e
            if attempt < retries:
                time.sleep(2 ** attempt * 2)
                continue
            raise FetchError(f"{watch.name} {depart_date}: {type(e).__name__}: {e}") from e

        offers = _normalize(result)
        if not offers:
            # 拿到响应但一条都解析不出来 —— 这通常意味着上游改版了，不是"真的没航班"
            last_err = FetchError(
                f"{watch.name} {depart_date}: 返回 {len(result)} 条但归一化后为空，疑似上游格式变更"
            )
            if attempt < retries:
                time.sleep(2 ** attempt * 2)
                continue
            raise last_err
        return FetchResult(offers=offers[: watch.keep_top], price_level=_price_level(html))

    raise FetchError(str(last_err))
