"""
collectors/monthly.py

Monthly metrics — written on the 1st of each month.
period_date = 1st of the current month.

  - owner_churn  : YTD cumulative owners lost, as % of Jan 1 baseline. Target <6%.
  - uum_growth   : New units added this month (ManagementStartDate in current month),
                   with total active UUM as Secondary Value.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from pyairtable import Api

from airtable_writer import MetricSnapshot, _today


AFP_BASE_ID     = os.environ.get("AFP_BASE_ID", "appg8MZ0eQP6CFyfZ")
CHURN_TARGET    = 6.0
OWNER_COUNT_JAN1 = int(os.environ.get("OWNER_COUNT_JAN1", "0"))


def collect(api_key: str) -> list[MetricSnapshot]:
    afp_key = os.environ.get("AFP_API_KEY") or api_key
    api = Api(afp_key)

    today = _today()
    month_start = today.replace(day=1)
    year_start  = today.replace(month=1, day=1).isoformat()

    owners_table = api.base(AFP_BASE_ID).table("Owners (Spine)")

    # YTD churned
    churned = owners_table.all(
        fields=["ExternalID", "InactiveAt"],
        formula=f"AND({{IsActive}} = FALSE(), IS_AFTER({{InactiveAt}}, '{year_start}'))",
    )
    churn_count = len(churned)

    # Current active
    active = owners_table.all(fields=["ExternalID"], formula="{IsActive}")
    total_active = len(active)

    jan1 = OWNER_COUNT_JAN1 if OWNER_COUNT_JAN1 > 0 else (total_active + churn_count)
    churn_pct = round(churn_count / jan1 * 100, 2) if jan1 > 0 else 0.0
    allowable = max(0, round(jan1 * CHURN_TARGET / 100) - churn_count)

    # UUM Growth — new units whose property came under management this month
    units_table = api.base(AFP_BASE_ID).table("Units (Spine)")
    all_active = units_table.all(
        fields=["ExternalID", "ManagementStartDate (from Property)"],
        formula="{IsActive}",
    )
    total_uum = len(all_active)

    new_this_month = 0
    for rec in all_active:
        mgmt_dates = rec["fields"].get("ManagementStartDate (from Property)") or []
        if isinstance(mgmt_dates, str):
            mgmt_dates = [mgmt_dates]
        for d in mgmt_dates:
            try:
                from datetime import date as date_type
                mgmt_date = date_type.fromisoformat(str(d)[:10])
                if mgmt_date.year == month_start.year and mgmt_date.month == month_start.month:
                    new_this_month += 1
                    break
            except (ValueError, TypeError):
                continue

    return [
        MetricSnapshot(
            metric="owner_churn",
            category="Monthly KPI",
            source="Airtable",
            value=float(churn_count),
            secondary_value=churn_pct,
            value_text=f"{churn_count} lost YTD ({churn_pct}% of {jan1})",
            target=CHURN_TARGET,
            detail="\n".join([
                f"YTD churned: {churn_count}",
                f"Current active: {total_active}",
                f"Year-start baseline: {jan1}",
                f"Remaining allowable at <{CHURN_TARGET}%: {allowable}",
            ]),
            status="Critical" if churn_pct >= CHURN_TARGET else ("Warning" if churn_pct >= CHURN_TARGET * 0.75 else "OK"),
            period_date=month_start,
        ),
        MetricSnapshot(
            metric="uum_growth",
            category="Monthly KPI",
            source="Airtable",
            value=float(new_this_month),
            secondary_value=float(total_uum),
            value_text=f"+{new_this_month} new this month ({total_uum} total active)",
            status="OK" if new_this_month > 0 else "Warning",
            period_date=month_start,
        ),
    ]
