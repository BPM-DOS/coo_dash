"""
collectors/weekly.py

Weekly metrics — written every Sunday night after Rippling sync completes.
period_date = Monday of the prior week (the week just ended).

  - brm_utilization_pct : From BPM Scorecard Billable Hour Util Rate (Rippling + PropertyMeld)
  - wo_assigned_pct     : % of whole active meld pipeline that has been assigned (excl. projects)
  - leasing_velocity    : Average days on market for units leased (CountersignedDate) in past 7 days
  - sla_pct             : % of Missive conversations replied to within 24h (rolling 7d)
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone, timedelta, date

import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from pyairtable import Api

from airtable_writer import MetricSnapshot, _today


AFP_BASE_ID      = os.environ.get("AFP_BASE_ID",      "appg8MZ0eQP6CFyfZ")
SCORECARD_BASE_ID = os.environ.get("SCORECARD_BASE_ID","appAwZySwIwQT0G0a")
MISSIVE_TOKEN    = os.environ.get("MISSIVE_TOKEN", "")
MISSIVE_BASE     = "https://public.missiveapp.com/v1"
BRM_TARGET_PCT   = 85.0

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
    now = datetime.now(timezone.utc)
    # Monday of the prior full week
    days_since_monday = now.weekday()
    prior_monday = (now - timedelta(days=days_since_monday + 7)).date()
    prior_sunday_str = (prior_monday + timedelta(days=6)).isoformat()
    prior_monday_str = prior_monday.isoformat()

    snapshots = []
    snapshots += _brm_utilization(api_key, prior_monday_str, prior_monday)
    snapshots += _wo_assigned(api_key, prior_monday)
    snapshots += _leasing_velocity(api_key, prior_monday)
    snapshots += _sla(prior_monday)
    return snapshots


# ---------------------------------------------------------------------------
# BRM Utilization
# ---------------------------------------------------------------------------

def _brm_utilization(api_key: str, week_start_str: str, week_date: date) -> list[MetricSnapshot]:
    try:
        sc_key = os.environ.get("AFP_API_KEY") or api_key
        table = Api(sc_key).base(SCORECARD_BASE_ID).table("Billable Hour Util Rate")
        rows = table.all(
            fields=["Week", "Tech Name", "Worked Hours", "Total Billed Hours"],
            formula=f"{{Week}} = '{week_start_str}'",
        )
        if rows:
            total_worked = sum(float(r["fields"].get("Worked Hours") or 0) for r in rows)
            total_billed = sum(float(r["fields"].get("Total Billed Hours") or 0) for r in rows)
            if total_worked > 0:
                pct = round(total_billed / total_worked * 100, 2)
                tech_lines = []
                for r in rows:
                    f = r["fields"]
                    w = float(f.get("Worked Hours") or 0)
                    b = float(f.get("Total Billed Hours") or 0)
                    tech_pct = round(b / w * 100, 1) if w > 0 else 0
                    tech_lines.append(f"{f.get('Tech Name','?')}: {b:.1f}h / {w:.1f}h = {tech_pct}%")
                return [MetricSnapshot(
                    metric="brm_utilization_pct",
                    category="Weekly KPI",
                    source="Airtable",
                    value=pct,
                    secondary_value=total_billed,
                    target=BRM_TARGET_PCT,
                    value_text=f"{pct}% ({total_billed:.1f}h billed / {total_worked:.1f}h worked)",
                    detail="\n".join(tech_lines),
                    status=_util_status(pct),
                    period_date=week_date,
                )]
    except Exception as e:
        print(f"  [weekly] Scorecard read failed: {e}")

    # Fallback: work logs only
    try:
        afp_key = os.environ.get("AFP_API_KEY") or api_key
        logs = Api(afp_key).base(AFP_BASE_ID).table("Work Logs").all(
            fields=["Allocated Estimated Billable Hours"],
            formula=f"AND(IS_AFTER({{CheckIn}}, '{week_start_str}'), IS_BEFORE({{CheckIn}}, '{(week_date + timedelta(days=7)).isoformat()}'))",
        )
        total = sum(float(r["fields"].get("Allocated Estimated Billable Hours") or 0) for r in logs)
        return [MetricSnapshot(
            metric="brm_utilization_pct",
            category="Weekly KPI",
            source="Airtable",
            value=total,
            target=BRM_TARGET_PCT,
            value_text=f"{total:.1f}h billed (Rippling hours not yet in Scorecard)",
            status="Warning",
            period_date=week_date,
        )]
    except Exception as e:
        print(f"  [weekly] Work logs fallback failed: {e}")
        return []


# ---------------------------------------------------------------------------
# WOs Assigned %
# ---------------------------------------------------------------------------

def _wo_assigned(api_key: str, week_date: date) -> list[MetricSnapshot]:
    try:
        afp_key = os.environ.get("AFP_API_KEY") or api_key
        all_melds = Api(afp_key).base(AFP_BASE_ID).table("Melds (Spine)").all(
            fields=["AssignedAt", "ProjectID", "IsActive"],
            formula="AND({IsActive}, OR(ProjectID = '', ProjectID = BLANK()))",
        )
        total = len(all_melds)
        assigned = sum(1 for r in all_melds if r["fields"].get("AssignedAt") not in (None, "", "None"))
        if total > 0:
            pct = round(assigned / total * 100, 2)
            return [MetricSnapshot(
                metric="wo_assigned_pct",
                category="Weekly KPI",
                source="Airtable",
                value=pct,
                secondary_value=float(total),
                status=_assignment_status(pct),
                period_date=week_date,
            )]
    except Exception as e:
        print(f"  [weekly] wo_assigned failed: {e}")
    return []


# ---------------------------------------------------------------------------
# Leasing Velocity — avg days on market for units leased in past 7 days
# ---------------------------------------------------------------------------

def _leasing_velocity(api_key: str, week_date: date) -> list[MetricSnapshot]:
    try:
        afp_key = os.environ.get("AFP_API_KEY") or api_key
        afp = Api(afp_key).base(AFP_BASE_ID)

        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")

        # Leases countersigned in the past 7 days (new leases executed)
        recent_leases = afp.table("Leases (Spine)").all(
            fields=["CountersignedDate", "Unit", "UnitExternalID"],
            formula=(
                f"AND("
                f"IS_AFTER({{CountersignedDate}}, '{cutoff}'), "
                f"{{CountersignedDate}} != '', "
                f"{{IsActive}}"
                f")"
            ),
        )

        if not recent_leases:
            return [MetricSnapshot(
                metric="leasing_velocity",
                category="Weekly KPI",
                source="Airtable",
                value=None,
                value_text="No units leased this week",
                status="Warning",
                period_date=week_date,
            )]

        # Build map of unit ExternalID → AvailableOn
        unit_ext_ids = list({
            r["fields"].get("UnitExternalID")
            for r in recent_leases
            if r["fields"].get("UnitExternalID")
        })

        # Fetch AvailableOn for those units
        id_filter = "OR(" + ", ".join(f"{{ExternalID}} = '{uid}'" for uid in unit_ext_ids) + ")"
        units = afp.table("Units (Spine)").all(
            fields=["ExternalID", "AvailableOn"],
            formula=id_filter,
        )
        available_on_map = {
            r["fields"]["ExternalID"]: r["fields"].get("AvailableOn")
            for r in units
            if r["fields"].get("ExternalID")
        }

        dom_values: list[float] = []
        for rec in recent_leases:
            f = rec["fields"]
            countersigned = f.get("CountersignedDate")
            unit_ext = f.get("UnitExternalID")
            available = available_on_map.get(unit_ext) if unit_ext else None

            if not countersigned or not available:
                continue

            try:
                from datetime import date as date_type
                d1 = date_type.fromisoformat(available)
                d2 = date_type.fromisoformat(countersigned)
                dom = (d2 - d1).days
                if 0 <= dom <= 365:  # sanity cap
                    dom_values.append(float(dom))
            except (ValueError, TypeError):
                continue

        if not dom_values:
            return []

        avg_dom = round(sum(dom_values) / len(dom_values), 1)
        return [MetricSnapshot(
            metric="leasing_velocity",
            category="Weekly KPI",
            source="Airtable",
            value=avg_dom,
            secondary_value=float(len(dom_values)),  # units leased this week
            value_text=f"{avg_dom} days avg ({len(dom_values)} units leased)",
            status="OK" if avg_dom <= 21 else ("Warning" if avg_dom <= 35 else "Critical"),
            period_date=week_date,
        )]
    except Exception as e:
        print(f"  [weekly] leasing_velocity failed: {e}")
    return []


# ---------------------------------------------------------------------------
# SLA % (Missive)
# ---------------------------------------------------------------------------

def _sla(week_date: date) -> list[MetricSnapshot]:
    if not MISSIVE_TOKEN:
        return []
    headers = {
        "Authorization": f"Bearer {MISSIVE_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        org_id = _get_org_id(headers)
        now = datetime.now(timezone.utc)
        start_ts = int((now - timedelta(days=7)).timestamp())
        end_ts = int(now.timestamp())
        all_frt: list[dict] = []
        reports = _fetch_reports_parallel(headers, org_id, start_ts, end_ts, list(TEAM_INBOXES.items()))
        for team_name, report in reports.items():
            sel = report.get("reports", report).get("selected_period", {})
            tallies = sel.get("global", {}).get("totals", {}).get("tallies", {})
            all_frt.extend(tallies.get("first_reply_time_counts", []))
        if all_frt:
            _, _, sla_24h = _sla_pcts(all_frt)
            if sla_24h is not None:
                return [MetricSnapshot(
                    metric="sla_pct",
                    category="Weekly KPI",
                    source="Missive",
                    value=round(sla_24h, 1),
                    status="OK" if sla_24h >= 95 else ("Warning" if sla_24h >= 80 else "Critical"),
                    period_date=week_date,
                )]
    except Exception as e:
        print(f"  [weekly] sla failed: {e}")
    return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_org_id(headers):
    r = requests.get(f"{MISSIVE_BASE}/organizations", headers=headers, timeout=15)
    r.raise_for_status()
    return r.json()["organizations"][0]["id"]


def _submit_report(headers, org_id, start_ts, end_ts, team_id) -> str:
    payload = {"reports": {"organization": org_id, "start": start_ts, "end": end_ts,
                           "time_zone": "America/New_York", "teams": [team_id]}}
    r = requests.post(f"{MISSIVE_BASE}/analytics/reports", headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()["reports"]["id"]


def _poll_report(headers, report_id, timeout=60) -> dict:
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
    submitted = {}
    for team_id, team_name in team_ids_names:
        try:
            report_id = _submit_report(headers, org_id, start_ts, end_ts, team_id)
            submitted[team_name] = report_id
        except Exception as e:
            print(f"  [missive] Submit failed for {team_name}: {e}")
        time.sleep(1.5)

    results = {}
    with ThreadPoolExecutor(max_workers=len(submitted) or 1) as pool:
        futures = {
            pool.submit(_poll_report, headers, rid): name
            for name, rid in submitted.items()
        }
        for future in as_completed(futures):
            team_name = futures[future]
            try:
                results[team_name] = future.result()
            except Exception as e:
                print(f"  [missive] Poll failed for {team_name}: {e}")

    return results


def _sla_pcts(frt):
    total = sum(i.get("v", 0) for i in frt)
    if not total:
        return None, None, None
    b1h  = {"1m","2m","3m","4m","5m","10m","15m","30m","45m","1h"}
    b4h  = b1h | {"2h","3h","4h"}
    b24h = b4h | {"6h","8h","10h","12h","24h"}
    c = lambda s: sum(i.get("v", 0) for i in frt if i.get("d") in s)
    return c(b1h)/total*100, c(b4h)/total*100, c(b24h)/total*100


def _util_status(pct):
    if pct >= BRM_TARGET_PCT:       return "OK"
    if pct >= BRM_TARGET_PCT - 15:  return "Warning"
    return "Critical"


def _assignment_status(pct):
    if pct < 70:  return "Critical"
    if pct < 85:  return "Warning"
    return "OK"
