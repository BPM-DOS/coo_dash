"""
collectors/utilization.py

Metrics:
  brm_utilization_pct  — SUM(Allocated Estimated Billable Hours from AppFolio) /
                         Rippling actual hours (from nightly Missive email)
                         Computed per tech, aggregated as total_estimated / total_rippling.

  wo_assigned_pct      — % of ALL active non-project melds that have been assigned.

brm_utilization_pct is skipped gracefully if the Rippling email has not yet arrived
(during daytime runs). When Runner.py calls this every 30 min, the metric only writes
once the 11:45 PM email shows up.
"""

from __future__ import annotations

import csv
import io
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

import requests
from pyairtable import Api

from airtable_writer import MetricSnapshot


# ── Airtable ───────────────────────────────────────────────────────────────────
AFP_BASE_ID = os.environ.get("AFP_BASE_ID", "appg8MZ0eQP6CFyfZ")

SC_BASE_ID  = "apppMJM2hsqCeNpNo"   # BPM Scorecard (Parabola-Less)
SC_TABLE_ID = "tblcDfpkYOF9MgKpA"   # Util_Rate_Daily

WORKLOGS_TABLE  = "Work Logs"
FIELD_TECH      = "fldDXhR9MBkHcHrLF"   # tech name (string)
FIELD_DATE      = "fldbkiufmk4CmzEjW"   # work date "YYYY-MM-DD"
FIELD_ALLOC_HRS = "fldaeEYn9RzdMLVQg"   # Allocated Estimated Billable Hours

# ── Missive ────────────────────────────────────────────────────────────────────
MISSIVE_BASE      = "https://public.missiveapp.com/v1"
TARGET_SUBJECT    = os.environ.get("RIPPLING_SUBJECT_CONTAINS", "BRM Utilization Rate")
MAX_AGE_HOURS     = 26   # only pick up the email from the last ~26 hours
MISSIVE_PAGE_LIMIT = 10

# ── Thresholds ─────────────────────────────────────────────────────────────────
BRM_TARGET_PCT = 85.0


# ── Public entry point ─────────────────────────────────────────────────────────

def collect(api_key: str) -> list[MetricSnapshot]:
    snapshots = []

    # brm_utilization_pct — only when Rippling email is present
    util = _brm_utilization(api_key)
    if util:
        snapshots.append(util)

    # wo_assigned_pct — always
    snapshots += _wo_assigned(api_key)

    return snapshots


# ── BRM Utilization ────────────────────────────────────────────────────────────

def _brm_utilization(api_key: str) -> MetricSnapshot | None:
    token = os.environ.get("MISSIVE_TOKEN", "")
    if not token:
        print("  [utilization] MISSIVE_TOKEN not set — skipping brm_utilization_pct")
        return None

    # 1. Find Rippling email in Missive
    convo = _find_rippling_conversation(token)
    if not convo:
        print("  [utilization] Rippling email not yet found — skipping brm_utilization_pct")
        return None

    # 2. Download CSV attachment
    try:
        raw_csv, delivered_ts = _fetch_csv_attachment(token, convo["id"])
    except Exception as e:
        print(f"  [utilization] Could not fetch CSV: {e}")
        return None

    # 3. Parse Rippling CSV → {tech: actual_hours}
    rippling_hours, pay_period = _parse_rippling_csv(raw_csv)
    if not rippling_hours:
        print("  [utilization] Rippling CSV is empty")
        return None

    # 4. Determine the work date from delivery timestamp
    work_date = datetime.fromtimestamp(delivered_ts, tz=ET).strftime("%Y-%m-%d")
    print(f"  [utilization] Rippling email found, work_date={work_date}")

    # 5. Query AppFolio Work Logs for that date → {tech: estimated_hours}
    try:
        appfolio_hours = _fetch_appfolio_hours(api_key, work_date)
    except Exception as e:
        print(f"  [utilization] Work Logs query failed: {e}")
        return None

    # 6. Compute per-tech and overall utilization
    total_estimated = 0.0
    total_rippling  = 0.0
    tech_lines      = []

    for tech, rippling_hrs in rippling_hours.items():
        estimated_hrs = appfolio_hours.get(tech, 0.0)
        pct = round(estimated_hrs / rippling_hrs * 100, 1) if rippling_hrs else 0
        tech_lines.append(
            f"{tech}: {estimated_hrs:.2f}h estimated / {rippling_hrs:.2f}h actual = {pct}%"
        )
        total_estimated += estimated_hrs
        total_rippling  += rippling_hrs

    if total_rippling == 0:
        print("  [utilization] Total Rippling hours = 0, cannot compute utilization")
        return None

    overall_pct = round(total_estimated / total_rippling * 100, 2)

    # 7. Persist per-tech rows to BPM Scorecard Util_Rate_Daily
    _write_to_scorecard(api_key, work_date, rippling_hours, appfolio_hours, pay_period)

    return MetricSnapshot(
        metric="brm_utilization_pct",
        category="Weekly KPI",
        source="Missive+AppFolio",
        value=overall_pct,
        secondary_value=round(total_estimated, 2),
        target=BRM_TARGET_PCT,
        value_text=(
            f"{overall_pct}% "
            f"({total_estimated:.1f}h estimated / {total_rippling:.1f}h actual) "
            f"— {pay_period}"
        ),
        detail="\n".join(tech_lines),
        status=_util_status(overall_pct),
    )


