"""Clear _unknown_notified and leg_verdicts for CFL UNKNOWN picks so tracker re-grades them."""
import json

with open("parse_cache.json") as f:
    cache = json.load(f)

fixed = 0
for key, entry in cache.items():
    if not isinstance(entry, dict):
        continue
    parsed = entry.get("parsed", {})
    if parsed.get("sport") != "CFL":
        continue
    if entry.get("_unknown_notified"):
        entry.pop("_unknown_notified", None)
        entry["leg_verdicts"] = {}
        fixed += 1
        print(f"Reset: {key} — {parsed.get('picks', [{}])[0].get('description', '?')}")

if fixed:
    with open("parse_cache.json", "w") as f:
        json.dump(cache, f, indent=2)
    print(f"\nFixed {fixed} entries")
else:
    print("No CFL UNKNOWN entries to fix")
