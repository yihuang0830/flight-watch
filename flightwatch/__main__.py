from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config, fetch, notify, report, store


def cmd_watch(args) -> int:
    watches = config.load(args.config)
    conn = store.connect(args.db)
    fetched_at = store.utcnow()

    jobs = [(w, d) for w in watches for d in w.dates]
    est = max(1, round(len(jobs) * (args.delay + 4) / 60))
    print(f"本轮 {len(watches)} 条航线 / {len(jobs)} 次查询，预计 {est} 分钟\n")

    n_ok = n_fail = 0
    for i, (w, d) in enumerate(jobs):
        if i:
            fetch.polite_sleep(args.delay)   # 限速，避免被 Google 拦
        tag = f"[{i + 1}/{len(jobs)}]"
        try:
            res = fetch.fetch_one(w, d, proxy=args.proxy)
        except fetch.FetchError as e:
            n_fail += 1
            store.log_fetch(conn, fetched_at, w.name, d, ok=False, error=str(e))
            print(f"  {tag} ✗ {w.name} {d}: {e}", file=sys.stderr)
            continue

        n_ok += 1
        store.save_offers(conn, fetched_at, w, d, res.offers)
        store.save_insight(conn, fetched_at, w.name, d, res.price_level)
        store.log_fetch(conn, fetched_at, w.name, d, ok=True, n_offers=len(res.offers))
        lvl = f"  [{res.price_level}]" if res.price_level else ""
        print(f"  {tag} ✓ {w.name} {d}: 最低 {w.currency} {res.offers[0]['price']}{lvl}")

    print(f"\n完成: {n_ok} 成功 / {n_fail} 失败  →  {args.db}")

    if not args.no_notify:
        summaries = [report.summarize(conn, w) for w in watches]
        print(notify.maybe_notify(conn, summaries, dry_run=args.dry_run))

    # 全军覆没时用退出码报警，便于 cron / CI 感知
    if n_ok == 0 and n_fail > 0:
        print("所有抓取均失败 —— 数据源可能已失效", file=sys.stderr)
        return 1
    return 0


def cmd_notify(args) -> int:
    watches = config.load(args.config)
    conn = store.connect(args.db)
    summaries = [report.summarize(conn, w) for w in watches]
    print(notify.maybe_notify(conn, summaries, dry_run=args.dry_run))
    return 0


def cmd_report(args) -> int:
    watches = config.load(args.config)
    conn = store.connect(args.db)
    summaries = [report.summarize(conn, w) for w in watches]
    print(report.render_text(summaries))
    if args.html:
        Path(args.html).write_text(report.render_html(summaries), encoding="utf-8")
        print(f"\nHTML 报告 → {args.html}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="flightwatch", description="国际机票价格监控")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--db", default="flights.db")

    # 同样的选项挂到子命令上，让 `watch --config x` 和 `--config x watch` 都能用。
    # SUPPRESS 保证子命令未显式传参时不会用 None 覆盖掉顶层的值。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS)
    common.add_argument("--db", default=argparse.SUPPRESS)

    sub = p.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("watch", parents=[common], help="抓取一轮并入库")
    w.add_argument("--proxy", default=None)
    w.add_argument("--delay", type=float, default=fetch.DEFAULT_DELAY,
                   help="每次查询之间的间隔秒数，默认 3")
    w.add_argument("--no-notify", action="store_true", help="本轮不推送")
    w.add_argument("--dry-run", action="store_true",
                   help="只打印将要推送的内容，不真发、不记账")
    w.set_defaults(func=cmd_watch)

    n = sub.add_parser("notify", parents=[common], help="基于已有数据检查并推送")
    n.add_argument("--dry-run", action="store_true")
    n.set_defaults(func=cmd_notify)

    r = sub.add_parser("report", parents=[common], help="基于历史数据生成报告")
    r.add_argument("--html", default=None, help="同时写出 HTML 报告到该路径")
    r.set_defaults(func=cmd_report)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
