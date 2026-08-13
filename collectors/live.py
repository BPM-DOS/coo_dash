"""
collectors/live.py

All live metrics — refreshed every 30 minutes.
Pulls from: Melds (Spine), Tasks, Missive.

  Maintenance Stalls (all exclude project melds; clocks start at next business hour):
    - wo_not_triaged_24h    : Active melds still PENDING_ASSIGNMENT > 24 biz-hours
    - stalled_wo_72h        : Active melds with no update in 72h (business-hour adjusted)
    - time_to_triage_hours  : Rolling 30d avg business-hours from creation → assignment

  Execution:
    - past_due_tasks        : Tasks where Task Due = "Past Due"

  Communication:
    - inboxes_over_50       : Individual staff members with > 50 open conversations (incl. team inbox assigned/unassigned)
"""

from __future__ import annotations

import os
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta, date

import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from pyairtable import Api

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

from airtable_writer import MetricSnapshot, _today


AFP_BASE_ID = os.environ.get("AFP_BASE_ID", "appg8MZ0eQP6CFyfZ")
BPM_BASE_ID = os.environ.get("BPM_BASE_ID", "apprp203tCiyHl6Dw")
MISSIVE_TOKEN = os.environ.get("MISSIVE_TOKEN", "")
MISSIVE_BASE = "https://public.missiveapp.com/v1"
INBOX_THRESHOLD = int(os.environ.get("MISSIVE_INBOX_THRESHOLD", "50"))

ET = ZoneInfo("America/New_York")
BIZ_START = 9   # 9am ET
BIZ_END   = 17  # 5pm ET

UNTRIAGED_STATUS = "PENDING_ASSIGNMENT"
CLOSED_STATUSES  = {"COMPLETED", "MANAGER_CANCELED", "TENANT_CANCELED", "CANCELLED", "CANCELED"}
DETAIL_CAP = 50

# Service/bot accounts and former staff to exclude from individual inbox counts
SKIP_USER_NAMES = {"BPM-DOS Team", "Marchenka White", "Jeff Stoddard"}


def collect(api_key: str) -> list[MetricSnapshot]:
    snapshots = []
    snapshots += _maintenance(api_key)
    snapshots += _execution(api_key)
    snapshots += _missive()
    return snapshots


# ---------------------------------------------------------------------------
# Business hours helper
# ---------------------------------------------------------------------------

def effective_biz_start(dt: datetime) -> datetime:
    """
    If dt falls during business hours (Mon-Fri 9am-5pm ET), return dt unchanged.
    If dt is before 9am on a weekday, return 9am that day.
    If dt is after 5pm or on a weekend, return 9am ET of the next business day.
    """
    if dt is None:
        return None
    dt_et = dt.astimezone(ET)
    weekday = dt_et.weekday()  # 0=Mon … 6=Sun
    hour    = dt_et.hour

    if weekday < 5 and BIZ_START <= hour < BIZ_END:
        return dt_et  # already in business hours

    d = dt_et.date()

    if weekday < 5 and hour < BIZ_START:
        # Before 9am on a weekday — same day open
        return datetime(d.year, d.month, d.day, BIZ_START, 0, 0, tzinfo=ET)

    # After hours or weekend
    if weekday == 4:    # Friday after hours → Monday
        days_forward = 3
    elif weekday == 5:  # Saturday → Monday
        days_forward = 2
    elif weekday == 6:  # Sunday → Monday
        days_forward = 1
    else:               # Mon–Thu after hours → next day
        days_forward = 1

    next_biz = d + timedelta(days=days_forward)
    return datetime(next_biz.year, next_biz.month, next_biz.day, BIZ_START, 0, 0, tzinfo=ET)


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------

