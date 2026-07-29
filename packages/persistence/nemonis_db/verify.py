"""``python -m nemonis_db.verify`` — audit chain integrity check.

Exits non-zero on a broken chain so it can gate a CI job or a cron check.
"""

from __future__ import annotations

import asyncio
import sys

from nemonis_db.audit import chain_head, verify_chain
from nemonis_db.session import dispose_engine, session_scope


async def _run() -> int:
    async with session_scope() as session:
        head, count = await chain_head(session)
        result = await verify_chain(session)

    if result.valid:
        print(f"Audit chain VALID — {result.events_checked} events, head {head[:16]}…")
        return 0

    print(f"Audit chain BROKEN after {result.events_checked} of {count} events", file=sys.stderr)
    print(f"  first bad event: {result.broken_at}", file=sys.stderr)
    print(f"  detail: {result.detail}", file=sys.stderr)
    return 1


def main() -> int:
    try:
        return asyncio.run(_run())
    finally:
        with __import__("contextlib").suppress(Exception):
            asyncio.run(dispose_engine())


if __name__ == "__main__":
    raise SystemExit(main())
