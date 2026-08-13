"""
test_inbox_counts.py

Quick test of the inbox-counting logic before deploying.
Prints per-user open conversation counts split by assigned vs team-unassigned.

Run: python test_inbox_counts.py
"""

import os, time
from collections import defaultdict
import requests
from dotenv import load_dotenv

load_dotenv()
MISSIVE_TOKEN = os.environ.get("MISSIVE_TOKEN", "")
MISSIVE_BASE  = "https://public.missiveapp.com/v1"
SKIP_NAMES    = {"BPM-DOS Team", "Reva Nowell", "Evan Mayo", "Jesse Leichtentritt"}
THRESHOLD     = 50

headers = {"Authorization": f"Bearer {MISSIVE_TOKEN}", "Accept": "application/json"}

org_id = requests.get(f"{MISSIVE_BASE}/organizations", headers=headers, timeout=15) \
    .json()["organizations"][0]["id"]
print(f"Org: {org_id}\n")

cutoff_ts = int(time.time()) - 90 * 86400
counts: dict[str, dict] = defaultdict(lambda: {"assigned": 0, "unassigned": 0})
params = {"organization": org_id, "all": "true", "limit": 50}
pages = 0

while True:
    resp = requests.get(f"{MISSIVE_BASE}/conversations", headers=headers,
                        params=params, timeout=30)
    if resp.status_code != 200:
        print(f"Error {resp.status_code}: {resp.text}")
        break

    convos = resp.json().get("conversations", [])
    if not convos:
        break

    pages += 1
    for convo in convos:
        for user in (convo.get("users") or []):
            name = user.get("name") or user.get("email", "")
            if not name or name in SKIP_NAMES:
                continue
            if not user.get("closed") and not user.get("trashed") and not user.get("junked"):
                if user.get("assigned"):
                    counts[name]["assigned"] += 1
                elif user.get("unassigned"):
                    counts[name]["unassigned"] += 1

    total_attr = sum(v["assigned"] + v["unassigned"] for v in counts.values())
    print(f"Page {pages}: {len(convos)} convos, {total_attr} total attributions so far")

    if len(convos) < 50:
        break
    oldest_ts = min(c.get("last_activity_at", 0) for c in convos if isinstance(c, dict))
    if oldest_ts and oldest_ts < cutoff_ts:
        print("Reached 90-day cutoff, stopping.")
        break
    params = {"organization": org_id, "all": "true", "limit": 50, "until": oldest_ts}

print(f"\n=== Results ({pages} pages) ===")
print(f"{'Name':<30} {'Total':>6} {'Assigned':>10} {'Unassigned':>12}")
print("-" * 62)
for name, data in sorted(counts.items(), key=lambda x: -(x[1]["assigned"] + x[1]["unassigned"])):
    total = data["assigned"] + data["unassigned"]
    marker = "  *** OVER 50" if total > THRESHOLD else ""
    print(f"{name:<30} {total:>6} {data['assigned']:>10} {data['unassigned']:>12}{marker}")