# ── Scorecard writer ──────────────────────────────────────────────────────────

def _write_to_scorecard(api_key: str, work_date: str,
                        rippling_hours: dict, appfolio_hours: dict,
                        pay_period: str) -> None:
    """Upsert one row per tech into Util_Rate_Daily in BPM Scorecard (Parabola-Less)."""
    try:
        sc_key = api_key
        table  = Api(sc_key).base(SC_BASE_ID).table(SC_TABLE_ID)

        existing = table.all(
            formula=f"FIND('{work_date}', {{ExternalID}}) > 0",
            fields=["ExternalID"],
        )
        existing_map = {
            rec["fields"]["ExternalID"]: rec["id"]
            for rec in existing
            if "ExternalID" in rec["fields"]
        }

        to_create, to_update = [], []
        for tech, rippling_hrs in rippling_hours.items():
            estimated_hrs = appfolio_hours.get(tech, 0.0)
            pct    = round(estimated_hrs / rippling_hrs * 100, 1) if rippling_hrs else 0.0
            ext_id = f"{work_date}|{tech}"
            fields = {
                "ExternalID":     ext_id,
                "Date":           work_date,
                "TechName":       tech,
                "EstimatedHours": round(estimated_hrs, 2),
                "RipplingHours":  round(rippling_hrs, 2),
                "UtilizationPct": pct,
                "PayPeriod":      pay_period,
            }
            if ext_id in existing_map:
                to_update.append({"id": existing_map[ext_id], "fields": fields})
            else:
                to_create.append(fields)

        if to_create:
            table.batch_create(to_create, typecast=True)
        if to_update:
            table.batch_update(to_update, typecast=True)

        print(f"  [utilization/scorecard] {len(to_create)} created, {len(to_update)} updated → Util_Rate_Daily")
    except Exception as e:
        print(f"  [utilization/scorecard] Write failed (non-fatal): {e}")


# ── WO Assigned Pct ────────────────────────────────────────────────────────────

def _wo_assigned(api_key: str) -> list[MetricSnapshot]:
    afp_key = os.environ.get("AFP_API_KEY") or api_key
    table = Api(afp_key).base(AFP_BASE_ID).table("Melds (Spine)")

    all_melds = table.all(
        fields=["AssignedAt", "ProjectID", "IsActive"],
        formula="AND({IsActive}, OR({ProjectID} = '', {ProjectID} = BLANK()))",
    )

    total_melds   = len(all_melds)
    assigned_melds = sum(
        1 for rec in all_melds
        if rec.get("fields", {}).get("AssignedAt") not in (None, "", "None")
    )

    if total_melds == 0:
        return []

    pct = round(assigned_melds / total_melds * 100, 2)
    return [MetricSnapshot(
        metric="wo_assigned_pct",
        category="Weekly KPI",
        source="Airtable",
        value=pct,
        secondary_value=float(total_melds),
        status=_assignment_status(pct),
    )]


