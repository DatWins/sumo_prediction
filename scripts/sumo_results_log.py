"""
sumo_results_log.py
====================
Compares a day's model predictions against actual bout results from sumo-api.com,
then appends/updates an entry in docs\results.json for the GitHub Pages site.

Usage (run AFTER bouts complete):
    python scripts/sumo_results_log.py 202611 10

What it does:
1. Fetches actual results for <bashoId> day <day> from sumo-api.com
2. Loads docs\predictions.json to find what the model predicted
3. Compares predicted winner vs actual winner for each bout
4. Appends a record to docs\results.json (creates it if missing)
5. Prints a summary: X/Y correct (Z%)

Place this file at:  C:\SUMO PREDICTION\scripts\sumo_results_log.py
Run from:            C:\SUMO PREDICTION\
"""

import sys
import json
import urllib.request
from pathlib import Path
from datetime import date

# ── Args ─────────────────────────────────────────────────────────────────────
if len(sys.argv) < 3:
    print("Usage: python scripts\\sumo_results_log.py <bashoId> <day>")
    print("  e.g. python scripts\\sumo_results_log.py 202611 10")
    sys.exit(1)

basho_id = sys.argv[1]          # e.g. "202611"
day      = int(sys.argv[2])     # e.g. 10

# ── Paths ─────────────────────────────────────────────────────────────────────
# Script lives in scripts\, so repo root is one level up from __file__
script_dir   = Path(__file__).parent
repo_root    = script_dir.parent
docs_dir     = repo_root / "docs"
results_path = docs_dir / "results.json"
pred_path    = docs_dir / "predictions.json"

# ── Fetch actual results from API ─────────────────────────────────────────────
url = f"https://sumo-api.com/api/basho/{basho_id}/torikumi/Makuuchi/{day}"
print(f"Fetching actual results: {url}")
try:
    with urllib.request.urlopen(url, timeout=15) as resp:
        data = json.loads(resp.read().decode())
except Exception as e:
    print(f"ERROR fetching from API: {e}")
    sys.exit(1)

# API returns either {"torikumi": [...]} or a list directly
torikumi = data.get("torikumi", data) if isinstance(data, dict) else data
if not torikumi:
    print(f"No bouts returned for basho {basho_id} day {day}. Bouts may not be complete yet.")
    sys.exit(1)

print(f"  Got {len(torikumi)} bouts from API")

# Build lookup by name pair: (eastShikona, westShikona) -> actual winner name (winnerEn)
# API fields confirmed: eastShikona, westShikona, winnerId, eastId, westId, winnerEn
actual = {}   # (eastName, westName) -> winnerEn  (English name of actual winner)
for bout in torikumi:
    ename      = (bout.get("eastShikona") or "").strip()
    wname      = (bout.get("westShikona") or "").strip()
    winner_en  = (bout.get("winnerEn") or "").strip()
    winner_id  = bout.get("winnerId")
    east_id    = bout.get("eastId")

    if not (ename and wname):
        continue

    if winner_en:
        actual[(ename, wname)] = winner_en
        actual[(wname, ename)] = winner_en  # also index reversed in case day card differs
    elif winner_id and east_id:
        # fallback: derive from IDs
        winner_name = ename if winner_id == east_id else wname
        actual[(ename, wname)] = winner_name
        actual[(wname, ename)] = winner_name

# ── Load predictions ──────────────────────────────────────────────────────────
if not pred_path.exists():
    print(f"ERROR: predictions.json not found at {pred_path}")
    print("  Run sumo_predict_json.py first, then copy to docs\\")
    sys.exit(1)

with open(pred_path, encoding="utf-8") as f:
    pred_data = json.load(f)

# predictions.json format: {"basho": "...", "day": N, "bouts": [...]}
preds = pred_data.get("bouts", [])
if not preds:
    print("ERROR: predictions.json has no 'bouts' array.")
    sys.exit(1)

pred_basho = pred_data.get("basho", basho_id)
pred_day   = pred_data.get("day", day)

