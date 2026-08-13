"""
test_inbox_counts.py

Tests the shared team inbox counting logic.
For each discovered team inbox, shows total / assigned / unassigned open conversations.

Run: python test_inbox_counts.py
"""

import os, time
import requests
from dotenv import load_dotenv

load_dotenv()
MISSIVE_TOKEN = os.environ.get("MISSIVE_TOKEN", "")
MISSIVE_BASE  = "https://public.missiveapp.com/v1"
THRESHOLD     = 50

headers = {"Authorization": f"Bearer {MISSIVE_TOKEN}", "Accept": "application/json"}

# --- org ---
org_id = requests.get(f"{MISSIVE_BASE}/organizations", headers=headers, timeout=15) \
    .json()["organizations"][0]["id"]
print(f"Org: {org_id}\n")

# --- discover team inboxes ---
print("Discovering team inboxes from recent conversations...")
teams = {}
params = {"organization": org_id, "all": "true", "limit": 50}
for page in range(4):
    resp = requests.get(f"{MISSIVE_BASE}/conversations", headers=headers,
                        params=params, timeout=30)
    if resp.status_code != 200:
        break
    convos = resp.json().get("conversations", [])
    if not convos:
        break
    for c in convos:
        t = c.get("team")
        if t:
            teams[t["id"]] = t.get("name", "?")
    if len(convos) < 50:
        break
    oldest_ts = min(c.get("last_activity_at", 0) for c in convos if isinstance(c, dict))
    params = {"organization": org_id, "all": "true", "limit": 50, "until": oldest_ts}

print(f"Found {len(teams)} team inboxes: {list(teams.values())}\n")

# --- count each inbox ---
def count_team_inbox(team_id):
    assigned = 0
    unassigned = 0
    p = {"team_inbox": team_id, "limit": 50}
    for _ in range(20):
        r = requests.get(f"{MISSIVE_BASE}/conversations", headers=headers, params=p, timeout=30)
        if r.status_code != 200:
            print(f"  HTTP {r.status_code} for team {team_id}")
            break
        convos = r.json().get("conversations", [])
        if not convos:
            break
        for convo in convos:
            is_assigned = any(u.get("assigned") for u in (convo.get("users") or []))
            if is_assigned:
                assigned += 1
            else:
                unassigned += 1
        if len(convos) < 50:
            break
        oldest_ts = min(c.get("last_activity_at", 0) for c in convos if isinstance(c, dict))
        p = {"team_inbox": team_id, "limit": 50, "until": oldest_ts}
    return assigned, unassigned

results = []
for team_id, team_name in teams.items():
    print(f"Counting {team_name}...")
    a, u = count_team_inbox(team_id)
    results.append((team_name, a + u, a, u))

results.sort(key=lambda x: -x[1])

print(f"\n=== Results ===")
print(f"{'Team Inbox':<35} {'Total':>6} {'Assigned':>10} {'Unassigned':>12}")
print("-" * 67)
for name, total, assigned, unassigned in results:
    marker = "  *** OVER 50" if total > THRESHOLD else ""
    print(f"{name:<35} {total:>6} {assigned:>10} {unassigned:>12}{marker}")
