"""
collectors/maintenance.py

Metrics from Melds (Spine) in the Appfolio Database:
  - wo_not_triaged_24h   : Melds still in PENDING_ASSIGNMENT > 24h, with ReferenceID drill-down
  - stalled_wo_72h       : Melds assigned but not completed within 72h, with ReferenceID drill-down
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
                "ReferenceID", "BriefDescription", "Origin"],
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
        ref_id = f.get("ReferenceID") or ""
        brief = f.get("BriefDescription") or ""
        label = f"{ref_id} — {brief}" if ref_id and brief else (ref_id or brief or rec["id"])

        if status in CLOSED_STATUSES:
            continue

        # --- wo_not_triaged_24h ---
        # Still PENDING_ASSIGNMENT and created more than 24h ago
        if status == UNTRIAGED_STATUS and created_at and created_at < cutoff_24h:
            not_triaged_refs.append(label)

        # --- stalled_wo_72h ---
        # Has been assigned (AssignedAt exists) but not closed, and AssignedAt > 72h ago.
        # This catches anything stuck after assignment regardless of current status.
        if assigned_at and assigned_at < cutoff_72h:
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
