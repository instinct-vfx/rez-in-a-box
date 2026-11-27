# SPDX-License-Identifier: Apache-2.0
"""Fill the tracking database with demo events.

Runs inside the rez-ctx-tracking-consumer image (compose service ``seed-demo``,
started by ``rezbox seed-demo``). The database is given by PG_DSN.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from psycopg_pool import AsyncConnectionPool

from demo import RealDataError, delete_demo_data, seed
from rez_ctx_consumer.logging_config import configure_logging


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rezbox seed-demo",
        description="Replace the demo data in the tracking database with made-up events. "
        "Demo rows have routing keys starting with DEMO.",
    )
    p.add_argument("--days", type=int, default=30, help="days of history (default: 30)")
    p.add_argument("--users", type=int, default=30, help="number of artists (default: 30)")
    p.add_argument("--seed", type=int, default=1, help="random seed (default: 1)")
    p.add_argument("--force", action="store_true",
                   help="add demo data even if there are real events")
    p.add_argument("--delete", action="store_true", help="only delete the demo data")
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
    except SystemExit as exc:  # --help or a usage error
        return exc.code if isinstance(exc.code, int) else 2
    dsn = os.environ.get("PG_DSN")
    if not dsn:
        print("PG_DSN must be set", file=sys.stderr)
        return 2
    configure_logging("INFO", "text")

    async def go() -> None:
        async with AsyncConnectionPool(dsn, min_size=1, max_size=2) as pool:
            if args.delete:
                async with pool.connection() as conn:
                    print(f"deleted {await delete_demo_data(conn)} demo events")
                return
            stored = await seed(
                pool, days=args.days, users=args.users, seed=args.seed, force=args.force
            )
            print(f"stored {stored} demo events covering the last {args.days} days")

    try:
        asyncio.run(go())
    except (RealDataError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
