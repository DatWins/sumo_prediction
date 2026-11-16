"""
sumo_predict_json.py — Generate predictions.json for the sumo predictor website.

Usage:
    python sumo_predict_json.py <basho_id> <day>

Example:
    python sumo_predict_json.py 202411 10

Outputs:
    predictions.json  — fetched from sumo-api.com and scored by the trained model
    h2h.json          — head-to-head lookup table for the H2H tab (run once per basho)

Place both files in docs/ of the sumo_prediction repo, then push to GitHub.
GitHub Pages serves them automatically.

Requirements:
    pip install requests lightgbm pandas numpy joblib
    sumo_bouts.db    — built by sumo_scraper.py
    sumo_model.pkl   — built by sumo_model.py

Run on your Windows machine (sumo-api.com is not reachable from Claude's servers).
"""

import sys
import json
import sqlite3
import joblib
import datetime
import numpy as np
import pandas as pd
import requests

# ── config ──────────────────────────────────────────────────
DB_PATH    = "sumo_bouts.db"
MODEL_PATH = "sumo_model.pkl"
API_BASE   = "https://sumo-api.com/api"
DIVISION   = "Makuuchi"

# Oshi (pushing) kimarite — drives oshi_ratio feature
OSHI_KIMARITE = {
    "oshidashi","oshitaoshi","tsukidashi","tsukitaoshi","hatakikomi",
    "hikiotoshi","nodowa","tsukiotoshi",
}

# ── rank helper (must match sumo_model.py) ───────────────────
def rank_to_int(rank_str: str) -> int:
    if not rank_str:
        return 99
    s = rank_str.strip().upper()
    tier_map = {"Y": 0, "O": 4, "S": 8, "K": 10, "M": 12, "J": 40, "MS": 60}
    for prefix in ("MS","Y","O","S","K","M","J"):
        if s.startswith(prefix):
            rest = s[len(prefix):]
            num_str = "".join(c for c in rest if c.isdigit())
            num = int(num_str) if num_str else 1
            side = 0 if s.endswith("E") else 1
            return tier_map.get(prefix, 99) + num * 2 + side
    return 99


# ── database helpers ─────────────────────────────────────────
def load_h2h(conn):
    """Build career H2H win-count dict: {(east_id, west_id): east_wins}."""
    cur = conn.execute("""
        SELECT east_id, west_id, east_win
        FROM bouts
        WHERE division = 'Makuuchi'
        ORDER BY basho_id, day
    """)
    h2h = {}
    for eid, wid, ew in cur:
        key = (min(eid, wid), max(eid, wid))
        if key not in h2h:
            h2h[key] = [0, 0]
        if eid < wid:
            h2h[key][0] += ew
            h2h[key][1] += (1 - ew)
        else:
            h2h[key][1] += ew
            h2h[key][0] += (1 - ew)
    return h2h


def load_oshi_ratios(conn):
    """Return {rikishi_id: oshi_win_ratio}."""
    cur = conn.execute("""
        SELECT east_id, west_id, east_win, kimarite
        FROM bouts WHERE division = 'Makuuchi'
    """)
    wins  = {}
    oshi_wins = {}
    for eid, wid, ew, km in cur:
        winner = eid if ew else wid
        wins[winner]       = wins.get(winner, 0) + 1
        if km and km.lower() in OSHI_KIMARITE:
            oshi_wins[winner] = oshi_wins.get(winner, 0) + 1
    return {rid: oshi_wins.get(rid, 0) / max(wins[rid], 1)
            for rid in wins}


def running_records(conn, basho_id: str, up_to_day: int):
    """Return {rikishi_id: [wins, losses]} for days < up_to_day in this basho."""
    cur = conn.execute("""
        SELECT east_id, west_id, east_win
        FROM bouts
        WHERE basho_id = ? AND day < ? AND division = 'Makuuchi'
    """, (basho_id, up_to_day))
    rec = {}
    for eid, wid, ew in cur:
        if eid not in rec: rec[eid] = [0, 0]
        if wid not in rec: rec[wid] = [0, 0]
        if ew:
            rec[eid][0] += 1
            rec[wid][1] += 1
        else:
            rec[wid][0] += 1
            rec[eid][1] += 1
    return rec


def recent_wins(conn, basho_id: str, rikishi_id: int, up_to_day: int, n: int = 3) -> int:
    """Wins in the last n bouts before up_to_day this basho."""
    cur = conn.execute("""
        SELECT east_id, west_id, east_win FROM bouts
        WHERE basho_id = ? AND division = 'Makuuchi'
          AND (east_id = ? OR west_id = ?)
          AND day < ?
        ORDER BY day DESC LIMIT ?
    """, (basho_id, rikishi_id, rikishi_id, up_to_day, n))
    rows = cur.fetchall()
    total = 0
    for eid, wid, ew in rows:
        if eid == rikishi_id: total += ew
        else:                  total += (1 - ew)
    return total


def rikishi_bmi(conn, rikishi_id: int):
    cur = conn.execute("SELECT height, weight FROM rikishi WHERE id = ?", (rikishi_id,))
    row = cur.fetchone()
    if not row or not row[0] or not row[1]:
        return None
    h, w = row
    return w / (h / 100) ** 2


