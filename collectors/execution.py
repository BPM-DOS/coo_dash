"""
collectors/execution.py

Metrics from Tasks table in BPM Operations Hub:
  - past_due_tasks : Count of open tasks where Target Completion Date < today
  - top_offender   : DEFERRED — definition not yet settled (push rules need clarifying)
"""

from __future__ import annotations

import os
from collections import Counter
from datetime import date

from pyairtable import Api

from airtable_writer import MetricSnapshot, _today


BPM_BASE_ID = os.environ.get("BPM_BASE_ID", "apprp203tCiyHl6Dw")
TASKS_TABLE = "Tasks"

# Status values that mean "not done"
OPEN_STATUSES = {"To Do", "In Progress", "Blocked", "Waiting"}


def collect(api_key: str) -> list[MetricSnapshot]:
    api = Api(api_key)
    table = api.base(BPM_BASE_ID).table(TASKS_TABLE)

    today = _today()
    today_str = today.isoformat()

    # Fetch open tasks with a due date in the past
    # Airtable formula: Status is not complete AND Target Completion Date < today
    # Exclude Complete, N/A (template/placeholder tasks), and Project Cancelled
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
        fields=["Status", "Target Completion Date", "Responsible Party", "Task Name"],
        formula=formula,
    )

    past_due_count = len(records)

    # Count overdue tasks per responsible party
    offender_counts: Counter[str] = Counter()
    for rec in records:
        f = rec.get("fields", {})
        # Responsible Party is a linked record — pyairtable returns list of record IDs.
        # We need the name; it's also available via lookup. Fetch display names separately
        # via the "Email (from Responsible Party)" or use the linked record name approach.
        # For now we store the record IDs and resolve names below.
        party_ids = f.get("Responsible Party") or []
        for pid in party_ids:
            offender_counts[pid] += 1

    top_offender_text = None
    top_offender_count = None

    if offender_counts:
        top_id, top_count = offender_counts.most_common(1)[0]
        top_offender_count = float(top_count)
        # Resolve the record ID → name by fetching the BPM Staff record
        try:
            staff_table = api.base(BPM_BASE_ID).table("BPM Staff")
            # use_field_ids=True returns fields keyed by field ID
            staff_rec = staff_table.get(top_id, use_field_ids=True)
            staff_fields = staff_rec.get("fields", {})
            # fldlDzW6T0eFT11XL is the full name field on BPM Staff
            name = staff_fields.get("fldlDzW6T0eFT11XL") or top_id
            top_offender_text = f"{name} ({top_count} overdue)"
        except Exception:
            top_offender_text = f"ID:{top_id} ({top_count} overdue)"

    snapshots = [
        MetricSnapshot(
            metric="past_due_tasks",
            category="Execution",
            source="Airtable",
            value=float(past_due_count),
            status=_threshold(past_due_count, warn=5, critical=15),
        ),
    ]

    if top_offender_text is not None:
        snapshots.append(
            MetricSnapshot(
                metric="top_offender",
                category="Execution",
                source="Airtable",
                value=top_offender_count,
                value_text=top_offender_text,
                status=_threshold(top_offender_count or 0, warn=3, critical=7),
            )
        )

    return snapshots


def _threshold(value: float, warn: float, critical: float) -> str:
    if value >= critical:
        return "Critical"
    if value >= warn:
        return "Warning"
    return "OK"
