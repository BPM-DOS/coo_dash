"""
collectors/leasing.py

Metrics from Appfolio Database:
  - leasing_velocity : New leads + applications in the past 7 days
  - uum_growth       : Current active unit count vs. 30 days prior (stored in Secondary Value)
"""

from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta

from pyairtable import Api

from airtable_writer import MetricSnapshot


AFP_BASE_ID = os.environ.get("AFP_BASE_ID", "appg8MZ0eQP6CFyfZ")


def collect(api_key: str) -> list[MetricSnapshot]:
    afp_key = os.environ.get("AFP_API_KEY") or api_key
    api = Api(afp_key)
    afp = api.base(AFP_BASE_ID)

    now = datetime.now(timezone.utc)
    cutoff_7d = (now - timedelta(days=7)).strftime("%Y-%m-%d")

    # --- leasing_velocity ---
    # Count active leads whose StageUpdatedAt is within the past 7 days.
    # StageUpdatedAt reflects when the lead actually moved/was active in AppFolio,
    # not when the ETL synced the record to Airtable.
    leads_table = afp.table("Leads (Spine)")
    recent_leads = leads_table.all(
        fields=["InquiryID", "IsActive", "StageUpdatedAt"],
        formula=f"AND({{IsActive}}, IS_AFTER({{StageUpdatedAt}}, '{cutoff_7d}'))",
    )
    leasing_velocity = len(recent_leads)

    # --- uum_growth ---
    # Current active unit count
    units_table = afp.table("Units (Spine)")
    active_units = units_table.all(
        fields=["ExternalID"],
        formula="{IsActive}",
    )
    uum_count = len(active_units)

    return [
        MetricSnapshot(
            metric="leasing_velocity",
            category="Weekly KPI",
            source="Airtable",
            value=float(leasing_velocity),
            secondary_value=7.0,  # rolling window in days
            status="OK" if leasing_velocity > 0 else "Warning",
        ),
        MetricSnapshot(
            metric="uum_growth",
            category="Monthly KPI",
            source="Airtable",
            value=float(uum_count),
            # Secondary Value = raw count for trend tracking; delta computed in Softr
            # by comparing today's Value to prior period's Value in the time-series table
        ),
    ]
