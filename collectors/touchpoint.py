"""
collectors/touchpoint.py

Metrics from the Touchpoint Tracker table in BPM Operations Hub.

Live metrics (collect_live):
  - errors_yesterday        : Open staff_error + vendor_error records (value=yesterday's new,
                              secondary=total backlog)
  - new_escalations_today   : High/Medium-severity owner/resident concerns from today or yesterday
  - escalations_untouched   : Open High/Medium-severity owner/resident with no activity in 48
                              biz-hours OR no Assigned Reviewer
  - repeat_errors           : Error categories with 2+ incidents in the last 7 days

Weekly metric (collect_weekly):
  - repeat_offenders        : Entities (staff, vendors, owners, residents) appearing in
                              2+ records within 7 days OR 3+ records within 90 days.
                              Skips process_failure (no reliable entity to track).

Category mapping (Airtable stores Title Case, we normalize):
  "Process Failure" → process_failure   (excluded from repeat offenders)
  "Staff Error"     → staff_error       → entity: Involved Staff linked records
  "Vendor Error"    → vendor_error      → entity: Involved Parties text + fuzzy match vs Vendors (Spine)
  "Owner Concern"   → owner_concern     → entity: Involved Parties text
  "Resident Concern"→ resident_concern  → entity: Involved Parties text

Escalation signal:
  Severity IN ("High", "Medium") AND Category IN (owner_concern, resident_concern)
  OR Response Type includes "Escalation" (once that choice is added to the field)

"Not touched" signal (in priority order):
  1. Last Activity Date rollup field (if created by user in Airtable)
  2. Status Last Modified formula field (fallback)
  3. Also separately flag: Assigned Reviewer is blank
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from datetime import datetime, timezone, timedelta, date
from difflib import get_close_matches

from pyairtable import Api

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

from airtable_writer import MetricSnapshot, _today


BPM_BASE_ID = os.environ.get("BPM_BASE_ID", "apprp203tCiyHl6Dw")
AFP_BASE_ID = os.environ.get("AFP_BASE_ID", "appg8MZ0eQP6CFyfZ")
TT_TABLE    = "tbl1FAj8ZRUlr3tJY"

ET = ZoneInfo("America/New_York")
BIZ_START, BIZ_END = 9, 17

OPEN_STATUSES          = {"New", "Under Review", "In Progress"}
ERROR_CATEGORIES       = {"staff_error", "vendor_error"}
ESCALATION_CATEGORIES  = {"owner_concern", "resident_concern"}
ESCALATION_SEVERITIES  = {"High", "Medium"}
ESCALATION_RESP_TYPES  = {"Escalation"}

REPEAT_MIN_7D  = 2
REPEAT_MIN_90D = 3
DETAIL_CAP     = 30

VENDOR_MATCH_CUTOFF = 0.75  # fuzzy match threshold for vendor name matching


# ---------------------------------------------------------------------------
# Business hours helper (self-contained — mirrors live.py)
# ---------------------------------------------------------------------------

def _effective_biz_start(dt: datetime) -> datetime | None:
    if dt is None:
        return None
    dt_et = dt.astimezone(ET)
    weekday, hour = dt_et.weekday(), dt_et.hour
    if weekday < 5 and BIZ_START <= hour < BIZ_END:
        return dt_et
    d = dt_et.date()
    if weekday < 5 and hour < BIZ_START:
        return datetime(d.year, d.month, d.day, BIZ_START, 0, 0, tzinfo=ET)
    if weekday == 4:   days_fwd = 3
    elif weekday == 5: days_fwd = 2
    elif weekday == 6: days_fwd = 1
    else:              days_fwd = 1
    nb = d + timedelta(days=days_fwd)
    return datetime(nb.year, nb.month, nb.day, BIZ_START, 0, 0, tzinfo=ET)


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None


def _normalize_category(raw) -> str:
    if isinstance(raw, list):
        raw = raw[0] if raw else ""
    if isinstance(raw, dict):
        raw = raw.get("name", "")
    return (raw or "").lower().replace(" ", "_")


def _is_escalation(f: dict) -> bool:
    """True if this record is an escalation by severity or response type."""
    cat      = _normalize_category(f.get("Category") or "")
    severity = (f.get("Severity", {}).get("name", "") if isinstance(f.get("Severity"), dict) else f.get("Severity") or "").strip()
    resp     = {(r.get("name", "") if isinstance(r, dict) else r) for r in (f.get("Response Type") or [])}
    return (
        (cat in ESCALATION_CATEGORIES and severity in ESCALATION_SEVERITIES)
        or bool(resp & ESCALATION_RESP_TYPES)
    )


def _last_touched_dt(f: dict) -> datetime | None:
    """Try Last Activity Date rollup first, fall back to Status Last Modified."""
    raw = f.get("Last Activity Date") or f.get("Status Last Modified")
    return _parse_dt(raw)


# ---------------------------------------------------------------------------
# Live metrics
# ---------------------------------------------------------------------------

def collect_live(api_key: str) -> list[MetricSnapshot]:
    bpm_key = api_key
    table   = Api(bpm_key).base(BPM_BASE_ID).table(TT_TABLE)

    records = table.all(
        fields=[
            "Summary", "Category", "Severity", "Status", "Response Type",
            "Date of Incident", "Assigned Reviewer", "Staff Name",
            "Involved Parties", "Placeholder?",
            "Last Activity Date", "Status Last Modified",
        ],
        formula="NOT({Placeholder?})",
    )

    now          = datetime.now(timezone.utc)
    cutoff_48h   = now - timedelta(hours=48)
    today_et     = datetime.now(ET).date()
    yesterday_et = today_et - timedelta(days=1)

    errors_yesterday: list[str]  = []  # open errors dated today or yesterday
    errors_backlog: list[str]    = []  # all open errors regardless of date
    escalations_old: list[str]   = []
    new_esc_today: list[str]     = []
    no_reviewer: list[str]       = []
    cutoff_7d = today_et - timedelta(days=7)

    for rec in records:
        f       = rec.get("fields", {})
        cat_raw = f.get("Category") or ""
        cat     = _normalize_category(cat_raw)
        status  = (f.get("Status", {}).get("name", "") if isinstance(f.get("Status"), dict) else f.get("Status") or "").strip()
        summary = f.get("Summary") or rec["id"]
        is_open = status in OPEN_STATUSES

        if not is_open:
            continue

        # --- errors (Q8: append staff names; Q9: split yesterday vs backlog) ---
        if cat in ERROR_CATEGORIES:
            # Staff Name is a lookup field — returns a list of name strings
            staff_name_raw = f.get("Staff Name") or []
            if isinstance(staff_name_raw, str):
                staff_name_raw = [staff_name_raw]
            staff_names = [s.strip() for s in staff_name_raw if s and str(s).strip()]
            label = f"{summary} [{cat_raw}]"
            if staff_names:
                label = f"{label} — {', '.join(staff_names)}"

            errors_backlog.append(label)

            incident_date_str = f.get("Date of Incident")
            if incident_date_str:
                try:
                    incident_date = date.fromisoformat(incident_date_str)
                    if incident_date in (today_et, yesterday_et):
                        errors_yesterday.append(label)
                except ValueError:
                    pass

        # --- escalation checks ---
        if _is_escalation(f):
            # Q8: append Involved Parties to escalation summaries
            involved = (f.get("Involved Parties") or "").strip()
            esc_label = f"{summary} — {involved}" if involved else summary

            # New escalations today/yesterday
            incident_date_str = f.get("Date of Incident")
            if incident_date_str:
                try:
                    incident_date = date.fromisoformat(incident_date_str)
                    if incident_date in (today_et, yesterday_et):
                        new_esc_today.append(esc_label)
                except ValueError:
                    pass

            # Not touched in 48 biz-hours
            last_dt  = _last_touched_dt(f)
            biz_last = _effective_biz_start(last_dt) if last_dt else None
            has_reviewer = bool(f.get("Assigned Reviewer"))

            if not has_reviewer:
                no_reviewer.append(esc_label)
            elif biz_last and biz_last < cutoff_48h:
                escalations_old.append(esc_label)

    def _detail(items):
        if not items:
            return None
        shown = items[:DETAIL_CAP]
        tail  = f"\n… and {len(items) - DETAIL_CAP} more" if len(items) > DETAIL_CAP else ""
        return "\n".join(shown) + tail

    untouched = escalations_old + no_reviewer  # combine: stale + unassigned

    # Q9: value_text for errors_yesterday
    n_yesterday = len(errors_yesterday)
    n_backlog   = len(errors_backlog)
    if n_yesterday > 0:
        errors_value_text = f"{n_yesterday} new yesterday ({n_backlog} open total)"
    elif n_backlog > 0:
        errors_value_text = f"{n_backlog} open total"
    else:
        errors_value_text = None

    if n_yesterday > 0:
        errors_status = "Critical"
    elif n_backlog > 0:
        errors_status = "Warning"
    else:
        errors_status = "OK"

    # Q10: repeat_errors — count error categories with 2+ records in last 7 days
    error_cat_counts: dict[str, int] = defaultdict(int)
    for rec in records:
        f        = rec.get("fields", {})
        cat_raw  = (f.get("Category", {}).get("name", "") if isinstance(f.get("Category"), dict) else f.get("Category") or "")
        cat      = _normalize_category(cat_raw)
        if cat not in ERROR_CATEGORIES:
            continue
        date_str = f.get("Date of Incident") or ""
        if not date_str:
            continue
        try:
            d = date.fromisoformat(date_str)
        except ValueError:
            continue
        if d >= cutoff_7d:
            error_cat_counts[cat] += 1

    flagged_error_cats = {c: cnt for c, cnt in error_cat_counts.items() if cnt >= 2}
    repeat_detail_lines = [f"{c}: {cnt} in last 7d" for c, cnt in sorted(flagged_error_cats.items())]

    return [
        MetricSnapshot(
            metric="errors_yesterday",
            category="Errors",
            source="Airtable",
            value=float(n_yesterday),
            secondary_value=float(n_backlog),
            detail=_detail(errors_backlog),
            value_text=errors_value_text,
            status=errors_status,
        ),
        MetricSnapshot(
            metric="new_escalations_today",
            category="Active Fires",
            source="Airtable",
            value=float(len(new_esc_today)),
            detail=_detail(new_esc_today),
            status="Critical" if new_esc_today else "OK",
        ),
        MetricSnapshot(
            metric="escalations_untouched_48h",
            category="Active Fires",
            source="Airtable",
            value=float(len(untouched)),
            secondary_value=float(len(no_reviewer)),   # how many have no reviewer at all
            detail=_detail(untouched),
            status=_threshold(len(untouched), warn=1, critical=3),
        ),
        MetricSnapshot(
            metric="repeat_errors",
            category="Active Fires",
            source="Airtable",
            value=float(len(flagged_error_cats)),
            detail="\n".join(repeat_detail_lines) if repeat_detail_lines else None,
            status=_threshold(len(flagged_error_cats), warn=1, critical=2),
        ),
    ]


# ---------------------------------------------------------------------------
# Weekly metric — Repeat Offenders
# ---------------------------------------------------------------------------

def collect_weekly(api_key: str) -> list[MetricSnapshot]:
    bpm_key  = api_key
    afp_key  = os.environ.get("AFP_API_KEY") or api_key

    now       = datetime.now(timezone.utc)
    cutoff_7  = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    cutoff_90 = (now - timedelta(days=90)).strftime("%Y-%m-%d")
    today_date = _today()
    week_date  = today_date  # period anchor for weekly snapshot

    # Fetch all TT records from last 90 days
    tt_table = Api(bpm_key).base(BPM_BASE_ID).table(TT_TABLE)
    records  = tt_table.all(
        fields=[
            "Category", "Status", "Date of Incident",
            "Staff Name", "Involved Parties", "Placeholder?",
        ],
        formula=f"AND(NOT({{Placeholder?}}), IS_AFTER({{Date of Incident}}, '{cutoff_90}'))",
    )

    # Load vendor names from Appfolio for fuzzy matching
    vendor_names = _load_vendor_names(afp_key)

    # Collect (entity_key, category, date_str) tuples
    incidents: list[tuple[str, str, str]] = []

    for rec in records:
        f       = rec.get("fields", {})
        cat_raw = f.get("Category") or ""
        cat     = _normalize_category(cat_raw)
        date_str = f.get("Date of Incident") or ""

        if not date_str or cat == "process_failure":
            continue

        entities = _extract_entities(f, cat, vendor_names, bpm_key)
        for entity in entities:
            incidents.append((entity, cat, date_str))

    # Count per entity: 7d and 90d windows
    counts_7:  dict[str, int] = defaultdict(int)
    counts_90: dict[str, int] = defaultdict(int)

    for entity, cat, date_str in incidents:
        try:
            d = date.fromisoformat(date_str)
        except ValueError:
            continue
        key = f"{entity} ({cat})"
        if d >= date.fromisoformat(cutoff_7):
            counts_7[key] += 1
        counts_90[key] += 1

    # Apply thresholds: 2+ in 7d OR 3+ in 90d
    flagged: dict[str, str] = {}
    for key, cnt_90 in counts_90.items():
        cnt_7 = counts_7.get(key, 0)
        if cnt_7 >= REPEAT_MIN_7D:
            flagged[key] = f"{key}: {cnt_7}x in 7d"
        elif cnt_90 >= REPEAT_MIN_90D:
            flagged[key] = f"{key}: {cnt_90}x in 90d"

    detail_lines = sorted(flagged.values())

    return [MetricSnapshot(
        metric="repeat_offenders",
        category="Errors",
        source="Airtable",
        value=float(len(flagged)),
        detail="\n".join(detail_lines[:DETAIL_CAP]) if detail_lines else None,
        status=_threshold(len(flagged), warn=1, critical=3),
        period_date=week_date,
    )]


# ---------------------------------------------------------------------------
# Entity extraction helpers
# ---------------------------------------------------------------------------

def _extract_entities(f: dict, cat: str, vendor_names: list[str], api_key: str) -> list[str]:
    """Return a list of entity identifiers for a TT record based on its category."""
    entities = []

    if cat == "staff_error":
        # Use Staff Name lookup field (returns list of name strings)
        staff_name_raw = f.get("Staff Name") or []
        if isinstance(staff_name_raw, str):
            staff_name_raw = [staff_name_raw]
        for name in staff_name_raw:
            name = str(name).strip()
            if name:
                entities.append(name)

    elif cat == "vendor_error":
        # Fuzzy match Involved Parties text against known vendor names
        raw = (f.get("Involved Parties") or "").strip()
        if raw and vendor_names:
            matched = get_close_matches(raw, vendor_names, n=1, cutoff=VENDOR_MATCH_CUTOFF)
            entities.append(matched[0] if matched else raw)
        elif raw:
            entities.append(raw)

    elif cat in ("owner_concern", "resident_concern"):
        # Use Involved Parties free text (normalize whitespace)
        raw = (f.get("Involved Parties") or "").strip()
        if raw:
            # Normalize: lowercase, collapse whitespace
            normalized = re.sub(r"\s+", " ", raw.lower()).strip()
            entities.append(normalized)

    return entities


def _load_vendor_names(afp_key: str) -> list[str]:
    """Fetch vendor names from Vendors (Spine) in Appfolio Database for fuzzy matching."""
    try:
        vendors = Api(afp_key).base(AFP_BASE_ID).table("Vendors (Spine)").all(
            fields=["VendorName"],
            formula="{IsActive}",
        )
        return [
            rec["fields"]["VendorName"].strip()
            for rec in vendors
            if rec.get("fields", {}).get("VendorName")
        ]
    except Exception as e:
        print(f"  [touchpoint] Could not load vendor names: {e}")
        return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _threshold(value: float, warn: float, critical: float) -> str:
    if value >= critical: return "Critical"
    if value >= warn:     return "Warning"
    return "OK"
