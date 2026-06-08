"""
collectors/ownership.py

Metrics from Appfolio Database:
  - owner_churn : YTD cumulative owners lost, as a % of owners at start of year.
                  Target: <6% annual churn. Set OWNER_COUNT_JAN1 in .env to the
                  number of active owners on Jan 1 of the current year.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from pyairtable import Api

from airtable_writer import MetricSnapshot, _today


AFP_BASE_ID = os.environ.get("AFP_BASE_ID", "appg8MZ0eQP6CFyfZ")

# Target: lose no more than 6% of owners in a year
CHURN_TARGET_PCT = 6.0

# Set this in .env once you know your Jan 1 owner count.
# If not set, we fall back to (current active + YTD churned) as the denominator.
OWNER_COUNT_JAN1 = int(os.environ.get("OWNER_COUNT_JAN1", "0"))


def collect(api_key: str) -> list[MetricSnapshot]:
    afp_key = os.environ.get("AFP_API_KEY") or api_key
    api = Api(afp_key)
    owners_table = api.base(AFP_BASE_ID).table("Owners (Spine)")

    today = _today()
    year_start = today.replace(month=1, day=1).isoformat()

    # YTD churned: inactive owners whose InactiveAt is in the current calendar year
    churned = owners_table.all(
        fields=["ExternalID", "InactiveAt"],
        formula=(
            f"AND("
            f"{{IsActive}} = FALSE(), "
            f"IS_AFTER({{InactiveAt}}, '{year_start}')"
            f")"
        ),
    )
    churn_count = len(churned)

    # Current active owners
    active_owners = owners_table.all(
        fields=["ExternalID"],
        formula="{IsActive}",
    )
    total_active = len(active_owners)

    # Denominator: Jan 1 count if set, otherwise best estimate
    jan1_count = OWNER_COUNT_JAN1 if OWNER_COUNT_JAN1 > 0 else (total_active + churn_count)

    churn_pct = round(churn_count / jan1_count * 100, 2) if jan1_count > 0 else 0.0

    # How many owners can we lose before hitting 6%?
    allowable_losses = max(0, round(jan1_count * CHURN_TARGET_PCT / 100) - churn_count)

    detail_lines = [
        f"YTD churned: {churn_count} owners",
        f"Current active: {total_active}",
        f"Year-start baseline: {jan1_count}",
        f"Remaining allowable losses at <{CHURN_TARGET_PCT}% target: {allowable_losses}",
    ]

    return [
        MetricSnapshot(
            metric="owner_churn",
            category="Monthly KPI",
            source="Airtable",
            value=float(churn_count),
            secondary_value=churn_pct,
            value_text=f"{churn_count} owners lost YTD ({churn_pct}% of {jan1_count})",
            target=CHURN_TARGET_PCT,
            detail="\n".join(detail_lines),
            status=_churn_status(churn_pct),
        ),
    ]


def _churn_status(pct: float) -> str:
    if pct >= CHURN_TARGET_PCT:
        return "Critical"
    if pct >= CHURN_TARGET_PCT * 0.75:  # 75% of the way to the limit = Warning
        return "Warning"
    return "OK"
