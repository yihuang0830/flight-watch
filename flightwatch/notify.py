"""微信推送（Server酱）。

免费额度只有 5 条/天，而我们有 20 条航线、每天跑 2 轮。
所以两条硬约束：
  1. 一轮只发一条消息，把所有达标航线聚合进去；
  2. 同样的结果不重复发 —— 否则两天就把额度耗光，真降价时反而发不出去。
"""
from __future__ import annotations

import os
import urllib.parse
import urllib.request

API = "https://sctapi.ftqq.com/{key}.send"


def send(send_key: str, title: str, desp: str, *, timeout: int = 15) -> tuple[bool, str]:
    """返回 (是否成功, 说明)。推送失败绝不能让抓取流程崩掉。"""
    data = urllib.parse.urlencode({"title": title[:100], "desp": desp}).encode()
    try:
        with urllib.request.urlopen(
            urllib.request.Request(API.format(key=send_key), data=data), timeout=timeout
        ) as r:
            body = r.read().decode("utf-8", "replace")
        if '"code":0' in body or '"code": 0' in body:
            return True, "已发送"
        return False, f"Server酱返回异常: {body[:200]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def pick_alerts(conn, summaries: list[dict]) -> list[dict]:
    """挑出需要提醒的航线。

    规则：价格 <= 目标价，且「比上次提醒时更便宜」或「之前从未提醒过」。
    一旦价格回到目标价之上，就清除记录 —— 这样它下次再跌破时能重新提醒，
    而不是因为"提醒过了"永远沉默。
    """
    from . import store

    alerts = []
    for s in summaries:
        w = s["watch"]
        price = s["current"]
        if w.target_price is None or price is None:
            continue

        last = store.last_notified_price(conn, w.name)
        if price > w.target_price:
            if last is not None:
                store.clear_notification(conn, w.name)   # 涨回去了，重置
            continue
        if last is None or price < last:
            alerts.append({"watch": w, "price": price, "prev": last,
                           "offers": s["latest_offers"]})
    return alerts


def build_message(alerts: list[dict]) -> tuple[str, str]:
    cur = alerts[0]["watch"].currency
    best = min(a["price"] for a in alerts)
    if len(alerts) == 1:
        a = alerts[0]
        title = f"✈️ {a['watch'].origin}→{a['watch'].dest} {cur} {a['price']}"
    else:
        title = f"✈️ {len(alerts)} 条航线跌破目标价，最低 {cur} {best}"

    lines = []
    for a in sorted(alerts, key=lambda x: x["price"]):
        w = a["watch"]
        drop = f"（上次提醒 {a['prev']}）" if a["prev"] else ""
        lines.append(f"### {w.origin} → {w.dest} — **{cur} {a['price']}**{drop}\n")
        lines.append("| 价格 | 出发 | 起降 | 中转 | 航司 | 经停 |")
        lines.append("|---|---|---|---|---|---|")
        for o in a["offers"][:3]:
            stops = "直飞" if o["stops"] == 0 else f"{o['stops']}转"
            lines.append(
                f"| {cur} {o['price']} | {o['depart_date'][5:]} | "
                f"{o['dep_time']}→{o['arr_time']} | {stops} | "
                f"{o['airlines']} | {o['route']} |"
            )
        lines.append("")
    lines.append("\n> 价格随时会变，看到就尽快去 Google Flights 核实下单。")
    return title, "\n".join(lines)


def maybe_notify(conn, summaries: list[dict], *, dry_run: bool = False) -> str:
    """返回一句人类可读的结果说明。"""
    from . import store

    key = os.environ.get("SERVERCHAN_SEND_KEY", "").strip()
    alerts = pick_alerts(conn, summaries)
    if not alerts:
        return "无需提醒（没有航线跌破目标价，或已提醒过且没更便宜）"

    title, desp = build_message(alerts)
    if dry_run:
        return f"[试运行] 本应推送: {title}\n\n{desp}"
    if not key:
        return f"有 {len(alerts)} 条航线达标，但未配置 SERVERCHAN_SEND_KEY，没能推送"

    ok, msg = send(key, title, desp)
    if ok:
        for a in alerts:
            store.record_notification(conn, a["watch"].name, a["price"])
        return f"已推送微信: {title}"
    return f"推送失败: {msg}"