def _maintenance(api_key: str) -> list[MetricSnapshot]:
    afp_key = os.environ.get("AFP_API_KEY") or api_key
    table = Api(afp_key).base(AFP_BASE_ID).table("Melds (Spine)")

    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)
    cutoff_72h = now - timedelta(hours=72)
    cutoff_30d = now - timedelta(days=30)

    # Exclude project-linked melds and turn work orders
    records = table.all(
        fields=["Status", "CreatedAt", "AssignedAt", "UpdatedAt",
                "IsActive", "ReferenceID", "BriefDescription", "Origin",
                "CoordinatorFirstName", "CoordinatorLastName"],
        formula="AND({IsActive}, OR({ProjectID} = '', {ProjectID} = BLANK()), {WorkCategory} != 'TURNOVER')",
    )

    not_triaged_refs, stalled_refs, triage_durations = [], [], []

    for rec in records:
        f = rec.get("fields", {})
        status      = (f.get("Status") or "").upper()
        created_at  = _parse_dt(f.get("CreatedAt"))
        assigned_at = _parse_dt(f.get("AssignedAt"))
        updated_at  = _parse_dt(f.get("UpdatedAt"))
        ref_id      = f.get("ReferenceID") or ""
        brief       = f.get("BriefDescription") or ""
        coord_first = (f.get("CoordinatorFirstName") or "").strip()
        coord_last  = (f.get("CoordinatorLastName") or "").strip()
        coord = " ".join(filter(None, [coord_first, coord_last]))
        label = f"{ref_id} — {brief}" if ref_id and brief else (ref_id or brief or rec["id"])
        if coord:
            label += f" [{coord}]"

        if status in CLOSED_STATUSES:
            continue

        # --- wo_not_triaged_24h ---
        # Clock starts at next business open if submitted after hours
        if status == UNTRIAGED_STATUS and created_at:
            biz_start = effective_biz_start(created_at)
            if biz_start and biz_start < cutoff_24h:
                not_triaged_refs.append(label)

        # --- stalled_wo_72h ---
        # Last activity (UpdatedAt) business-hours adjusted; catches truly stuck melds
        last_activity = updated_at or assigned_at or created_at
        if last_activity:
            biz_last = effective_biz_start(last_activity)
            if biz_last and biz_last < cutoff_72h:
                stalled_refs.append(label)

        # --- time_to_triage (rolling 30d, resident-submitted) ---
        origin = (f.get("Origin") or "").upper()
        is_resident = origin in ("TENANT", "RESIDENT", "")
        if is_resident and created_at and assigned_at and created_at >= cutoff_30d:
            biz_created = effective_biz_start(created_at)
            if biz_created:
                hours = (assigned_at - biz_created).total_seconds() / 3600
                if 0 <= hours < 720:
                    triage_durations.append(hours)

    def _detail(refs):
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

    if triage_durations:
        avg = round(sum(triage_durations) / len(triage_durations), 2)
        snapshots.append(MetricSnapshot(
            metric="time_to_triage_hours",
            category="Maintenance Stalls",
            source="Airtable",
            value=avg,
            secondary_value=float(len(triage_durations)),
            status=_threshold(avg, warn=12, critical=24),
        ))

    return snapshots


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def _execution(api_key: str) -> list[MetricSnapshot]:
    """Use the Task Due formula field — value is 'Past Due' for overdue tasks."""
    table = Api(api_key).base(BPM_BASE_ID).table("Tasks")

    records = table.all(
        fields=["Task Due", "Task Name", "Status"],
        formula=(
            "AND("
            "{Task Due} = 'Past Due', "
            "{Status} != 'Complete', "
            "{Status} != 'N/A', "
            "{Status} != 'Project Cancelled'"
            ")"
        ),
    )

    return [MetricSnapshot(
        metric="past_due_tasks",
        category="Execution",
        source="Airtable",
        value=float(len(records)),
        status=_threshold(len(records), warn=5, critical=15),
    )]


# ---------------------------------------------------------------------------
# Missive — individual user unarchived conversation count
# ---------------------------------------------------------------------------

