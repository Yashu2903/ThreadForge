"""Spend report from the usage ledger. Money is estimated from configured prices; no content.

    python -m app.commands.ai_spend_report [--days 30] [--by day|user|room|operation|key_source]

Needs only DATABASE_URL.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import text

from app.config import ConfigError, load_database_url
from app.db import create_engine_from_url, create_sessionmaker

_BY = {
    "day": "to_char((l.created_at AT TIME ZONE 'UTC')::date, 'YYYY-MM-DD')",
    "user": "COALESCE(u.display_name, '(system)')",
    "room": "COALESCE(l.room_id::text, '(none)')",
    "operation": "l.operation",
    "key_source": "l.key_source",
}


async def report(days: int, by: str) -> list[tuple[str, int, int, int, int]]:
    sql = text(
        f"SELECT {_BY[by]} AS label, count(*) AS calls, "  # noqa: S608 - _BY is a constant dict
        "COALESCE(sum(l.input_tokens), 0) AS tin, COALESCE(sum(l.output_tokens), 0) AS tout, "
        "COALESCE(sum(l.est_cost_micros), 0) AS cost "
        "FROM ai_usage_ledger l LEFT JOIN users u ON u.id = l.charged_user_id "
        "WHERE l.created_at > now() - make_interval(days => :d) GROUP BY 1 ORDER BY 5 DESC, 1"
    )
    engine = create_engine_from_url(load_database_url())
    try:
        async with create_sessionmaker(engine)() as db:
            rows = (await db.execute(sql, {"d": days})).all()
    finally:
        await engine.dispose()
    return [(r.label, r.calls, int(r.tin), int(r.tout), int(r.cost)) for r in rows]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--by", choices=sorted(_BY), default="day")
    args = parser.parse_args(argv)
    try:
        rows = asyncio.run(report(args.days, args.by))
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"{args.by:38} {'calls':>7} {'in tok':>10} {'out tok':>10} {'est USD':>10}")
    for label, calls, tin, tout, cost in rows:
        print(f"{label:38} {calls:7} {tin:10} {tout:10} {cost / 1e6:10.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
