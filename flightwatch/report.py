"""分析与报告：把一堆历史价格变成"现在该不该买"的判断。

刻意不引入 LLM —— 分位数、最低价、趋势都是确定性计算，
每次跑必须给出完全一致的结果。模型的位置在这一层之外。
"""
from __future__ import annotations

import datetime as dt
import html

from . import store


def summarize(conn, watch) -> dict:
    hist = store.best_price_history(conn, watch.name)
    latest = store.latest_offers(conn, watch.name, limit=5)
    fails = store.consecutive_failures(conn, watch.name)

    out = {
        "watch": watch,
        "levels": store.latest_levels(conn, watch.name),
        "n_observations": len(hist),
        "consecutive_failures": fails,
        "latest_offers": latest,
        "current": None,
        "low": None,
        "high": None,
        "percentile": None,
        "delta_7d": None,
        "target_hit": False,
    }
    if not hist:
        return out

    prices = [p for _, p in hist]
    current = prices[-1]
    out["current"] = current
    out["low"] = min(prices)
    out["high"] = max(prices)

    # 当前价便宜过多少比例的历史观测。只有一次观测时无意义，留空。
    if len(prices) > 1:
        cheaper_than = sum(1 for p in prices if p > current)
        out["percentile"] = round(100 * cheaper_than / (len(prices) - 1))

    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)).isoformat()
    old = [p for t, p in hist if t < cutoff]
    if old:
        out["delta_7d"] = current - old[-1]

    if watch.target_price is not None:
        out["target_hit"] = current <= watch.target_price
    return out


_LEVEL_CN = {"low": "偏低", "typical": "正常", "high": "偏高"}


def google_verdict(s: dict) -> str | None:
    """Google 对各出发日的价格水平汇总成一句话。"""
    levels = s.get("levels") or {}
    if not levels:
        return None
    parts = [f"{d} {_LEVEL_CN.get(v, v)}" for d, v in sorted(levels.items())]
    return "Google 判断: " + " · ".join(parts)


def _verdict(s: dict) -> str:
    """一句话结论。措辞要诚实反映样本量 —— 3 次观测得不出"史低"。"""
    if s["current"] is None:
        return "无数据"
    if s["n_observations"] < 3:
        # 自有历史不够时，退回 Google 的判断，而不是干脆什么都不说
        base = f"仅 {s['n_observations']} 次观测，自有历史还不足以判断"
        if any(v == "low" for v in (s.get("levels") or {}).values()):
            return base + "，但 Google 认为当前价格偏低"
        return base + "，先参考下面 Google 的判断"
    if s["current"] <= s["low"]:
        return "当前是已观测到的最低价"
    pct = s["percentile"]
    if pct is None:
        return "样本不足"
    if pct >= 80:
        return f"偏低（便宜过 {pct}% 的历史观测）"
    if pct <= 20:
        return f"偏高（只便宜过 {pct}% 的历史观测）"
    return f"中等水平（便宜过 {pct}% 的历史观测）"


def render_text(summaries: list[dict]) -> str:
    lines = []
    for s in summaries:
        w = s["watch"]
        lines.append(f"\n{'=' * 64}")
        lines.append(f"{w.name}   {w.origin} → {w.dest}   [{', '.join(w.dates)}]")
        lines.append("=" * 64)

        if s["consecutive_failures"] >= 3:
            lines.append(
                f"  ⚠️  连续 {s['consecutive_failures']} 次抓取失败 —— 监控可能已失效，"
                f"请检查 fast-flights 是否需要升级"
            )
        if s["current"] is None:
            lines.append("  （暂无数据）")
            continue

        cur = f"{w.currency} {s['current']}"
        lines.append(f"  当前最低: {cur:<14} 历史区间: {s['low']} ~ {s['high']}"
                     f"   观测 {s['n_observations']} 次")

        if s["delta_7d"] is not None:
            d = s["delta_7d"]
            arrow = "↓" if d < 0 else ("↑" if d > 0 else "→")
            lines.append(f"  7 天变化: {arrow} {abs(d)}")

        gv = google_verdict(s)
        if gv:
            lines.append(f"  {gv}")
        lines.append(f"  结论: {_verdict(s)}")
        if w.target_price is not None:
            mark = "✅ 已达目标价" if s["target_hit"] else "未达目标价"
            lines.append(f"  目标 {w.currency} {w.target_price}: {mark}")

        lines.append("  最近一次抓取的前几档:")
        for o in s["latest_offers"]:
            lines.append(
                f"    {w.currency} {o['price']:<7} {o['depart_date']}  "
                f"{o['dep_time']}→{o['arr_time']}  {o['stops']} 转  "
                f"{o['airlines']:<22} {o['route']}"
            )
    return "\n".join(lines)