def _missive() -> list[MetricSnapshot]:
    if not MISSIVE_TOKEN:
        print("  [live/missive] MISSIVE_TOKEN not set — skipping")
        return []

    headers = {
        "Authorization": f"Bearer {MISSIVE_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        org_id = _get_org_id(headers)
        user_counts = _count_open_per_user(headers, org_id)
    except Exception as e:
        print(f"  [live/missive] failed: {e}")
        return [MetricSnapshot(
            metric="inboxes_over_50",
            category="Communication Backlog",
            source="Missive",
            value=0.0,
            value_text="None",
            status="OK",
        )]

    # user_counts is now {name: {"assigned": N, "labeled": M, "total": N+M}}
    over = {name: data["total"] for name, data in user_counts.items() if data["total"] > INBOX_THRESHOLD}
    count_over = len(over)

    # Pipe-delimited detail for structured frontend rendering: Name|total|assigned|labeled
    detail_lines = []
    for name, total in sorted(over.items(), key=lambda x: -x[1]):
        a = user_counts[name]["assigned"]
        l = user_counts[name]["labeled"]
        detail_lines.append(f"{name}|{total}|{a}|{l}")

    return [MetricSnapshot(
        metric="inboxes_over_50",
        category="Communication Backlog",
        source="Missive",
        value=float(count_over),
        value_text=", ".join(name for name in sorted(over, key=lambda n: -over[n])) if over else "None",
        detail="\n".join(detail_lines) if detail_lines else None,
        status="Critical" if count_over > 0 else "OK",
    )]


def _fetch_at_labels(headers: dict, org_id: str) -> dict:
    """
    Fetch all org labels and return {label_id: shortname} for @-prefixed ones.
    e.g. {"abc123": "joe"} for a label named "@joe".
    """
    resp = requests.get(
        f"{MISSIVE_BASE}/labels",
        headers=headers,
        params={"organization": org_id},
        timeout=15,
    )
    if resp.status_code != 200:
        print(f"  [live/missive] labels fetch returned {resp.status_code}")
        return {}
    return {
        l["id"]: l["name"][1:].lower()
        for l in resp.json().get("labels", [])
        if (l.get("name") or "").startswith("@")
    }


def _count_open_per_user(headers: dict, org_id: str, max_pages: int = 100) -> dict:
    """
    Page through ALL conversations and count open ones per user.
    Counts: (1) conversations explicitly assigned to them, plus
            (2) conversations carrying their personal @label (e.g. @joe).
    Deduped — a conversation that is both assigned and @labeled counts once.
    Stops paginating once conversations older than 90 days are reached.
    Returns {user_name: {"assigned": N, "labeled": M, "total": N+M}}.
    """
    at_labels = _fetch_at_labels(headers, org_id)
    print(f"  [live/missive] @-labels found: {list(at_labels.values()) or 'none'}")

    cutoff_ts = int(time.time()) - 90 * 86400

    # Use sets of convo IDs to deduplicate across assigned and labeled
    assigned_ids: dict[str, set] = defaultdict(set)
    labeled_ids: dict[str, set] = defaultdict(set)

    # first_name (lowercase) → full user name, built as we discover users
    first_to_name: dict[str, str] = {}

    params = {"organization": org_id, "all": "true", "limit": 50}

    for page in range(max_pages):
        resp = requests.get(
            f"{MISSIVE_BASE}/conversations",
            headers=headers,
            params=params,
            timeout=30,
        )
        if resp.status_code != 200:
            print(f"  [live/missive] conversations page {page+1} returned {resp.status_code}")
            break

        convos = resp.json().get("conversations", [])
        if not convos:
            break

        for convo in convos:
            convo_id = convo.get("id")
            convo_label_ids = {l["id"] for l in (convo.get("labels") or [])}

            # Register user names on this conversation first so @label matching
            # on the same conversation can find them immediately.
            for user in (convo.get("users") or []):
                name = user.get("name") or user.get("email", "")
                if name and name not in SKIP_USER_NAMES:
                    first = name.split()[0].lower()
                    first_to_name.setdefault(first, name)

            # Count assigned conversations
            for user in (convo.get("users") or []):
                name = user.get("name") or user.get("email", "")
                if not name or name in SKIP_USER_NAMES:
                    continue
                if not user.get("closed") and not user.get("trashed") and not user.get("junked"):
                    if user.get("assigned"):
                        assigned_ids[name].add(convo_id)

            # Count @labeled conversations
            if at_labels and convo_label_ids:
                for label_id, shortname in at_labels.items():
                    if label_id not in convo_label_ids:
                        continue
                    # Match shortname to a known user by first name prefix
                    matched = None
                    for first, uname in first_to_name.items():
                        if first.startswith(shortname) or shortname.startswith(first):
                            matched = uname
                            break
                    if matched:
                        labeled_ids[matched].add(convo_id)

        if len(convos) < 50:
            break

        oldest_ts = min(c.get("last_activity_at", 0) for c in convos if isinstance(c, dict))
        if oldest_ts and oldest_ts < cutoff_ts:
            break

        params = {"organization": org_id, "all": "true", "limit": 50, "until": oldest_ts}

    # Build results: labeled = conversations with @label NOT already in assigned
    result = {}
    for name in set(assigned_ids) | set(labeled_ids):
        a = assigned_ids.get(name, set())
        l = labeled_ids.get(name, set()) - a  # exclude already-assigned
        result[name] = {
            "assigned": len(a),
            "labeled": len(l),
            "total": len(a) + len(l),
        }
    return result


# ---------------------------------------------------------------------------
# Missive API helpers
# ---------------------------------------------------------------------------

def _get_org_id(headers):
    resp = requests.get(f"{MISSIVE_BASE}/organizations", headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()["organizations"][0]["id"]


def _submit_report(headers, org_id, start_ts, end_ts, filter_key, filter_id) -> str:
    payload = {"reports": {"organization": org_id, "start": start_ts, "end": end_ts,
                           "time_zone": "America/New_York", filter_key: [filter_id]}}
    resp = requests.post(f"{MISSIVE_BASE}/analytics/reports", headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()["reports"]["id"]


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
    """Submit all reports staggered, poll in parallel. Returns {name: report}."""
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


def _mv(d, key):
    m = d.get(key, {})
    return m.get("v") if isinstance(m, dict) else None


def _parse_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None


def _threshold(value, warn, critical):
    if value >= critical:
        return "Critical"
    if value >= warn:
        return "Warning"
    return "OK"
