"""
test_inbox_counts.py

Quick test of the inbox-counting logic before deploying.
Counts: assigned conversations + conversations carrying the user's personal @label.

Run: python test_inbox_counts.py
"""

import os, time
from collections import defaultdict
import requests
from dotenv import load_dotenv

load_dotenv()
MISSIVE_TOKEN = os.environ.get("MISSIVE_TOKEN", "")
MISSIVE_BASE  = "https://public.missiveapp.com/v1"
SKIP_NAMES    = {"BPM-DOS Team", "Marchenka White", "Jeff Stoddard"}
THRESHOLD     = 50

headers = {"Authorization": f"Bearer {MISSIVE_TOKEN}", "Accept": "application/json"}

# --- org ---
org_id = requests.get(f"{MISSIVE_BASE}/organizations", headers=headers, timeout=15) \
    .json()["organizations"][0]["id"]
print(f"Org: {org_id}\n")

# --- fetch @-labels ---
label_resp = requests.get(f"{MISSIVE_BASE}/labels", headers=headers,
                          params={"organization": org_id}, timeout=15)
at_labels = {}
if label_resp.status_code == 200:
    at_labels = {
        l["id"]: l["name"][1:].lower()
        for l in label_resp.json().get("labels", [])
        if (l.get("name") or "").startswith("@")
    }
print(f"@-labels found: {list(at_labels.values()) or 'none'}\n")

# --- paginate conversations ---
cutoff_ts = int(time.time()) - 90 * 86400
assigned_ids: dict[str, set] = defaultdict(set)
labeled_ids:  dict[str, set] = defaultdict(set)
first_to_name: dict[str, str] = {}

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
        convo_id = convo.get("id")
        convo_label_ids = {l["id"] for l in (convo.get("labels") or [])}

        # Register user names on this convo first
        for user in (convo.get("users") or []):
            name = user.get("name") or user.get("email", "")
            if name and name not in SKIP_NAMES:
                first = name.split()[0].lower()
                first_to_name.setdefault(first, name)

        # Assigned
        for user in (convo.get("users") or []):
            name = user.get("name") or user.get("email", "")
            if not name or name in SKIP_NAMES:
                continue
            if not user.get("closed") and not user.get("trashed") and not user.get("junked"):
                if user.get("assigned"):
                    assigned_ids[name].add(convo_id)

        # @labeled
        if at_labels and convo_label_ids:
            for label_id, shortname in at_labels.items():
                if label_id not in convo_label_ids:
                    continue
                for first, uname in first_to_name.items():
                    if first.startswith(shortname) or shortname.startswith(first):
                        labeled_ids[uname].add(convo_id)
                        break

    total_attr = sum(len(s) for s in assigned_ids.values()) + sum(len(s) for s in labeled_ids.values())
    print(f"Page {pages}: {len(convos)} convos, {total_attr} total attributions so far")

    if len(convos) < 50:
        break
    oldest_ts = min(c.get("last_activity_at", 0) for c in convos if isinstance(c, dict))
    if oldest_ts and oldest_ts < cutoff_ts:
        print("Reached 90-day cutoff, stopping.")
        break
    params = {"organization": org_id, "all": "true", "limit": 50, "until": oldest_ts}

# --- results ---
all_names = set(assigned_ids) | set(labeled_ids)
results = []
for name in all_names:
    a = assigned_ids.get(name, set())
    l = labeled_ids.get(name, set()) - a
    results.append((name, len(a) + len(l), len(a), len(l)))

results.sort(key=lambda x: -x[1])

print(f"\n=== Results ({pages} pages) ===")
print(f"{'Name':<30} {'Total':>6} {'Assigned':>10} {'@Labeled':>10}")
print("-" * 60)
for name, total, assigned, labeled in results:
    marker = "  *** OVER 50" if total > THRESHOLD else ""
    print(f"{name:<30} {total:>6} {assigned:>10} {labeled:>10}{marker}")
