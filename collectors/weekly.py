"""
collectors/weekly.py

Weekly metrics — written every Sunday night after Rippling sync completes.
period_date = Monday of the prior week (the week just ended).

  - brm_utilization_pct : Allocated Estimated Billable Hours from Work Logs / (tech_count × 40h)
  - wo_assigned_pct     : % of whole active meld pipeline assigned (excl. project melds)
  - leasing_velocity    : Avg days AvailableOn→CountersignedDate for new leases (excl. renewals)
  - sla_pct             : Per-user SLA%, top 3 offenders in detail (prior Mon–Sun window)
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone, timedelta, date

import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from pyairtable import Api

from airtable_writer import MetricSnapshot, _today


AFP_BASE_ID      = os.environ.get("AFP_BASE_ID", "appg8MZ0eQP6CFyfZ")
LEASING_BASE_ID  = "appAZr2BlFWD11rSQ"   # Leasing Database
LEASE_UP_TABLE   = "tblGdXesu731CLnBF"   # Lease-Up Cycles
MISSIVE_TOKEN    = os.environ.get("MISSIVE_TOKEN", "")
MISSIVE_BASE     = "https://public.missiveapp.com/v1"
BRM_TARGET_PCT   = 85.0

# Service/bot accounts to exclude from SLA reporting
SKIP_USER_NAMES = {"BPM-DOS Team", "Reva Nowell", "Evan Mayo", "Jesse Leichtentritt"}


def collect(api_key: str) -> list[MetricSnapshot]:
    now = datetime.now(timezone.utc)
    days_since_monday = now.weekday()
    prior_monday = (now - timedelta(days=days_since_monday + 7)).date()
    prior_sunday = prior_monday + timedelta(days=6)
    prior_monday_str = prior_monday.isoformat()
    prior_sunday_str = prior_sunday.isoformat()

    snapshots = []
    snapshots += _brm_utilization(api_key, prior_monday_str, prior_monday, prior_sunday_str)
    snapshots += _wo_assigned(api_key, prior_monday)
    snapshots += _leasing_velocity(api_key, prior_monday, prior_monday + timedelta(days=6), prior_monday)
    snapshots += _sla(prior_monday, prior_monday, prior_sunday)
    return snapshots


# ---------------------------------------------------------------------------
# BRM Utilization — Work Logs only (Allocated Estimated Billable Hours)
# ---------------------------------------------------------------------------

def _brm_utilization(api_key: str, week_start_str: str, week_date: date,
                     week_end_str: str) -> list[MetricSnapshot]:
    try:
        afp_key = os.environ.get("AFP_API_KEY") or api_key
        logs = Api(afp_key).base(AFP_BASE_ID).table("Work Logs").all(
            fields=["Allocated Estimated Billable Hours", "TechName"],
            formula=(
                f"AND("
                f"IS_AFTER({{CheckIn}}, '{week_start_str}'), "
                f"IS_BEFORE({{CheckIn}}, '{week_end_str}')"
                f")"
            ),
        )

        # Sum billed hours and break down by tech
        tech_hours: dict[str, float] = {}
        for rec in logs:
            f = rec["fields"]
            hrs = float(f.get("Allocated Estimated Billable Hours") or 0)
            tech = (f.get("TechName") or "Unknown").strip()
            tech_hours[tech] = tech_hours.get(tech, 0) + hrs

        total_billed = sum(tech_hours.values())
        tech_count = int(os.environ.get("BRM_TECH_COUNT", "0"))
        hours_per_tech = float(os.environ.get("BRM_HOURS_PER_TECH_WEEK", "40"))

        detail_lines = [f"{tech}: {hrs:.1f}h" for tech, hrs in sorted(tech_hours.items())]

        if tech_count > 0:
            available = tech_count * hours_per_tech
            pct = round(min(total_billed / available * 100, 100), 2)
            return [MetricSnapshot(
                metric="brm_utilization_pct",
                category="Weekly KPI",
                source="Airtable",
                value=pct,
                secondary_value=total_billed,
                target=BRM_TARGET_PCT,
                value_text=f"{pct}% ({total_billed:.1f}h billed / {available:.0f}h available)",
                detail="\n".join(detail_lines),
                status=_util_status(pct),
                period_date=week_date,
            )]
        else:
            return [MetricSnapshot(
                metric="brm_utilization_pct",
                category="Weekly KPI",
                source="Airtable",
                value=total_billed,
                target=BRM_TARGET_PCT,
                value_text=f"{total_billed:.1f}h billed (set BRM_TECH_COUNT for %)",
                detail="\n".join(detail_lines),
                status="Warning",
                period_date=week_date,
            )]
    except Exception as e:
        print(f"  [weekly] brm_utilization failed: {e}")
    return []


# ---------------------------------------------------------------------------
# WOs Assigned % — whole pipeline, exclude project melds
# ---------------------------------------------------------------------------

def _wo_assigned(api_key: str, week_date: date) -> list[MetricSnapshot]:
    try:
        afp_key = os.environ.get("AFP_API_KEY") or api_key
        all_melds = Api(afp_key).base(AFP_BASE_ID).table("Melds (Spine)").all(
            fields=["AssignedAt", "ProjectID", "IsActive"],
            formula="AND({IsActive}, OR({ProjectID} = '', {ProjectID} = BLANK()))",
        )
        total = len(all_melds)
        assigned = sum(1 for r in all_melds
                       if r["fields"].get("AssignedAt") not in (None, "", "None"))
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
# Leasing Velocity — avg days from AvailableOn to CountersignedDate
# ---------------------------------------------------------------------------

def _leasing_velocity(api_key: str, week_start: date, week_end: date,
                      week_date: date) -> list[MetricSnapshot]:
    """
    Queries Lease-Up Cycles in the Leasing Database.
    Velocity = Outcome Date − Date Activated for cycles that closed as 'Leased'
    in the prior week. Renewals do not generate Lease-Up Cycles so no extra
    exclusion is needed.
    """
    try:
        cycles = Api(api_key).base(LEASING_BASE_ID).table(LEASE_UP_TABLE).all(
            fields=["Date Activated", "Outcome Date", "Outcome"],
            formula=(
                f"AND("
                f"IS_AFTER({{Outcome Date}}, '{(week_start - timedelta(days=1)).isoformat()}'), "
                f"IS_BEFORE({{Outcome Date}}, '{(week_end + timedelta(days=1)).isoformat()}'), "
                f"{{Outcome}} = 'Leased'"
                f")"
            ),
        )

        cycle_count = len(cycles)

        if not cycles:
            return [MetricSnapshot(
                metric="leasing_velocity",
                category="Weekly KPI",
                source="Airtable",
                value=0.0,
                secondary_value=0.0,
                value_text="0 leases closed this week",
                status="OK",
                period_date=week_date,
            )]

        days_list: list[int] = []
        for rec in cycles:
            f = rec["fields"]
            activated_raw = f.get("Date Activated")
            outcome_raw   = f.get("Outcome Date")
            if not activated_raw or not outcome_raw:
                continue
            try:
                activated_date = date.fromisoformat(activated_raw[:10])
                outcome_date   = date.fromisoformat(outcome_raw[:10])
                days = (outcome_date - activated_date).days
                if 0 <= days <= 365:
                    days_list.append(days)
            except (ValueError, TypeError):
                continue

        if not days_list:
            return [MetricSnapshot(
                metric="leasing_velocity",
                category="Weekly KPI",
                source="Airtable",
                value=0.0,
                secondary_value=float(cycle_count),
                value_text=f"{cycle_count} lease(s) closed; no Date Activated found",
                status="Warning",
                period_date=week_date,
            )]

        avg_days = round(sum(days_list) / len(days_list), 1)
        status = "OK" if avg_days <= 14 else ("Warning" if avg_days <= 30 else "Critical")
        return [MetricSnapshot(
            metric="leasing_velocity",
            category="Weekly KPI",
            source="Airtable",
            value=avg_days,
            secondary_value=float(cycle_count),
            value_text=f"{avg_days}d avg ({cycle_count} lease(s))",
            status=status,
            period_date=week_date,
        )]

    except Exception as e:
        print(f"  [weekly] leasing_velocity failed: {e}")
    return []


# ---------------------------------------------------------------------------
# SLA % — per-user, top 3 offenders
# ---------------------------------------------------------------------------

def _sla(week_date: date, prior_monday: date, prior_sunday: date) -> list[MetricSnapshot]:
    if not MISSIVE_TOKEN:
        return []

    headers = {
        "Authorization": f"Bearer {MISSIVE_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        org_id = _get_org_id(headers)

        # Discover users from recent conversations
        users = _discover_users(headers, org_id)
        if not users:
            return []

        start_ts = int(datetime(prior_monday.year, prior_monday.month, prior_monday.day,
                                tzinfo=timezone.utc).timestamp())
        end_ts   = int(datetime(prior_sunday.year, prior_sunday.month, prior_sunday.day,
                                23, 59, 59, tzinfo=timezone.utc).timestamp())

        # Fetch per-user analytics in parallel
        reports = _fetch_reports_parallel(
            headers, org_id, start_ts, end_ts,
            list(users.items()), filter_key="users"
        )

        # Compute SLA% per user
        user_sla: dict[str, float] = {}
        for user_name, report in reports.items():
            sel     = report.get("reports", report).get("selected_period", {})
            tallies = sel.get("global", {}).get("totals", {}).get("tallies", {})
            frt     = tallies.get("first_reply_time_counts", [])
            if not frt:
                continue
            total = sum(i.get("v", 0) for i in frt)
            if total == 0:
                continue
            b24h = {"1m","2m","3m","4m","5m","10m","15m","30m","45m",
                    "1h","2h","3h","4h","6h","8h","10h","12h","24h"}
            within_24h = sum(i.get("v", 0) for i in frt if i.get("d") in b24h)
            user_sla[user_name] = round(within_24h / total * 100, 1)

        if not user_sla:
            return []

        # Overall SLA (weighted average across all users)
        overall = round(sum(user_sla.values()) / len(user_sla), 1)

        # Bottom 3 offenders (lowest SLA%)
        bottom3 = sorted(user_sla.items(), key=lambda x: x[1])[:3]
        detail_lines = [f"{name}: {pct}%" for name, pct in bottom3]

        return [MetricSnapshot(
            metric="sla_pct",
            category="Weekly KPI",
            source="Missive",
            value=overall,
            detail="Bottom 3 SLA%:\n" + "\n".join(detail_lines),
            status="OK" if overall >= 95 else ("Warning" if overall >= 80 else "Critical"),
            period_date=week_date,
        )]

    except Exception as e:
        print(f"  [weekly] sla failed: {e}")
    return []


def _discover_users(headers: dict, org_id: str, pages: int = 2) -> dict:
    """
    Fetch recent conversations to discover user IDs.
    Returns {user_id: user_name}, excluding service accounts.
    """
    users = {}
    params = {"organization": org_id, "all": "true", "limit": 50}

    for _ in range(pages):
        resp = requests.get(f"{MISSIVE_BASE}/conversations", headers=headers,
                            params=params, timeout=30)
        if resp.status_code != 200:
            break
        convos = resp.json().get("conversations", [])
        if not convos:
            break
        for c in convos:
            for u in (c.get("users") or []):
                uid  = u.get("id")
                name = u.get("name") or u.get("email", "")
                if uid and name and name not in SKIP_USER_NAMES:
                    users[uid] = name
        if len(convos) < 50:
            break
        oldest_ts = min(c.get("last_activity_at", 0) for c in convos)
        params = {"organization": org_id, "all": "true", "limit": 50, "until": oldest_ts}

    return users


# ---------------------------------------------------------------------------
# Missive API helpers (mirrored from live.py)
# ---------------------------------------------------------------------------

def _get_org_id(headers):
    r = requests.get(f"{MISSIVE_BASE}/organizations", headers=headers, timeout=15)
    r.raise_for_status()
    return r.json()["organizations"][0]["id"]


def _submit_report(headers, org_id, start_ts, end_ts, filter_key, filter_id) -> str:
    payload = {"reports": {"organization": org_id, "start": start_ts, "end": end_ts,
                           "time_zone": "America/New_York", filter_key: [filter_id]}}
    r = requests.post(f"{MISSIVE_BASE}/analytics/reports", headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()["reports"]["id"]


def _poll_report(headers, report_id, timeout=60) -> dict:
    url = f"{MISSIVE_BASE}/analytics/reports/{report_id}"
    deadline = time.time() + timeout
    time.sleep(5)
    while time.time() < deadline:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 200:
            return r.json()
        if r.status_code not in (202, 404):
            r.raise_for_status()
        time.sleep(5)
    raise TimeoutError(f"Report {report_id} timed out")


def _fetch_reports_parallel(headers, org_id, start_ts, end_ts, items: list,
                             filter_key: str = "teams") -> dict:
    submitted = {}
    for entity_id, entity_name in items:
        try:
            rid = _submit_report(headers, org_id, start_ts, end_ts, filter_key, entity_id)
            submitted[entity_name] = rid
        except Exception as e:
            print(f"  [missive] Submit failed for {entity_name}: {e}")
        time.sleep(1.5)

    results = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(_poll_report, headers, rid): name
                   for name, rid in submitted.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except Exception as e:
                print(f"  [missive] Poll failed for {name}: {e}")

    return results


def _util_status(pct: float) -> str:
    if pct >= BRM_TARGET_PCT:       return "OK"
    if pct >= BRM_TARGET_PCT - 15:  return "Warning"
    return "Critical"


def _assignment_status(pct: float) -> str:
    if pct < 70:  return "Critical"
    if pct < 85:  return "Warning"
    return "OK"
