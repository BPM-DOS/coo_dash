"""
collectors/live.py

All live metrics — refreshed every 30 minutes.
Pulls from: Melds (Spine), Tasks, Missive.

  Maintenance Stalls:
    - wo_not_triaged_24h    : Active melds still PENDING_ASSIGNMENT > 24h
    - stalled_wo_72h        : Active melds assigned but not completed > 72h
    - time_to_triage_hours  : Rolling 30d avg hours from creation → assignment

  Execution:
    - past_due_tasks        : Open tasks with Target Completion Date in the past

  Communication:
    - inboxes_over_50       : Team inboxes with estimated open conversations > 50
    - oldest_message_age_hours : Age of oldest open Missive conversation
"""

from __future__ import annotations

import os
import time
from collections import Counter
from datetime import datetime, timezone, timedelta

import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from pyairtable import Api

from airtable_writer import MetricSnapshot, _today


AFP_BASE_ID = os.environ.get("AFP_BASE_ID", "appg8MZ0eQP6CFyfZ")
BPM_BASE_ID = os.environ.get("BPM_BASE_ID", "apprp203tCiyHl6Dw")
MISSIVE_TOKEN = os.environ.get("MISSIVE_TOKEN", "")
MISSIVE_BASE = "https://public.missiveapp.com/v1"
INBOX_THRESHOLD = int(os.environ.get("MISSIVE_INBOX_THRESHOLD", "50"))

UNTRIAGED_STATUS = "PENDING_ASSIGNMENT"
CLOSED_STATUSES = {"COMPLETED", "MANAGER_CANCELED", "TENANT_CANCELED", "CANCELLED", "CANCELED"}
DETAIL_CAP = 50

TEAM_INBOXES = {
    "9a7adab1-5e02-4ca7-9d60-306fc274d186": "Finance",
    "4e3e2112-7a52-4cba-a77d-d25142def86d": "Maintenance",
    "6c4c0b77-67e7-443f-be30-c66bb6e87e8e": "Marketing",
    "fb007418-7e6a-4db5-bf41-b1f8a3fddeeb": "Office",
    "27036b84-8fc4-480b-9334-94195631fd5b": "Property Management",
    "891298e2-2a54-47d8-ba83-ff007a8f751b": "Rentals/Leasing",
    "01ccca67-ec22-4627-ba23-a567fac25a98": "Sales",
}


def collect(api_key: str) -> list[MetricSnapshot]:
    snapshots = []
    snapshots += _maintenance(api_key)
    snapshots += _execution(api_key)
    snapshots += _missive()
    return snapshots


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------

def _maintenance(api_key: str) -> list[MetricSnapshot]:
    afp_key = os.environ.get("AFP_API_KEY") or api_key
    table = Api(afp_key).base(AFP_BASE_ID).table("Melds (Spine)")

    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)
    cutoff_72h = now - timedelta(hours=72)
    cutoff_30d = now - timedelta(days=30)

    records = table.all(
        fields=["Status", "CreatedAt", "AssignedAt", "UpdatedAt",
                "IsActive", "ReferenceID", "BriefDescription", "Origin"],
        formula="{IsActive}",
    )

    not_triaged_refs, stalled_refs, triage_durations = [], [], []

    for rec in records:
        f = rec.get("fields", {})
        status     = (f.get("Status") or "").upper()
        created_at = _parse_dt(f.get("CreatedAt"))
        assigned_at = _parse_dt(f.get("AssignedAt"))
        ref_id     = f.get("ReferenceID") or ""
        brief      = f.get("BriefDescription") or ""
        label      = f"{ref_id} — {brief}" if ref_id and brief else (ref_id or brief or rec["id"])

        if status in CLOSED_STATUSES:
            continue

        if status == UNTRIAGED_STATUS and created_at and created_at < cutoff_24h:
            not_triaged_refs.append(label)

        if assigned_at and assigned_at < cutoff_72h:
            stalled_refs.append(label)

        origin = (f.get("Origin") or "").upper()
        is_resident = origin in ("TENANT", "RESIDENT", "")
        if is_resident and created_at and assigned_at and created_at >= cutoff_30d:
            hours = (assigned_at - created_at).total_seconds() / 3600
            if 0 <= hours < 720:
                triage_durations.append(hours)

    def _detail(refs):
        if not refs:
            return None
        shown = refs[:DETAIL_CAP]
        tail = f"\n… and {len(refs) - DETAIL_CAP} more" if len(refs) > DETAIL_CAP else ""
        return "\n".join(shown) + tail

    snapshots = [
        MetricSnapshot(
            metric="wo_not_triaged_24h",
            category="Maintenance Stalls",
            source="Airtable",
            value=float(len(not_triaged_refs)),
            detail=_detail(not_triaged_refs),
            status=_threshold(len(not_triaged_refs), warn=3, critical=7),
        ),
        MetricSnapshot(
            metric="stalled_wo_72h",
            category="Maintenance Stalls",
            source="Airtable",
            value=float(len(stalled_refs)),
            detail=_detail(stalled_refs),
            status=_threshold(len(stalled_refs), warn=5, critical=15),
        ),
    ]

    if triage_durations:
        avg = round(sum(triage_durations) / len(triage_durations), 2)
        snapshots.append(MetricSnapshot(
            metric="time_to_triage_hours",
            category="Maintenance Stalls",
            source="Airtable",
            value=avg,
            secondary_value=float(len(triage_durations)),
            status=_threshold(avg, warn=12, critical=24),
        ))

    return snapshots


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def _execution(api_key: str) -> list[MetricSnapshot]:
    table = Api(api_key).base(BPM_BASE_ID).table("Tasks")
    today_str = _today().isoformat()

    formula = (
        f"AND("
        f"IS_BEFORE({{Target Completion Date}}, '{today_str}'), "
        f"{{Target Completion Date}} != '', "
        f"{{Status}} != 'Complete', "
        f"{{Status}} != 'N/A', "
        f"{{Status}} != 'Project Cancelled'"
        f")"
    )

    records = table.all(
        fields=["Status", "Target Completion Date", "Task Name"],
        formula=formula,
    )

    return [MetricSnapshot(
        metric="past_due_tasks",
        category="Execution",
        source="Airtable",
        value=float(len(records)),
        status=_threshold(len(records), warn=5, critical=15),
    )]


