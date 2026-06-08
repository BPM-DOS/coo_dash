#!/usr/bin/env python3
"""
scripts/rippling_sync.py

Finds the weekly Rippling utilization email in Missive, downloads the CSV
attachment, and writes Worked Hours into the BPM Scorecard
→ Billable Hour Util Rate table.

Run manually or via cron every Sunday evening after the email arrives:
    0 20 * * 0 flock -n /tmp/rippling_sync.lock python /root/coo_dash/scripts/rippling_sync.py >> /var/log/rippling_sync.log 2>&1
"""

from __future__ import annotations

import csv
import io
import os
import sys
from datetime import datetime, timezone, timedelta, date

import requests
from dotenv import load_dotenv
from pyairtable import Api

load_dotenv(override=True)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MISSIVE_TOKEN     = os.environ["MISSIVE_TOKEN"]
MISSIVE_BASE      = "https://public.missiveapp.com/v1"

AIRTABLE_API_KEY  = os.environ["AIRTABLE_API_KEY"]
SCORECARD_BASE_ID = os.environ.get("SCORECARD_BASE_ID", "appAwZySwIwQT0G0a")
UTIL_TABLE        = "Billable Hour Util Rate"

# Search terms to locate the Rippling email in Missive
RIPPLING_SENDER_DOMAIN = os.environ.get("RIPPLING_SENDER_DOMAIN", "rippling.com")
RIPPLING_SUBJECT_CONTAINS = os.environ.get("RIPPLING_SUBJECT_CONTAINS", "utilization")

MISSIVE_HEADERS = {
    "Authorization": f"Bearer {MISSIVE_TOKEN}",
    "Content-Type": "application/json",
    "Accept": "application/json",
}


# ---------------------------------------------------------------------------
# Step 1: Find the Rippling email in Missive
# ---------------------------------------------------------------------------

def find_rippling_conversation() -> dict | None:
    """
    Search Missive conversations for the Rippling utilization email
    received in the last 3 days.
    """
    # Missive conversation search — filter by recent + unarchived
    resp = requests.get(
        f"{MISSIVE_BASE}/conversations",
        headers=MISSIVE_HEADERS,
        params={
            "limit": 25,
            "all": "false",
        },
        timeout=15,
    )
    resp.raise_for_status()
    conversations = resp.json().get("conversations", [])

    for convo in conversations:
        subject = (convo.get("subject") or "").lower()
        # Match on subject containing our keyword
        if RIPPLING_SUBJECT_CONTAINS.lower() in subject:
            return convo

    # If not in open inbox, search more broadly
    resp = requests.get(
        f"{MISSIVE_BASE}/conversations",
        headers=MISSIVE_HEADERS,
        params={
            "limit": 50,
            "all": "true",
        },
        timeout=15,
    )
    resp.raise_for_status()
    conversations = resp.json().get("conversations", [])

    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    for convo in conversations:
        subject = (convo.get("subject") or "").lower()
        created_at = convo.get("created_at", 0)
        convo_dt = datetime.fromtimestamp(created_at, tz=timezone.utc)
        if RIPPLING_SUBJECT_CONTAINS.lower() in subject and convo_dt > cutoff:
            return convo

    return None


# ---------------------------------------------------------------------------
# Step 2: Get the CSV attachment from the conversation
# ---------------------------------------------------------------------------

def get_csv_attachment(conversation_id: str) -> str | None:
    """
    Fetch messages in the conversation and return the content of the first
    CSV attachment found.
    """
    resp = requests.get(
        f"{MISSIVE_BASE}/conversations/{conversation_id}/messages",
        headers=MISSIVE_HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    messages = resp.json().get("messages", [])

    for message in messages:
        attachments = message.get("attachments", [])
        for attachment in attachments:
            filename = attachment.get("filename", "")
            if filename.lower().endswith(".csv"):
                url = attachment.get("url") or attachment.get("download_url")
                if url:
                    dl = requests.get(url, headers=MISSIVE_HEADERS, timeout=30)
                    dl.raise_for_status()
                    return dl.text

    return None


# ---------------------------------------------------------------------------
# Step 3: Parse the CSV
# ---------------------------------------------------------------------------

def parse_rippling_csv(csv_text: str) -> list[dict]:
    """
    Parse the Rippling CSV and return a list of:
        {"tech_name": str, "worked_hours": float, "week_date": str (YYYY-MM-DD)}
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = []

    for row in reader:
        employee = (row.get("Employee") or "").strip()
        if not employee:
            continue

        # Payable hours — use Time entry payable time as the worked hours source
        hours_str = (row.get("Time entry payable time (hours)") or "0").strip()
        try:
            worked_hours = float(hours_str)
        except ValueError:
            continue

        if worked_hours <= 0:
            continue

        # Use Pay period start as the week key
        week_date = (row.get("Pay period start") or "").strip()
        if not week_date:
            continue

        rows.append({
            "tech_name": employee,
            "worked_hours": worked_hours,
            "week_date": week_date,
        })

    return rows


# ---------------------------------------------------------------------------
# Step 4: Write to Airtable Billable Hour Util Rate
# ---------------------------------------------------------------------------

def upsert_to_scorecard(rows: list[dict]) -> dict:
    """
    For each parsed row, upsert into Billable Hour Util Rate.
    Upsert key: Tech Name + Week date.
    """
    api = Api(AIRTABLE_API_KEY)
    table = api.base(SCORECARD_BASE_ID).table(UTIL_TABLE)

    created = updated = 0

    for row in rows:
        tech = row["tech_name"]
        week = row["week_date"]
        worked = row["worked_hours"]

        # Check if a row already exists for this tech + week
        existing = table.all(
            fields=["Tech Name", "Week", "Worked Hours"],
            formula=f"AND({{Tech Name}} = '{tech}', {{Week}} = '{week}')",
        )

        if existing:
            record_id = existing[0]["id"]
            table.update(record_id, {"Worked Hours": worked})
            print(f"  Updated: {tech} | {week} | {worked}h")
            updated += 1
        else:
            table.create({"Tech Name": tech, "Week": week, "Worked Hours": worked})
            print(f"  Created: {tech} | {week} | {worked}h")
            created += 1

    return {"created": created, "updated": updated}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"\n=== Rippling Sync — {datetime.now(timezone.utc).isoformat()} ===")

    print("Searching Missive for Rippling email...")
    convo = find_rippling_conversation()
    if not convo:
        print(f"ERROR: No Rippling email found matching '{RIPPLING_SUBJECT_CONTAINS}' in last 7 days.")
        print("Check RIPPLING_SUBJECT_CONTAINS in .env — should match part of the email subject.")
        sys.exit(1)

    print(f"Found conversation: '{convo.get('subject')}' (id: {convo['id']})")

    print("Downloading CSV attachment...")
    csv_text = get_csv_attachment(convo["id"])
    if not csv_text:
        print("ERROR: No CSV attachment found in conversation.")
        sys.exit(1)

    print(f"Parsing CSV ({len(csv_text)} bytes)...")
    rows = parse_rippling_csv(csv_text)
    if not rows:
        print("ERROR: CSV parsed but no valid rows found. Check column names.")
        print("Expected columns: 'Employee', 'Time entry payable time (hours)', 'Pay period start'")
        sys.exit(1)

    print(f"Found {len(rows)} row(s):")
    for r in rows:
        print(f"  {r['tech_name']} — {r['worked_hours']}h — week {r['week_date']}")

    print("Writing to Airtable...")
    result = upsert_to_scorecard(rows)
    print(f"Done: {result['created']} created, {result['updated']} updated.")


if __name__ == "__main__":
    main()