def rikishi_birthdate(conn, rikishi_id: int):
    cur = conn.execute("SELECT birth_date FROM rikishi WHERE id = ?", (rikishi_id,))
    row = cur.fetchone()
    if not row or not row[0]:
        return None
    try:
        return datetime.datetime.strptime(row[0][:10], "%Y-%m-%d").date()
    except Exception:
        return None


# ── basho history for accuracy tracking ─────────────────────
def load_basho_log(conn, current_basho: str):
    """Check how many picks were right in earlier days this basho (if results exist)."""
    cur = conn.execute("""
        SELECT day, COUNT(*) as total,
               SUM(CASE WHEN east_win = 1 THEN 1 ELSE 0 END) as east_wins
        FROM bouts
        WHERE basho_id = ? AND division = 'Makuuchi'
        GROUP BY day ORDER BY day
    """, (current_basho,))
    return cur.fetchall()


# ── build feature row ────────────────────────────────────────
def build_feature(bout: dict, conn, h2h: dict, oshi: dict,
                  records: dict, oshi_ratios: dict, day: int) -> dict:
    eid = bout["east_id"]
    wid = bout["west_id"]

    er = records.get(eid, [0, 0])
    wr = records.get(wid, [0, 0])

    e_rank = rank_to_int(bout.get("east_rank", ""))
    w_rank = rank_to_int(bout.get("west_rank", ""))

    # H2H
    key = (min(eid, wid), max(eid, wid))
    h = h2h.get(key, [0, 0])
    if eid < wid:
        e_h2h_wins, h2h_total = h[0], h[0] + h[1]
    else:
        e_h2h_wins, h2h_total = h[1], h[0] + h[1]
    h2h_pct   = e_h2h_wins / h2h_total if h2h_total else 0.5
    h2h_count = h2h_total

    e_wins, e_losses = er
    w_wins, w_losses = wr

    return {
        "rank_diff":         e_rank - w_rank,
        "record_diff":       (e_wins - e_losses) - (w_wins - w_losses),
        "h2h_east_win_pct":  h2h_pct,
        "h2h_count":         h2h_count,
        "east_momentum":     recent_wins(conn, bout["basho_id"], eid, day),
        "west_momentum":     recent_wins(conn, bout["basho_id"], wid, day),
        "east_kk_safe":      1 if e_wins >= 8 else 0,
        "west_kk_safe":      1 if w_wins >= 8 else 0,
        "east_mk_risk":      1 if e_losses >= 7 else 0,
        "west_mk_risk":      1 if w_losses >= 7 else 0,
        "east_oshi_ratio":   oshi_ratios.get(eid, 0.3),
        "west_oshi_ratio":   oshi_ratios.get(wid, 0.3),
        "bmi_diff":          (rikishi_bmi(conn, eid) or 40) - (rikishi_bmi(conn, wid) or 40),
        "age_diff":          age_diff(conn, eid, wid, bout["basho_id"]),
        "day":               day,
    }


def age_diff(conn, eid: int, wid: int, basho_id: str) -> float:
    year = int(basho_id[:4])
    month = int(basho_id[4:])
    ref = datetime.date(year, month, 1)
    eb = rikishi_birthdate(conn, eid)
    wb = rikishi_birthdate(conn, wid)
    if not eb or not wb:
        return 0.0
    return (ref - eb).days / 365.25 - (ref - wb).days / 365.25


# ── fetch today's card from sumo-api.com ────────────────────
def fetch_torikumi(basho_id: str, day: int):
    url = f"{API_BASE}/basho/{basho_id}/torikumi/{DIVISION}/{day}"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    return r.json()


def fetch_banzuke(basho_id: str):
    url = f"{API_BASE}/basho/{basho_id}/banzuke/{DIVISION}"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    return r.json()


# ── H2H export for the website ───────────────────────────────
def build_h2h_export(conn):
    """
    Build h2h.json: {"{name1}|{name2}": {wins, losses, bouts:[...]}}
    Uses shikona names from the rikishi table.
    """
    name_map = {}
    for row in conn.execute("SELECT id, shikona_en FROM rikishi"):
        if row[1]:
            name_map[row[0]] = row[1]

    cur = conn.execute("""
        SELECT b.east_id, b.west_id, b.east_win, b.kimarite,
               b.basho_id, b.day
        FROM bouts b
        WHERE b.division = 'Makuuchi'
        ORDER BY b.basho_id, b.day
    """)

    pairs = {}
    for eid, wid, ew, km, bid, day in cur:
        en = name_map.get(eid, str(eid))
        wn = name_map.get(wid, str(wid))
        key = f"{en}|{wn}"
        if key not in pairs:
            pairs[key] = {"wins": 0, "losses": 0, "bouts": []}
        pairs[key]["wins"]   += ew
        pairs[key]["losses"] += (1 - ew)
        pairs[key]["bouts"].append({
            "basho_id": bid, "day": day,
            "east": en, "west": wn,
            "winner": en if ew else wn,
            "kimarite": km or "",
        })

    return pairs