# ---------------------------------------------------------------------------
# Missive (live)
# ---------------------------------------------------------------------------

def _missive() -> list[MetricSnapshot]:
    if not MISSIVE_TOKEN:
        print("  [live/missive] MISSIVE_TOKEN not set — skipping")
        return []

    headers = {
        "Authorization": f"Bearer {MISSIVE_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    snapshots = []

    inboxes_over: list[str] = []
    try:
        org_id = _get_org_id(headers)
        now = datetime.now(timezone.utc)
        start_ts = int((now - timedelta(hours=24)).timestamp())
        end_ts = int(now.timestamp())

        reports = _fetch_reports_parallel(headers, org_id, start_ts, end_ts, list(TEAM_INBOXES.items()))
        for team_name, report in reports.items():
            sel = report.get("reports", report).get("selected_period", {})
            metrics = sel.get("global", {}).get("totals", {}).get("metrics", {})
            inbound = _mv(metrics, "inbound_count") or 0
            replied = _mv(metrics, "first_reply_count") or 0
            open_est = max(0, inbound - replied)
            if open_est > INBOX_THRESHOLD:
                inboxes_over.append(f"{team_name} (~{open_est} open)")
    except Exception as e:
        print(f"  [live/missive] inbox check failed: {e}")

    # Always write inboxes_over_50 — even if all teams failed, record 0
    count_over = len(inboxes_over)
    snapshots.append(MetricSnapshot(
        metric="inboxes_over_50",
        category="Communication Backlog",
        source="Missive",
        value=float(count_over),
        value_text=", ".join(inboxes_over) if inboxes_over else "None",
        detail="\n".join(inboxes_over) if inboxes_over else None,
        status="Critical" if count_over > 0 else "OK",
    ))

    # oldest_message_age_hours: deferred — Missive conversations endpoint
    # requires an inbox filter; global listing is not supported by the API.

    return snapshots


# ---------------------------------------------------------------------------
# Missive API helpers
# ---------------------------------------------------------------------------

def _get_org_id(headers):
    resp = requests.get(f"{MISSIVE_BASE}/organizations", headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()["organizations"][0]["id"]


def _submit_report(headers, org_id, start_ts, end_ts, team_id) -> str:
    """Submit a report job and return the report ID. Does NOT wait for completion."""
    payload = {"reports": {"organization": org_id, "start": start_ts, "end": end_ts,
                           "time_zone": "America/New_York", "teams": [team_id]}}
    resp = requests.post(f"{MISSIVE_BASE}/analytics/reports", headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()["reports"]["id"]


def _poll_report(headers, report_id, timeout=60) -> dict:
    """Poll a submitted report until complete."""
    url = f"{MISSIVE_BASE}/analytics/reports/{report_id}"
    deadline = time.time() + timeout
    time.sleep(3)
    while time.time() < deadline:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 200:
            return r.json()
        if r.status_code not in (202, 404):
            r.raise_for_status()
        time.sleep(3)
    raise TimeoutError(f"Report {report_id} timed out")


def _fetch_reports_parallel(headers, org_id, start_ts, end_ts, team_ids_names: list) -> dict:
    """
    Submit all reports with a stagger, then poll them all in parallel.
    Returns {team_name: report_data}.
    """
    # Step 1: submit reports one at a time with 1.5s gap to avoid 429
    submitted = {}  # team_name → report_id
    for team_id, team_name in team_ids_names:
        try:
            report_id = _submit_report(headers, org_id, start_ts, end_ts, team_id)
            submitted[team_name] = report_id
        except Exception as e:
            print(f"  [missive] Submit failed for {team_name}: {e}")
        time.sleep(1.5)

    # Step 2: poll all submitted reports in parallel
    results = {}
    with ThreadPoolExecutor(max_workers=len(submitted)) as pool:
        futures = {
            pool.submit(_poll_report, headers, report_id): team_name
            for team_name, report_id in submitted.items()
        }
        for future in as_completed(futures):
            team_name = futures[future]
            try:
                results[team_name] = future.result()
            except Exception as e:
                print(f"  [missive] Poll failed for {team_name}: {e}")

    return results


def _mv(d, key):
    m = d.get(key, {})
    return m.get("v") if isinstance(m, dict) else None


def _parse_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None


def _threshold(value, warn, critical):
    if value >= critical:
        return "Critical"
    if value >= warn:
        return "Warning"
    return "OK"
