"""Admin CLI. Inside the container:  docker compose exec tax-assistant python -m tax_assistant.cli <cmd>

Commands:
    report [--year YYYY]   print per-category totals and write report-YYYY.csv
    rebuild-db             rebuild the SQLite ledger from the JSON sidecars
    poll-once              run a single mail poll cycle (no healthcheck ping)
    set-balance USD        sync the real credit balance from the Anthropic Console
    costs                  show API spend (lifetime + since last balance sync)
    test-push              send a test Pushover notification
    test-healthcheck       send a success ping to healthchecks.io
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

from . import config, costs, poller, report, storage
from .notify import hc_ping, pushover


def cmd_report(cfg, args) -> int:
    year = args.year or str(datetime.now(timezone.utc).year)
    totals = report.category_totals(cfg.data_dir, year)
    if not totals:
        print(f"No receipts for {year}.")
        return 0
    width = max(len(t["category"]) for t in totals)
    print(f"\nSchedule C totals for {year}\n" + "-" * (width + 24))
    for t in totals:
        line = t["line"].split("-")[0]
        print(f"  {line:>4}  {t['category']:<{width}}  {t['count']:>4}  ${t['total']:>10.2f}")
    print("-" * (width + 24))
    print(f"        {'TOTAL':<{width}}        ${report.year_total(cfg.data_dir, year):>10.2f}\n")
    path = report.write_csv(cfg.data_dir, year)
    print(f"CSV written to {path}")
    return 0


def cmd_rebuild_db(cfg, args) -> int:
    n = storage.rebuild_db(cfg.data_dir)
    print(f"Rebuilt ledger from {n} sidecar(s).")
    return 0


def cmd_poll_once(cfg, args) -> int:
    problems = config.validate_for_polling(cfg)
    if problems:
        print("Cannot poll:", "; ".join(problems), file=sys.stderr)
        return 1
    print(poller.poll_once(cfg))
    return 0


def cmd_set_balance(cfg, args) -> int:
    if args.usd < 0:
        print("Balance can't be negative.", file=sys.stderr)
        return 1
    costs.sync_balance(cfg, args.usd)
    print(f"Balance synced: ${args.usd:.2f} as of now. "
          "Future notifications count spend from this point.")
    return 0


def cmd_costs(cfg, args) -> int:
    s = costs.summary(cfg)
    print(f"API calls recorded:   {s['calls']}")
    print(f"Lifetime spend:       ${s['lifetime_usd']:.4f}")
    if s["anchor_usd"] is None:
        print("No balance anchor set — run `set-balance <usd>` with the figure "
              "from the Anthropic Console to enable the remaining estimate.")
    else:
        print(f"Balance anchor:       ${s['anchor_usd']:.2f} (as of {s['anchor_as_of'] or 'the beginning'})")
        print(f"Spent since anchor:   ${s['spent_since_anchor_usd']:.4f}")
        print(f"Remaining (estimate): ${s['remaining_usd']:.2f}")
    return 0


def cmd_test_push(cfg, args) -> int:
    ok = pushover(cfg, "Tax assistant test", "If you can read this, Pushover is working. 🎉")
    print("Pushover notification sent." if ok else "Pushover FAILED — check keys/logs.")
    return 0 if ok else 1


def cmd_test_healthcheck(cfg, args) -> int:
    if not cfg.healthchecks_url:
        print("HEALTHCHECKS_URL is not set.", file=sys.stderr)
        return 1
    hc_ping(cfg, ok=True, message="manual test ping")
    print(f"Pinged {cfg.healthchecks_url}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level="INFO", format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="tax_assistant")
    sub = parser.add_subparsers(dest="command", required=True)

    p_report = sub.add_parser("report", help="per-category totals + CSV export")
    p_report.add_argument("--year", help="tax year (default: current)")
    p_report.set_defaults(func=cmd_report)

    sub.add_parser("rebuild-db", help="rebuild ledger.db from sidecars").set_defaults(func=cmd_rebuild_db)
    sub.add_parser("poll-once", help="run one mail poll cycle").set_defaults(func=cmd_poll_once)

    p_balance = sub.add_parser("set-balance",
                               help="sync the real credit balance from the Anthropic Console")
    p_balance.add_argument("usd", type=float, help="balance in USD, e.g. 23.45")
    p_balance.set_defaults(func=cmd_set_balance)

    sub.add_parser("costs", help="show API spend and balance estimate").set_defaults(func=cmd_costs)
    sub.add_parser("test-push", help="send a test Pushover notification").set_defaults(func=cmd_test_push)
    sub.add_parser("test-healthcheck", help="ping healthchecks.io").set_defaults(func=cmd_test_healthcheck)

    args = parser.parse_args(argv)
    cfg = config.load()
    return args.func(cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
