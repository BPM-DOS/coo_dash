"""
backfill_util_rate.py

Backfills Util_Rate_Daily in BPM Scorecard (Parabola-Less) for a date range
using AppFolio Work Logs only (no Rippling email needed).

RipplingHours will be 0 and UtilizationPct will be 0 for backfilled rows
UNLESS you also have a Rippling CSV — but this at least populates EstimatedHours
so the scorecard has AppFolio data for every day.

Usage:
    python backfill_util_rate.py 2025-07-01 2025-08-24
"""

import os
import sys
from datetime import date, timedelta
from pyairtable import Api
from dotenv import load_dotenv

load_dotenv()

AFP_BASE_ID = os.environ.get("AFP_BASE_ID", "appg8MZ0eQP6CFyfZ")
SC_BASE_ID  = "apppMJM2hsqCeNpNo"   # BPM Scorecard (Parabola-Less)
SC_TABLE_ID = "tblcDfpkYOF9MgKpA"   # Util_Rate_Daily

FIELD_TECH      = "fldDXhR9MBkHcHrLF"
FIELD_DATE      = "fldbkiufmk4CmzEjW"
FIELD_ALLOC_HRS = "fldaeEYn9RzdMLVQg"

api_key = os.environ.get("AIRTABLE_API_KEY", "")
if not api_key:
    print("AIRTABLE_API_KEY not set"); sys.exit(1)

afp_key = os.environ.get("AFP_API_KEY") or api_key

start_date = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date(2025, 7, 1)
end_date   = date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else date.today() - timedelta(days=1)

print(f"Backfilling {start_date} → {end_date}\n")

sc_table  = Api(api_key).base(SC_BASE_ID).table(SC_TABLE_ID)
afp_table = Api(afp_key).base(AFP_BASE_ID).table("Work Logs")

# Fetch all existing ExternalIDs in range to avoid duplicates
print("Fetching existing scorecard rows...")
existing = sc_table.all(
    formula=f"AND({{Date}} >= '{start_date}', {{Date}} <= '{end_date}')",
    fields=["ExternalID"],
)
existing_ids = {
    rec["fields"]["ExternalID"]: rec["id"]
    for rec in existing
    if "ExternalID" in rec["fields"]
}
print(f"  {len(existing_ids)} existing rows found\n")

current = start_date
while current <= end_date:
    work_date = current.isoformat()

    # Query AppFolio Work Logs for this date
    records = afp_table.all(
        fields=[FIELD_TECH, FIELD_DATE, FIELD_ALLOC_HRS],
        formula=f"{{{FIELD_DATE}}} = '{work_date}'",
        use_field_ids=True,
    )

    if not records:
        print(f"  {work_date}: no Work Log entries, skipping")
        current += timedelta(days=1)
        continue

    # Sum hours per tech
    tech_hours: dict[str, float] = {}
    for rec in records:
        f    = rec["fields"]
        tech = f.get(FIELD_TECH)
        hrs  = f.get(FIELD_ALLOC_HRS)
        if tech and hrs is not None:
            tech_hours[tech] = tech_hours.get(tech, 0.0) + float(hrs)

    to_create, to_update = [], []
    for tech, estimated_hrs in tech_hours.items():
        ext_id = f"{work_date}|{tech}"
        fields = {
            "ExternalID":     ext_id,
            "Date":           work_date,
            "TechName":       tech,
            "EstimatedHours": round(estimated_hrs, 2),
            "RipplingHours":  0.0,   # not available for backfill
            "UtilizationPct": 0.0,   # can't compute without Rippling
            "PayPeriod":      "backfill",
        }
        if ext_id in existing_ids:
            to_update.append({"id": existing_ids[ext_id], "fields": fields})
        else:
            to_create.append(fields)

    if to_create:
        sc_table.batch_create(to_create, typecast=True)
    if to_update:
        sc_table.batch_update(to_update, typecast=True)

    print(f"  {work_date}: {len(tech_hours)} techs — "
          f"{len(to_create)} created, {len(to_update)} updated")

    current += timedelta(days=1)

print("\nDone.")