def render_html(summaries: list[dict]) -> str:
    """网页报告。这是用户唯一的界面，所以最重要的信息必须在第一屏。

    排序原则：现在多少钱 > 哪几班 > 历史。历史只留一行小字。
    """
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    parts = [
        "<!doctype html><html lang='zh'><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>机票监控</title>",
        """<style>
        body{font:15px/1.6 -apple-system,system-ui,"PingFang SC",sans-serif;
             max-width:820px;margin:0 auto;padding:24px 16px 64px;color:#16181d;
             background:#fafafa}
        h1{font-size:20px;margin:0 0 4px}
        .sub{color:#6b7280;font-size:13px;margin:0 0 28px}
        .card{background:#fff;border:1px solid #e8e8ec;border-radius:12px;
              padding:18px 20px;margin-bottom:18px}
        .route{font-size:17px;font-weight:600;margin:0}
        .dates{color:#6b7280;font-size:13px;margin:2px 0 14px}
        .price{font-size:32px;font-weight:700;letter-spacing:-.5px}
        .price .cur{font-size:16px;font-weight:500;color:#6b7280;margin-right:4px}
        .lvl{display:inline-block;font-size:12px;padding:2px 9px;border-radius:99px;
             margin-left:10px;vertical-align:6px}
        .lvl.low{background:#e7f6ec;color:#0a7a2f}
        .lvl.typical{background:#eef1f5;color:#4b5563}
        .lvl.high{background:#fdeceb;color:#b42318}
        .hit{background:#e7f6ec;color:#0a7a2f;font-weight:600;padding:9px 12px;
             border-radius:8px;margin:12px 0 0}
        table{border-collapse:collapse;width:100%;margin-top:14px;font-size:13.5px}
        th,td{text-align:left;padding:7px 8px;border-bottom:1px solid #f0f0f3}
        th{color:#6b7280;font-weight:500;font-size:12px}
        td.p{font-weight:600;white-space:nowrap}
        .hist{color:#9ca3af;font-size:12px;margin-top:12px}
        .warn{background:#fff4e5;color:#8a4b00;padding:11px 13px;border-radius:8px;
              margin:0 0 12px;font-size:13.5px}
        </style>""",
        "<h1>✈️ 机票监控</h1>",
        f"<p class='sub'>更新于 {now}</p>",
    ]

    for s in summaries:
        w = s["watch"]
        parts.append("<div class='card'>")
        parts.append(f"<p class='route'>{w.origin} → {w.dest}</p>")
        parts.append(f"<p class='dates'>{html.escape(' / '.join(w.dates))}</p>")

        if s["consecutive_failures"] >= 3:
            parts.append(
                f"<p class='warn'>⚠️ 连续 {s['consecutive_failures']} 次抓取失败，"
                f"监控可能已失效，价格未必是最新的</p>"
            )
        if s["current"] is None:
            parts.append("<p class='hist'>暂无数据</p></div>")
            continue

        # Google 对最便宜那天的判断，用色块直观标出
        levels = s.get("levels") or {}
        badge = ""
        if levels:
            best = min(levels.values(), key=lambda v: ["low", "typical", "high"].index(v)
                       if v in ("low", "typical", "high") else 9)
            cn = _LEVEL_CN.get(best, best)
            badge = f"<span class='lvl {best}'>Google：价格{cn}</span>"

        parts.append(
            f"<div class='price'><span class='cur'>{w.currency}</span>"
            f"{s['current']}{badge}</div>"
        )
        if w.target_price is not None and s["target_hit"]:
            parts.append(
                f"<p class='hit'>✅ 已跌到目标价 {w.currency} {w.target_price} 以下</p>"
            )

        parts.append("<table><tr><th>价格</th><th>出发</th><th>起降</th>"
                     "<th>中转</th><th>航司</th><th>经停</th></tr>")
        for o in s["latest_offers"]:
            stops = "直飞" if o["stops"] == 0 else f"{o['stops']} 转"
            parts.append(
                f"<tr><td class='p'>{w.currency} {o['price']}</td>"
                f"<td>{o['depart_date'][5:]}</td>"
                f"<td>{o['dep_time']}→{o['arr_time']}</td><td>{stops}</td>"
                f"<td>{html.escape(o['airlines'])}</td>"
                f"<td>{html.escape(o['route'])}</td></tr>"
            )
        parts.append("</table>")

        if s["n_observations"] > 1:
            parts.append(
                f"<p class='hist'>已盯 {s['n_observations']} 次 · "
                f"区间 {s['low']}–{s['high']}</p>"
            )
        parts.append("</div>")

    return "\n".join(parts)
