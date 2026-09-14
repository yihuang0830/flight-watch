"""微信推送（Server酱）。

免费额度只有 5 条/天，而我们有 20 条航线、每天跑 2 轮。
所以两条硬约束：
  1. 一轮只发一条消息，把所有达标航线聚合进去；
  2. 同样的结果不重复发 —— 否则两天就把额度耗光，真降价时反而发不出去。
"""
from __future__ import annotations

import datetime as dt
import os
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

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


def due_report_slot(conn, settings: dict, now: dt.datetime | None = None):
    """判断当前是否该发定时报告，返回 (本地日期, 时段) 或 None。

    用时区库按本地时间判断，而不是把时刻硬编码成 UTC cron ——
    这样夏令时切换完全不用管。每个时段一天只发一次；某轮若被 GitHub
    延迟或跳过，下一轮（30 分钟后）会自动补发。
    """
    times = settings.get("report_times") or []
    if not times:
        return None
    tz = ZoneInfo(settings.get("report_timezone") or "America/Chicago")
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(tz)
    today = now.date().isoformat()

    passed = []
    for t in times:
        h, m = (int(x) for x in t.split(":"))
        if (now.hour, now.minute) >= (h, m):
            passed.append(t)
    if not passed:
        return None

    latest = max(passed, key=lambda t: tuple(int(x) for x in t.split(":")))
    from . import store
    if store.report_already_sent(conn, today, latest):
        return None
    return today, latest


def pick_urgent(summaries: list[dict], urgent_price) -> list[dict]:
    """跌破紧急阈值的航线。这是要立刻打断你的那一类。"""
    if urgent_price is None:
        return []
    out = []
    for s in summaries:
        if s["current"] is not None and s["current"] <= urgent_price:
            out.append({"watch": s["watch"], "price": s["current"],
                        "prev": None, "offers": s["latest_offers"]})
    return out


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


def build_message(alerts: list[dict], summaries: list[dict]) -> tuple[str, str]:
    """达标航线给明细，其余航线给一览表。

    触发靠 alerts，但消息里附上全部 20 条航线 —— 捡漏时你需要看到全局
    才知道这个价到底值不值得下手，而不是只看到孤零零一条。
    """
    from .report import _LEVEL_CN, by_price

    ranked = [s for s in by_price(summaries) if s["current"] is not None]
    cur = ranked[0]["watch"].currency if ranked else "USD"
    n = len(alerts)

    if n:
        best = min(a["price"] for a in alerts)
        title = (f"🎯 {alerts[0]['watch'].origin}→{alerts[0]['watch'].dest} {cur} {best}"
                 if n == 1 else f"🎯 {n} 条航线跌破目标价，最低 {cur} {best}")
    else:
        # 心跳模式：没有航线达标也发，收不到消息就说明监控挂了
        low = ranked[0]
        title = (f"✈️ 最低 {cur} {low['current']} · "
                 f"{low['watch'].origin}→{low['watch'].dest}")

    lines = [f"## 🎯 跌破目标价的 {n} 条\n"] if n else []
    for a in sorted(alerts, key=lambda x: x["price"]):
        w = a["watch"]
        drop = f"（上次提醒 {a['prev']}）" if a["prev"] else ""
        lines.append(f"**{w.origin} → {w.dest} — {cur} {a['price']}**{drop}\n")
        lines.append("| 价格 | 出发 | 起降 | 中转 | 航司 | 经停 |")
        lines.append("|---|---|---|---|---|---|")
        for o in a["offers"][:3]:
            stops = "直飞" if o["stops"] == 0 else f"{o['stops']}转"
            lines.append(
                f"| {cur} {o['price']} | {o['depart_date'][5:]} | "
                f"{o['dep_time']}→{o['arr_time']} | {stops} | {o['airlines']} | {o['route']} |"
            )
        lines.append("")

    lines.append(f"\n## 📋 全部 {len(ranked)} 条航线\n")
    lines.append("| # | 航线 | 最低价 | 较上轮 | 最便宜日 | 经停 |")
    lines.append("|---|---|---|---|---|---|")
    for i, s in enumerate(ranked, 1):
        w = s["watch"]
        top = s["latest_offers"][0] if s["latest_offers"] else None
        lvl = (s.get("levels") or {}).get(top["depart_date"], "") if top else ""
        mark = " 🎯" if w.target_price and s["current"] <= w.target_price else ""
        d = s.get("delta_prev")
        chg = "—" if not d else (f"↓{abs(d)}" if d < 0 else f"↑{d}")
        lines.append(
            f"| {i} | {w.origin}→{w.dest}{mark} | **{cur} {s['current']}** | {chg} | "
            f"{top['depart_date'][5:] if top else '-'} | {top['route'] if top else '-'} |"
        )

    lines.append("\n> 价格随时会变，看到就尽快去 Google Flights 核实下单。")
    return title, "\n".join(lines)


def maybe_notify(conn, summaries: list[dict], settings: dict, *,
                 dry_run: bool = False) -> str:
    """决定本轮发什么。两条独立路径：

    1. **紧急**：任何航线跌破 urgent_price，立刻单独推一条（去重：同价不重发）。
       这是唯一允许打断你的情况。
    2. **定时**：到了配置的报告时段，推一份全量报告（兼作心跳）。

    其余时候静默 —— 每 30 分钟推一条会让人直接把通知关掉，
    那样真出好价时反而通知不到。
    """
    from . import store

    key = os.environ.get("SERVERCHAN_SEND_KEY", "").strip()
    if not any(s["current"] is not None for s in summaries):
        return "无数据可推送"

    # ---- 路径 1：紧急 ----
    urgent = [a for a in pick_urgent(summaries, settings.get("urgent_price"))
              if (last := store.last_notified_price(conn, a["watch"].name)) is None
              or a["price"] < last]
    for s in summaries:                      # 涨回阈值之上则清除记录，便于下次再报
        up = settings.get("urgent_price")
        if up is not None and s["current"] is not None and s["current"] > up:
            if store.last_notified_price(conn, s["watch"].name) is not None:
                store.clear_notification(conn, s["watch"].name)

    if urgent:
        title, desp = build_message(urgent, summaries)
        title = "🚨 " + title.lstrip("🎯✈️ ")
        if dry_run:
            return f"[试运行] 紧急推送: {title}"
        if not key:
            return f"{len(urgent)} 条跌破紧急阈值，但未配置 SERVERCHAN_SEND_KEY"
        ok, msg = send(key, title, desp)
        if ok:
            for a in urgent:
                store.record_notification(conn, a["watch"].name, a["price"])
            return f"已紧急推送: {title}"
        return f"紧急推送失败: {msg}"

    # ---- 路径 2：定时报告 ----
    due = due_report_slot(conn, settings)
    if not due:
        return "静默（无航线跌破紧急阈值，且未到报告时段）"

    slot_date, slot = due
    alerts = pick_alerts(conn, summaries)      # 810 以下的在报告里打 🎯
    title, desp = build_message(alerts, summaries)
    if dry_run:
        return f"[试运行] {slot} 定时报告: {title}"
    if not key:
        return f"到了 {slot} 报告时段，但未配置 SERVERCHAN_SEND_KEY"
    ok, msg = send(key, f"{title} · {slot}", desp)
    if ok:
        store.mark_report_sent(conn, slot_date, slot)
        return f"已推送 {slot} 定时报告: {title}"
    return f"定时报告推送失败: {msg}"