if str(pred_day) != str(day):
    print(f"WARNING: predictions.json is for day {pred_day}, but you asked for day {day}.")
    print("  Make sure you pushed Day N predictions before running this script for Day N.")

# ── Compare ───────────────────────────────────────────────────────────────────
correct = 0
total   = 0
bout_details = []

for bout in preds:
    east_name = bout.get("east", "")
    west_name = bout.get("west", "")
    eid = str(bout.get("east_id", ""))
    wid = str(bout.get("west_id", ""))

    # Determine predicted winner: use "pick" (a name), predicted_winner, or east_win_prob
    pred_winner = bout.get("predicted_winner")
    if pred_winner is None:
        pick_name = bout.get("pick", "")
        if pick_name:
            # compare pick name to east/west rikishi name
            pred_winner = "east" if pick_name.strip() == east_name.strip() else "west"
        else:
            prob = bout.get("east_win_prob", bout.get("pick_prob", 0.5))
            pred_winner = "east" if float(prob) >= 0.5 else "west"

    # Lookup actual winner name from API, trying both orientations
    winner_name = actual.get((east_name.strip(), west_name.strip()))
    if winner_name is None:
        winner_name = actual.get((west_name.strip(), east_name.strip()))

    # Convert winner name to "east" / "west"
    if winner_name is None:
        actual_winner = None
    elif winner_name.strip() == east_name.strip():
        actual_winner = "east"
    elif winner_name.strip() == west_name.strip():
        actual_winner = "west"
    else:
        actual_winner = None  # name mismatch

    if actual_winner is None:
        # Result not in API yet
        bout_details.append({
            "east": east_name, "west": west_name,
            "predicted": pred_winner, "actual": "pending", "correct": None
        })
        continue

    is_correct = (pred_winner == actual_winner)
    if is_correct:
        correct += 1
    total += 1

    bout_details.append({
        "east": east_name, "west": west_name,
        "predicted": pred_winner,
        "actual": actual_winner,
        "correct": is_correct
    })

accuracy = round(correct / total * 100, 1) if total > 0 else 0.0
print(f"\nDay {day} result: {correct}/{total} correct ({accuracy}%)")

# Print per-bout summary
print("\nBout-by-bout:")
for b in bout_details:
    mark = "✓" if b["correct"] else ("?" if b["correct"] is None else "✗")
    print(f"  {mark}  {b['east']} vs {b['west']}  — predicted: {b['predicted']}, actual: {b['actual']}")

# ── Update results.json ───────────────────────────────────────────────────────
if results_path.exists():
    with open(results_path, encoding="utf-8") as f:
        results = json.load(f)
else:
    results = {"basho": pred_basho, "days": []}

# Remove any existing entry for this day (allow re-run)
results["days"] = [d for d in results["days"] if d.get("day") != day]

# Basho display name
basho_year  = basho_id[:4]
basho_month = int(basho_id[4:6])
month_names = {1:"Hatsu",3:"Haru",5:"Natsu",7:"Nagoya",9:"Aki",11:"Kyushu"}
basho_name  = f"{month_names.get(basho_month,'Unknown')} {basho_year}"

results["basho"]     = pred_basho or basho_name
results["basho_id"]  = basho_id
results["days"].append({
    "day":      day,
    "correct":  correct,
    "total":    total,
    "accuracy": accuracy,
    "date":     date.today().isoformat(),
    "bouts":    bout_details
})

# Sort days in order
results["days"].sort(key=lambda d: d["day"])

# Running totals
all_correct = sum(d["correct"] for d in results["days"])
all_total   = sum(d["total"]   for d in results["days"])
results["basho_correct"]  = all_correct
results["basho_total"]    = all_total
results["basho_accuracy"] = round(all_correct / all_total * 100, 1) if all_total > 0 else 0.0

docs_dir.mkdir(parents=True, exist_ok=True)
with open(results_path, "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2, ensure_ascii=False)

print(f"\nSaved → {results_path}")
print(f"Basho running total: {all_correct}/{all_total} ({results['basho_accuracy']}%)")
print("\nNext: git add docs\\results.json, commit, push")
