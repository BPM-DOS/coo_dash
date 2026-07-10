#!/usr/bin/env python3
"""
weekly_runner.py — Weekly metrics
===================================
Runs every Sunday night AFTER rippling_sync.py has written worked hours
to the Scorecard. Writes to COO Weekly Metrics (time-series, one row per metric per week).

Cron (Sunday 9pm, after rippling_sync at 8pm):
    0 21 * * 0 flock -n /tmp/coo_weekly.lock python /root/coo_dash/weekly_runner.py >> /var/log/coo_weekly.log 2>&1
"""

import os
import traceback
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv(override=True)

from airtable_writer import WeeklyWriter
from collectors import weekly, touchpoint

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
    print(f"COO Weekly ETL — {start.isoformat()}")
    print(f"{'='*50}")

    snapshots  = run_collector("weekly",     weekly.collect,              AIRTABLE_API_KEY)
    snapshots += run_collector("touchpoint", touchpoint.collect_weekly,   AIRTABLE_API_KEY)

    if snapshots:
        writer = WeeklyWriter(api_key=AIRTABLE_API_KEY)
        result = writer.upsert(snapshots)
        print(f"  Upserted: {result['upserted']} ({result['created']} created, {result['updated']} updated)")

    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    print(f"Done in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
