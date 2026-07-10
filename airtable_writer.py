"""
airtable_writer.py

Three write modes matching the three COO Dashboard tables:

  LiveWriter    — COO Live Metrics   : upserts by Metric only (overwrites in place, no date key)
  WeeklyWriter  — COO Weekly Metrics : upserts by Metric + Week Of
  MonthlyWriter — COO Monthly Metrics: upserts by Metric + Month
"""

from __future__ import annotations

import os
from datetime import datetime, timezone, date
from typing import Any

from pyairtable import Api


BPM_BASE_ID        = os.environ.get("BPM_BASE_ID",         "apprp203tCiyHl6Dw")
LIVE_TABLE_ID      = os.environ.get("COO_LIVE_TABLE_ID",   "tblLn8gzdV4fbkL9v")
WEEKLY_TABLE_ID    = os.environ.get("COO_WEEKLY_TABLE_ID", "tblwSbHsE0SR3slaU")
MONTHLY_TABLE_ID   = os.environ.get("COO_MONTHLY_TABLE_ID","tblEAjRK2PO5MdNJ0")


# ---------------------------------------------------------------------------
# MetricSnapshot — shared data container for all three writers
# ---------------------------------------------------------------------------

class MetricSnapshot:
    def __init__(
        self,
        metric: str,
        category: str,
        source: str,
        value: float | None = None,
        value_text: str | None = None,
        secondary_value: float | None = None,
        status: str | None = None,
        detail: str | None = None,
        target: float | None = None,
        period_date: date | None = None,   # week start or month start
    ):
        self.metric         = metric
        self.category       = category
        self.source         = source
        self.value          = value
        self.value_text     = value_text
        self.secondary_value = secondary_value
        self.status         = status
        self.detail         = detail
        self.target         = target
        self.period_date    = period_date or _today()

    def _base_fields(self) -> dict[str, Any]:
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        fields: dict[str, Any] = {
            "Metric":       self.metric,
            "Source":       self.source,
            "Last Updated": now_str,
        }
        if self.value          is not None: fields["Value"]          = round(self.value, 2)
        if self.value_text     is not None: fields["Value (Text)"]   = self.value_text
        if self.secondary_value is not None: fields["Secondary Value"] = round(self.secondary_value, 2)
        if self.status         is not None: fields["Status"]         = self.status
        if self.detail         is not None: fields["Detail"]         = self.detail
        if self.target         is not None: fields["Target"]         = round(self.target, 2)
        return fields

    def to_live_fields(self) -> dict[str, Any]:
        """Live table: keyed on Metric only."""
        return {"Record ID": self.metric, **self._base_fields()}

    def to_weekly_fields(self) -> dict[str, Any]:
        """Weekly table: keyed on metric-YYYY-MM-DD (Monday of the week)."""
        record_id = f"{self.metric}-{self.period_date.isoformat()}"
        return {
            "Record ID": record_id,
            "Week Of":   self.period_date.isoformat(),
            **self._base_fields(),
        }

    def to_monthly_fields(self) -> dict[str, Any]:
        """Monthly table: keyed on metric-YYYY-MM-01."""
        month_start = self.period_date.replace(day=1)
        record_id = f"{self.metric}-{month_start.isoformat()}"
        return {
            "Record ID": record_id,
            "Month":     month_start.isoformat(),
            **self._base_fields(),
        }


# ---------------------------------------------------------------------------
# Base writer — shared upsert logic
# ---------------------------------------------------------------------------

class _BaseWriter:
    def __init__(self, api_key: str | None, table_id: str):
        key = api_key or os.environ.get("AIRTABLE_API_KEY")
        if not key:
            raise RuntimeError("Missing AIRTABLE_API_KEY")
        self._table = Api(key).base(BPM_BASE_ID).table(table_id)

    def _upsert(self, records: list[dict]) -> dict:
        if not records:
            return {"upserted": 0, "created": 0, "updated": 0}

        incoming_ids = [r["fields"]["Record ID"] for r in records]
        or_clauses = ", ".join(f"{{Record ID}} = '{rid}'" for rid in incoming_ids)
        formula = f"OR({or_clauses})" if len(incoming_ids) > 1 else f"{{Record ID}} = '{incoming_ids[0]}'"
        existing = self._table.all(fields=["Record ID"], formula=formula)
        existing_map = {
            rec["fields"]["Record ID"]: rec["id"]
            for rec in existing
            if rec.get("fields", {}).get("Record ID")
        }

        to_create, to_update = [], []
        for rec in records:
            airtable_id = existing_map.get(rec["fields"]["Record ID"])
            if airtable_id:
                to_update.append({"id": airtable_id, "fields": rec["fields"]})
            else:
                to_create.append(rec["fields"])

        created = updated = 0
        if to_create:
            self._table.batch_create(to_create, typecast=True)
            created = len(to_create)
        if to_update:
            self._table.batch_update(to_update, typecast=True)
            updated = len(to_update)

        return {"upserted": created + updated, "created": created, "updated": updated}


# ---------------------------------------------------------------------------
# Three concrete writers
# ---------------------------------------------------------------------------

class LiveWriter(_BaseWriter):
    """Overwrites live metric rows in place. No date — always current."""
    def __init__(self, api_key: str | None = None):
        super().__init__(api_key, LIVE_TABLE_ID)

    def upsert(self, snapshots: list[MetricSnapshot]) -> dict:
        records = [{"fields": s.to_live_fields()} for s in snapshots]
        return self._upsert(records)


class WeeklyWriter(_BaseWriter):
    """Upserts one row per metric per week."""
    def __init__(self, api_key: str | None = None):
        super().__init__(api_key, WEEKLY_TABLE_ID)

    def upsert(self, snapshots: list[MetricSnapshot]) -> dict:
        records = [{"fields": s.to_weekly_fields()} for s in snapshots]
        return self._upsert(records)


class MonthlyWriter(_BaseWriter):
    """Upserts one row per metric per month."""
    def __init__(self, api_key: str | None = None):
        super().__init__(api_key, MONTHLY_TABLE_ID)

    def upsert(self, snapshots: list[MetricSnapshot]) -> dict:
        records = [{"fields": s.to_monthly_fields()} for s in snapshots]
        return self._upsert(records)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _today() -> date:
    tz_name = os.environ.get("TZ", "America/New_York")
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tz_name)).date()
    except Exception:
        return datetime.now(timezone.utc).date()
