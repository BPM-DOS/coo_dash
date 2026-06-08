"""
collectors/missive.py

Metrics from the Missive Analytics API (same approach as Missive_Dashboard.py):
  - inboxes_over_50          : Count of team inboxes with estimated open conversations > 50
  - oldest_message_age_hours : Age in hours of the oldest open conversation
  - sla_pct                  : % of conversations replied to within 24h (rolling 24h window)

Auth: MISSIVE_TOKEN env var (Bearer token) — same var used in Missive_Dashboard.py
Team inbox IDs are hardcoded from Missive Settings → Teams.

If MISSIVE_TOKEN is not set, this collector skips silently.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone, timedelta

import requests

from airtable_writer import MetricSnapshot


MISSIVE_TOKEN = os.environ.get("MISSIVE_TOKEN", "")
BASE = "https://public.missiveapp.com/v1"
TIME_ZONE = "America/New_York"

# Team inbox IDs confirmed from Missive_Dashboard.py (Missive Settings → Teams)
TEAM_INBOXES = {
    "9a7adab1-5e02-4ca7-9d60-306fc274d186": "Finance",
    "ff188c21-4cb8-4585-8ae4-ad245b30c7b2": "Leadership",
    "4e3e2112-7a52-4cba-a77d-d25142def86d": "Maintenance",
    "6c4c0b77-67e7-443f-be30-c66bb6e87e8e": "Marketing",
    "fb007418-7e6a-4db5-bf41-b1f8a3fddeeb": "Office",
    "27036b84-8fc4-480b-9334-94195631fd5b": "Property Management",
    "891298e2-2a54-47d8-ba83-ff007a8f751b": "Rentals/Leasing",
    "01ccca67-ec22-4627-ba23-a567fac25a98": "Sales",
}

INBOX_THRESHOLD = int(os.environ.get("MISSIVE_INBOX_THRESHOLD", "50"))


def collect() -> list[MetricSnapshot]:
    if not MISSIVE_TOKEN:
        print("  [missive] MISSIVE_TOKEN not set — skipping Missive metrics")
        return []
    try:
        return _collect()
    except Exception as exc:
        print(f"  [missive] Collection failed: {exc}")
        return []


def _collect() -> list[MetricSnapshot]:
    headers = {
        "Authorization": f"Bearer {MISSIVE_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    org_id = _get_org_id(headers)

    # Rolling 24h window for analytics reports
    now = datetime.now(timezone.utc)
    start_ts = int((now - timedelta(hours=24)).timestamp())
    end_ts = int(now.timestamp())

    all_frt_counts: list[dict] = []
    inboxes_over_threshold: list[str] = []  # names of inboxes over threshold

    for team_id, team_name in TEAM_INBOXES.items():
        try:
            report = _fetch_report(headers, org_id, start_ts, end_ts, team_id)
            sel = report.get("reports", report).get("selected_period", {})
            metrics = sel.get("global", {}).get("totals", {}).get("metrics", {})
            tallies = sel.get("global", {}).get("totals", {}).get("tallies", {})

            frt = tallies.get("first_reply_time_counts", [])
            all_frt_counts.extend(frt)

            # Estimate open conversations as inbound minus replied
            inbound = _mv(metrics, "inbound_count") or 0
            replied = _mv(metrics, "first_reply_count") or 0
            open_est = max(0, inbound - replied)
            if open_est > INBOX_THRESHOLD:
                inboxes_over_threshold.append(f"{team_name} (~{open_est} open)")

        except Exception as e:
            print(f"  [missive] Skipping {team_name}: {e}")

    count_over = len(inboxes_over_threshold)
    snapshots = [
        MetricSnapshot(
            metric="inboxes_over_50",
            category="Communication Backlog",
            source="Missive",
            value=float(count_over),
            value_text=", ".join(inboxes_over_threshold) if inboxes_over_threshold else "None",
            detail="\n".join(inboxes_over_threshold) if inboxes_over_threshold else None,
            status="Critical" if count_over > 0 else "OK",
        )
    ]

    if all_frt_counts:
        sla_1h, sla_4h, sla_24h = _sla_pcts(all_frt_counts)
        if sla_24h is not None:
            snapshots.append(
                MetricSnapshot(
                    metric="sla_pct",
                    category="Weekly KPI",
                    source="Missive",
                    value=round(sla_24h, 1),
                    secondary_value=round(sla_1h, 1) if sla_1h is not None else None,
                    status=_sla_status(sla_24h),
                )
            )

    oldest_age_hours = _get_oldest_open_age_hours(headers)
    if oldest_age_hours is not None:
        snapshots.append(
            MetricSnapshot(
                metric="oldest_message_age_hours",
                category="Communication Backlog",
                source="Missive",
                value=round(oldest_age_hours, 1),
                status=_age_status(oldest_age_hours),
            )
        )

    return snapshots


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _get_org_id(headers: dict) -> str:
    resp = requests.get(f"{BASE}/organizations", headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()["organizations"][0]["id"]


def _fetch_report(headers: dict, org_id: str, start_ts: int, end_ts: int, team_id: str) -> dict:
    """POST analytics report, then poll until complete — mirrors Missive_Dashboard.py."""
    payload = {"reports": {
        "organization": org_id,
        "start": start_ts,
        "end": end_ts,
        "time_zone": TIME_ZONE,
        "teams": [team_id],
    }}
    resp = requests.post(f"{BASE}/analytics/reports", headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    report_id = resp.json()["reports"]["id"]

    url = f"{BASE}/analytics/reports/{report_id}"
    deadline = time.time() + 60
    time.sleep(3)
    while time.time() < deadline:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 200:
            return r.json()
        if r.status_code not in (202, 404):
            r.raise_for_status()
        time.sleep(3)
    raise TimeoutError(f"Missive report {report_id} timed out")


def _get_oldest_open_age_hours(headers: dict) -> float | None:
    """Fetch the oldest open conversation using the conversations endpoint."""
    try:
        resp = requests.get(
            f"{BASE}/conversations",
            headers=headers,
            params={"limit": 1, "all": "false"},
            timeout=15,
        )
        resp.raise_for_status()
        convos = resp.json().get("conversations", [])
        if not convos:
            return None
        created_at = convos[0].get("created_at")
        if not created_at:
            return None
        created_dt = datetime.fromtimestamp(created_at, tz=timezone.utc)
        return (datetime.now(timezone.utc) - created_dt).total_seconds() / 3600
    except Exception:
        return None


def _mv(d: dict, key: str):
    m = d.get(key, {})
    return m.get("v") if isinstance(m, dict) else None


def _sla_pcts(frt_counts: list) -> tuple:
    total = sum(item.get("v", 0) for item in frt_counts)
    if not total:
        return None, None, None
    b1h  = {"1m", "2m", "3m", "4m", "5m", "10m", "15m", "30m", "45m", "1h"}
    b4h  = b1h | {"2h", "3h", "4h"}
    b24h = b4h | {"6h", "8h", "10h", "12h", "24h"}
    count = lambda s: sum(item.get("v", 0) for item in frt_counts if item.get("d") in s)
    return count(b1h) / total * 100, count(b4h) / total * 100, count(b24h) / total * 100


def _sla_status(pct: float) -> str:
    if pct >= 95:
        return "OK"
    if pct >= 80:
        return "Warning"
    return "Critical"


def _age_status(hours: float) -> str:
    if hours >= 72:
        return "Critical"
    if hours >= 24:
        return "Warning"
    return "OK"
