#!/usr/bin/env python3
"""
Runner.py — Live metrics
========================
Runs every 30 minutes via cron. Overwrites live metric rows in COO Live Metrics.

Cron:
    */30 * * * * flock -n /tmp/coo_live.lock python /root/coo_dash/Runner.py >> /var/log/coo_live.log 2>&1
"""

import os
import traceback
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv(override=True)

from airtable_writer import LiveWriter
from collectors import live, touchpoint, utilization

AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY")
if not AIRTABLE_API_KEY:
    raise RuntimeError("Missing AIRTABLE_API_KEY")


def run_collector(name, fn, *args):
    try:
        results = fn(*args)
        print(f"  [{name}] {len(results)} snapshot(s)")
        return results
    except Exception as exc:
        print(f"  [{name}] ERROR: {exc}")
        traceback.print_exc()
        return []


def main():
    start = datetime.now(timezone.utc)
    print(f"\n{'='*50}")
    print(f"COO Live ETL — {start.isoformat()}")
    print(f"{'='*50}")

    snapshots  = run_collector("live",        live.collect,             AIRTABLE_API_KEY)
    snapshots += run_collector("touchpoint", touchpoint.collect_live,  AIRTABLE_API_KEY)
    snapshots += run_collector("utilization", utilization.collect,     AIRTABLE_API_KEY)

    if snapshots:
        writer = LiveWriter(api_key=AIRTABLE_API_KEY)
        result = writer.upsert(snapshots)
        print(f"  Upserted: {result['upserted']} ({result['created']} created, {result['updated']} updated)")

    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    print(f"Done in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
