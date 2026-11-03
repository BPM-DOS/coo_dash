#!/usr/bin/env python3
"""
monthly_runner.py — Monthly metrics
=====================================
Runs on the 1st of each month. Writes to COO Monthly Metrics
(time-series, one row per metric per month).

Cron (1st of month, 6am):
    0 6 1 * * flock -n /tmp/coo_monthly.lock python /root/coo_dash/monthly_runner.py >> /var/log/coo_monthly.log 2>&1
"""

import os
import sys
import traceback
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv(override=True)

# Error emails to bpm-dos@ + dead man's switch heartbeat. Started before the
# other imports so even an import error is reported. See alerts.py.
import alerts
monitor = alerts.start(
    "COO Dash monthly metrics",
    heartbeat_env="COO_MONTHLY_HEARTBEAT_URL",
    state_dir=os.path.dirname(os.path.abspath(__file__)),
    runbook=(
        "Logs:  ssh root@104.131.181.174  then  tail -n 300 /var/log/coo_monthly.log\n"
        "Code:  BPM-DOS/coo_dash (/root/coo_dash on the VM) - monthly_runner.py runs collectors/monthly.py\n"
        "Alerting setup: see the docstring at the top of alerts.py"
    ),
)

from airtable_writer import MonthlyWriter
from collectors import monthly

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
        alerts.error(name, "collector failed - its metrics were not updated this run", exc=exc)
        return []


def main():
    start = datetime.now(timezone.utc)
    print(f"\n{'='*50}")
    print(f"COO Monthly ETL — {start.isoformat()}")
    print(f"{'='*50}")

    snapshots = run_collector("monthly", monthly.collect, AIRTABLE_API_KEY)

    if snapshots:
        writer = MonthlyWriter(api_key=AIRTABLE_API_KEY)
        result = writer.upsert(snapshots)
        print(f"  Upserted: {result['upserted']} ({result['created']} created, {result['updated']} updated)")

    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    print(f"Done in {elapsed:.1f}s")
    return monitor.finish()


if __name__ == "__main__":
    sys.exit(main())