# ── main ─────────────────────────────────────────────────────
def main():
    if len(sys.argv) < 3:
        print("Usage: python sumo_predict_json.py <basho_id> <day>")
        print("  e.g. python sumo_predict_json.py 202411 10")
        sys.exit(1)

    basho_id = sys.argv[1]
    day      = int(sys.argv[2])

    # Load model
    print(f"Loading model from {MODEL_PATH}…")
    model  = joblib.load(MODEL_PATH)
    FEATURES = [
        "rank_diff","record_diff","h2h_east_win_pct","h2h_count",
        "east_momentum","west_momentum",
        "east_kk_safe","west_kk_safe","east_mk_risk","west_mk_risk",
        "east_oshi_ratio","west_oshi_ratio",
        "bmi_diff","age_diff","day",
    ]

    # Load database
    print(f"Opening {DB_PATH}…")
    conn = sqlite3.connect(DB_PATH)

    # Pre-load lookups
    print("Building H2H and oshi lookups…")
    h2h          = load_h2h(conn)
    oshi_ratios  = load_oshi_ratios(conn)
    records      = running_records(conn, basho_id, day)

    # Fetch today's card
    print(f"Fetching torikumi for basho {basho_id} day {day}…")
    try:
        card = fetch_torikumi(basho_id, day)
    except Exception as e:
        print(f"ERROR fetching torikumi: {e}")
        sys.exit(1)

    # Build banzuke rank lookup
    try:
        banzuke = fetch_banzuke(basho_id)
        rank_lookup = {}
        for rikishi in banzuke.get("east", []) + banzuke.get("west", []):
            rank_lookup[rikishi["rikishiID"]] = rikishi.get("rank","")
    except Exception as e:
        print(f"Warning: could not fetch banzuke — ranks may be missing ({e})")
        rank_lookup = {}

    name_map = {r[0]: r[1] for r in conn.execute("SELECT id, shikona_en FROM rikishi") if r[1]}

    # Score each bout
    bouts_out = []
    for bout_raw in card.get("torikumi", []):
        eid = bout_raw.get("eastId") or bout_raw.get("east_id")
        wid = bout_raw.get("westId") or bout_raw.get("west_id")
        if not eid or not wid:
            continue

        bout = {
            "basho_id": basho_id,
            "east_id":  eid,
            "west_id":  wid,
            "east_rank": rank_lookup.get(eid, bout_raw.get("eastRank","")),
            "west_rank": rank_lookup.get(wid, bout_raw.get("westRank","")),
        }

        feats = build_feature(bout, conn, h2h, oshi_ratios, records, oshi_ratios, day)
        X = pd.DataFrame([feats])[FEATURES]
        prob_east = float(model.predict_proba(X)[0][1])

        east_name = name_map.get(eid, str(eid))
        west_name = name_map.get(wid, str(wid))
        er = records.get(eid, [0, 0])
        wr = records.get(wid, [0, 0])

        # H2H summary string
        key = (min(eid, wid), max(eid, wid))
        h = h2h.get(key, [0, 0])
        if eid < wid:
            e_hw, h_total = h[0], h[0]+h[1]
        else:
            e_hw, h_total = h[1], h[0]+h[1]
        h2h_str = f"{e_hw}-{h_total - e_hw}" if h_total else "—"

        if prob_east >= 0.5:
            pick = east_name
            pick_prob = prob_east
        else:
            pick = west_name
            pick_prob = 1 - prob_east

        bouts_out.append({
            "east":      east_name,
            "west":      west_name,
            "east_rank": bout["east_rank"],
            "west_rank": bout["west_rank"],
            "east_rec":  f"{er[0]}-{er[1]}",
            "west_rec":  f"{wr[0]}-{wr[1]}",
            "h2h":       h2h_str,
            "pick":      pick,
            "pick_prob": round(pick_prob, 4),
        })
        print(f"  {east_name} vs {west_name} → {pick} ({pick_prob*100:.1f}%)")

    # Basho accuracy so far (only if results are in the DB already)
    log_rows = load_basho_log(conn, basho_id)
    total_correct = 0
    total_bouts   = 0
    for _day, t, _ew in log_rows:
        total_bouts += t
    # (We don't have the model picks stored, so we can't compute accuracy here
    #  without re-running all prior days. Leave as placeholder.)

    output = {
        "basho_id":      basho_id,
        "day":           day,
        "generated":     datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "basho_correct": None,
        "basho_total":   total_bouts,
        "bouts":         bouts_out,
    }

    with open("predictions.json", "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n✓  predictions.json written — {len(bouts_out)} bouts")

    # H2H export (only regenerate if requested or first run)
    if "--h2h" in sys.argv:
        print("Building h2h.json (this may take a minute)…")
        h2h_export = build_h2h_export(conn)
        with open("h2h.json", "w") as f:
            json.dump(h2h_export, f)
        print(f"✓  h2h.json written — {len(h2h_export)} pairs")

    conn.close()
    print("\nNext step: copy predictions.json (and h2h.json) to docs/ and push to GitHub.")


if __name__ == "__main__":
    main()
