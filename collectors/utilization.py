"""
collectors/utilization.py

  - brm_utilization_pct : Read from BPM Scorecard → Billable Hour Util Rate table.
                          Sums all techs for the prior week. Worked Hours come from
                          Rippling (entered manually); Total Billed Hours from PropertyMeld.
                          Falls back to computing from Work Logs if no Scorecard row exists.
  - wo_assigned_pct     : % of ALL active melds (whole pipeline) that have been assigned,
                          excluding project-linked melds.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta

from pyairtable import Api

from airtable_writer import MetricSnapshot


AFP_BASE_ID = os.environ.get("AFP_BASE_ID", "appg8MZ0eQP6CFyfZ")
SCORECARD_BASE_ID = os.environ.get("SCORECARD_BASE_ID", "appAwZySwIwQT0G0a")
UTIL_RATE_TABLE = "Billable Hour Util Rate"

BRM_TARGET_PCT = 85.0


def collect(api_key: str) -> list[MetricSnapshot]:
    afp_key = os.environ.get("AFP_API_KEY") or api_key
    api = Api(afp_key)

    now = datetime.now(timezone.utc)
    # Prior full week: the Monday–Sunday that just ended
    days_since_monday = now.weekday()
    prior_week_end   = (now - timedelta(days=days_since_monday)).strftime("%Y-%m-%d")
    prior_week_start = (now - timedelta(days=days_since_monday + 7)).strftime("%Y-%m-%d")

    snapshots = []

    # --- brm_utilization_pct ---
    # Primary: read from BPM Scorecard Billable Hour Util Rate table
    util_snapshot = _util_from_scorecard(api_key, prior_week_start, prior_week_end)
    if util_snapshot:
        snapshots.append(util_snapshot)
    else:
        # Fallback: compute from Work Logs directly (no worked hours denominator, raw billed hours only)
        snapshots.append(_util_from_work_logs(api, afp_key, prior_week_start, prior_week_end))

    # --- wo_assigned_pct (whole active pipeline, excluding project melds) ---
    melds_table = api.base(AFP_BASE_ID).table("Melds (Spine)")
    all_melds = melds_table.all(
        fields=["AssignedAt", "ProjectID", "IsActive"],
        formula=(
            "AND("
            "{IsActive}, "
            "OR(ProjectID = '', ProjectID = BLANK())"
            ")"
        ),
    )

    total_melds = len(all_melds)
    assigned_melds = sum(
        1 for rec in all_melds
        if rec.get("fields", {}).get("AssignedAt") not in (None, "", "None")
    )

    if total_melds > 0:
        wo_assigned_pct = round(assigned_melds / total_melds * 100, 2)
        snapshots.append(
            MetricSnapshot(
                metric="wo_assigned_pct",
                category="Weekly KPI",
                source="Airtable",
                value=wo_assigned_pct,
                secondary_value=float(total_melds),
                status=_assignment_status(wo_assigned_pct),
            )
        )

    return snapshots


def _util_from_scorecard(api_key: str, week_start: str, week_end: str) -> MetricSnapshot | None:
    """Read prior week utilization from BPM Scorecard Billable Hour Util Rate table."""
    try:
        sc_key = os.environ.get("SCORECARD_API_KEY") or api_key
        api = Api(sc_key)
        table = api.base(SCORECARD_BASE_ID).table(UTIL_RATE_TABLE)

        # Rows are per-tech per-week; fetch all rows for prior week
        rows = table.all(
            fields=["Week", "Tech Name", "Worked Hours", "Total Billed Hours", "Utilization RATE"],
            formula=f"{{Week}} = '{week_start}'",
        )

        if not rows:
            return None

        total_worked = 0.0
        total_billed = 0.0
        tech_lines = []

        for rec in rows:
            f = rec.get("fields", {})
            worked = float(f.get("Worked Hours") or 0)
            billed = float(f.get("Total Billed Hours") or 0)
            tech = f.get("Tech Name") or "Unknown"
            total_worked += worked
            total_billed += billed
            tech_pct = round(billed / worked * 100, 1) if worked > 0 else 0
            tech_lines.append(f"{tech}: {billed:.1f}h billed / {worked:.1f}h worked = {tech_pct}%")

        if total_worked == 0:
            return None

        overall_pct = round(total_billed / total_worked * 100, 2)

        return MetricSnapshot(
            metric="brm_utilization_pct",
            category="Weekly KPI",
            source="Airtable",
            value=overall_pct,
            secondary_value=total_billed,
            target=BRM_TARGET_PCT,
            detail="\n".join(tech_lines),
            value_text=f"{overall_pct}% ({total_billed:.1f}h billed / {total_worked:.1f}h worked)",
            status=_utilization_status(overall_pct),
        )
    except Exception as e:
        print(f"  [utilization] Scorecard read failed: {e} — falling back to Work Logs")
        return None


def _util_from_work_logs(api, afp_key: str, week_start: str, week_end: str) -> MetricSnapshot:
    """Fallback: sum Allocated Estimated Billable Hours from Work Logs for the prior week."""
    work_logs_table = api.base(AFP_BASE_ID).table("Work Logs")
    week_logs = work_logs_table.all(
        fields=["Allocated Estimated Billable Hours", "ExternalID"],
        formula=(
            f"AND("
            f"IS_AFTER({{CheckIn}}, '{week_start}'), "
            f"IS_BEFORE({{CheckIn}}, '{week_end}')"
            f")"
        ),
    )

    total_billed = 0.0
    for rec in week_logs:
        hrs = rec.get("fields", {}).get("Allocated Estimated Billable Hours")
        if hrs:
            try:
                total_billed += float(hrs)
            except (TypeError, ValueError):
                pass

    tech_count = int(os.environ.get("BRM_TECH_COUNT", "0"))
    hours_per_tech = float(os.environ.get("BRM_HOURS_PER_TECH_WEEK", "40"))

    if tech_count > 0:
        available = tech_count * hours_per_tech
        pct = round(min(total_billed / available * 100, 100), 2)
        return MetricSnapshot(
            metric="brm_utilization_pct",
            category="Weekly KPI",
            source="Airtable",
            value=pct,
            secondary_value=total_billed,
            target=BRM_TARGET_PCT,
            value_text=f"{pct}% ({total_billed:.1f}h billed — Rippling hours not yet entered)",
            status=_utilization_status(pct),
        )
    else:
        return MetricSnapshot(
            metric="brm_utilization_pct",
            category="Weekly KPI",
            source="Airtable",
            value=total_billed,
            target=BRM_TARGET_PCT,
            value_text=f"{total_billed:.1f}h billed (no Rippling hours — enter in Scorecard)",
            status="Warning",
        )


def _utilization_status(pct: float) -> str:
    if pct >= BRM_TARGET_PCT:
        return "OK"
    if pct >= BRM_TARGET_PCT - 15:
        return "Warning"
    return "Critical"


def _assignment_status(pct: float) -> str:
    if pct < 70:
        return "Critical"
    if pct < 85:
        return "Warning"
    return "OK"
