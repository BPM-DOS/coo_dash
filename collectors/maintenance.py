"""
collectors/maintenance.py

Metrics from Melds (Spine) in the Appfolio Database:
  - wo_not_triaged_24h   : Melds still in PENDING_ASSIGNMENT > 24h, with ReferenceID drill-down
  - stalled_wo_72h       : Melds assigned 72h+ ago with no chat activity in that window,
                           excluding turn melds (ProjectID set, or MICR/MOCR in name)
  - time_to_triage_hours : Avg hours from CreatedAt → AssignedAt (rolling 30d, resident-submitted)
"""

from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta

from pyairtable import Api

from airtable_writer import MetricSnapshot


AFP_BASE_ID = os.environ.get("AFP_BASE_ID", "appg8MZ0eQP6CFyfZ")
MELDS_TABLE = "Melds (Spine)"

# Only this status means "not yet triaged"
UNTRIAGED_STATUS = "PENDING_ASSIGNMENT"

# Terminal statuses — exclude from all counts
CLOSED_STATUSES = {
    "COMPLETED",
    "MANAGER_CANCELED",
    "TENANT_CANCELED",
    "CANCELLED",
    "CANCELED",
}

# Detail list cap — don't dump 500 IDs into a text field
DETAIL_CAP = 50


def collect(api_key: str) -> list[MetricSnapshot]:
    afp_key = os.environ.get("AFP_API_KEY") or api_key
    api = Api(afp_key)
    table = api.base(AFP_BASE_ID).table(MELDS_TABLE)

    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)
    cutoff_72h = now - timedelta(hours=72)
    cutoff_30d = now - timedelta(days=30)

    records = table.all(
        fields=["Status", "CreatedAt", "AssignedAt", "UpdatedAt", "IsActive",
                "ReferenceID", "BriefDescription", "Origin",
                "LastActivityAt", "LastActivityType", "ProjectID"],
        formula="{IsActive}",
    )

    not_triaged_refs: list[str] = []
    stalled_refs: list[str] = []
    triage_durations: list[float] = []

    for rec in records:
        f = rec.get("fields", {})
        status = (f.get("Status") or "").upper()
        created_at = _parse_dt(f.get("CreatedAt"))
        assigned_at = _parse_dt(f.get("AssignedAt"))
        updated_at = _parse_dt(f.get("UpdatedAt"))
        last_activity_at = _parse_dt(f.get("LastActivityAt"))
        ref_id = f.get("ReferenceID") or ""
        brief = f.get("BriefDescription") or ""
        project_id = f.get("ProjectID") or ""
        label = f"{ref_id} — {brief}" if ref_id and brief else (ref_id or brief or rec["id"])

        # Turn melds: part of a project, or name contains MICR/MOCR (turn project naming issues)
        brief_upper = brief.upper()
        is_turn = bool(project_id) or "MICR" in brief_upper or "MOCR" in brief_upper

        if status in CLOSED_STATUSES:
            continue

        # --- wo_not_triaged_24h ---
        # Still PENDING_ASSIGNMENT and created more than 24h ago
        if status == UNTRIAGED_STATUS and created_at and created_at < cutoff_24h:
            not_triaged_refs.append(label)

        # --- stalled_wo_72h ---
        # Assigned 72h+ ago, no chat activity in that window, and not a turn meld.
        # LastActivityAt (newest chat message timestamp) is the authoritative signal for
        # "someone actually touched this." UpdatedAt is intentionally NOT used here because
        # PropertyMeld bumps it on server-side events (cache stamps, denorm recalcs) that
        # are invisible to users.
        if (not is_turn
                and assigned_at and assigned_at < cutoff_72h
                and (last_activity_at is None or last_activity_at < cutoff_72h)):
            stalled_refs.append(label)

        # --- time_to_triage (rolling 30d, resident-submitted only) ---
        origin = (f.get("Origin") or "").upper()
        is_resident = origin in ("TENANT", "RESIDENT", "")  # blank origin is usually resident
        if is_resident and created_at and assigned_at and created_at >= cutoff_30d:
            hours = (assigned_at - created_at).total_seconds() / 3600
            if 0 <= hours < 720:
                triage_durations.append(hours)

    avg_triage_hours = (
        round(sum(triage_durations) / len(triage_durations), 2)
        if triage_durations else None
    )

    def _detail(refs: list[str]) -> str | None:
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

    if avg_triage_hours is not None:
        snapshots.append(
            MetricSnapshot(
                metric="time_to_triage_hours",
                category="Maintenance Stalls",
                source="Airtable",
                value=avg_triage_hours,
                secondary_value=float(len(triage_durations)),
                status=_threshold(avg_triage_hours, warn=12, critical=24),
            )
        )

    return snapshots


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, AttributeError):
        return None


def _threshold(value: float, warn: float, critical: float) -> str:
    if value >= critical:
        return "Critical"
    if value >= warn:
        return "Warning"
    return "OK"