# ── Missive helpers ────────────────────────────────────────────────────────────

def _missive_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _find_rippling_conversation(token: str) -> dict | None:
    h      = _missive_headers(token)
    cutoff = time.time() - MAX_AGE_HOURS * 3600
    params = {"all": "true", "limit": 50}

    for _ in range(MISSIVE_PAGE_LIMIT):
        resp = requests.get(f"{MISSIVE_BASE}/conversations", headers=h,
                            params=params, timeout=20)
        resp.raise_for_status()
        convos = resp.json().get("conversations", [])
        if not convos:
            break

        for c in convos:
            subj = (c.get("latest_message_subject") or "").strip()
            if TARGET_SUBJECT.lower() in subj.lower():
                return c

        last_ts = convos[-1].get("last_activity_at", 0)
        if last_ts < cutoff:
            break
        params["until"] = last_ts
        time.sleep(0.15)

    return None


def _fetch_csv_attachment(token: str, convo_id: str) -> tuple[str, int]:
    """Returns (raw_csv_text, delivered_at_timestamp)."""
    h    = _missive_headers(token)
    resp = requests.get(f"{MISSIVE_BASE}/conversations/{convo_id}/messages",
                        headers=h, timeout=20)
    resp.raise_for_status()

    for msg in resp.json().get("messages", []):
        if msg.get("from_field", {}).get("address") != "no-reply@rippling.com":
            continue
        for att in msg.get("attachments") or []:
            if att.get("extension") == "csv" or "utilization" in (att.get("filename") or "").lower():
                dl = requests.get(att["url"], timeout=20)
                dl.raise_for_status()
                return dl.text, msg.get("delivered_at", int(time.time()))

    raise RuntimeError(f"No CSV attachment found in conversation {convo_id}")


def _parse_rippling_csv(raw: str) -> tuple[dict[str, float], str]:
    reader = csv.DictReader(io.StringIO(raw))
    rows   = list(reader)
    if not rows:
        return {}, "unknown"
    tech_hours = {
        r["Employee"]: float(r["Time entry payable time (hours)"])
        for r in rows
        if r.get("Employee") and r.get("Time entry payable time (hours)")
    }
    return tech_hours, rows[0].get("Pay period", "unknown")


# ── AppFolio helpers ───────────────────────────────────────────────────────────

def _fetch_appfolio_hours(api_key: str, work_date: str) -> dict[str, float]:
    """Returns {tech_name: sum_allocated_estimated_billable_hours} for work_date."""
    afp_key = os.environ.get("AFP_API_KEY") or api_key
    table   = Api(afp_key).base(AFP_BASE_ID).table(WORKLOGS_TABLE)

    records = table.all(
        fields=[FIELD_TECH, FIELD_DATE, FIELD_ALLOC_HRS],
        formula=f"{{fldbkiufmk4CmzEjW}} = '{work_date}'",
    )

    totals: dict[str, float] = {}
    for rec in records:
        f    = rec["fields"]
        tech = f.get(FIELD_TECH)
        hrs  = f.get(FIELD_ALLOC_HRS)
        if not tech or hrs is None:
            continue
        totals[tech] = totals.get(tech, 0.0) + float(hrs)

    return totals


# ── Status helpers ─────────────────────────────────────────────────────────────

def _util_status(pct: float) -> str:
    if pct >= BRM_TARGET_PCT:        return "OK"
    if pct >= BRM_TARGET_PCT - 15:   return "Warning"
    return "Critical"


def _assignment_status(pct: float) -> str:
    if pct >= 85:   return "OK"
    if pct >= 70:   return "Warning"
    return "Critical"
