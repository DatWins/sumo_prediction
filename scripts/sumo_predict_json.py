"""
sumo_predict_json.py — Generate predictions.json for the sumo predictor website.

Usage:
    python scripts\sumo_predict_json.py <basho_id> <day> [--h2h]

Examples:
    python scripts\sumo_predict_json.py 202411 10
    python scripts\sumo_predict_json.py 202411 10 --h2h

Output files (copy both to docs\ then push to GitHub):
    predictions.json  — today's bout card with model picks
    h2h.json          — head-to-head lookup (--h2h flag; run once per basho)

Requirements:
    pip install requests lightgbm pandas numpy joblib scikit-learn
    sumo_bouts.db   — built by sumo_scraper.py
    sumo_model.pkl  — built by sumo_model.py

Run on your Windows machine (sumo-api.com is blocked from Claude's servers).
"""

import sys
import json
import sqlite3
import pickle
import datetime
import numpy as np
import pandas as pd
import requests
from collections import defaultdict

DB_PATH    = "sumo_bouts.db"
MODEL_PATH = "sumo_model.pkl"
API_BASE   = "https://sumo-api.com/api"
DIVISION   = "Makuuchi"

OSHI_TECHNIQUES = {
    'oshidashi','oshitaoshi','tsukidashi','tsukitaoshi',
    'hatakikomi','hikiotoshi','okuridashi','okuritaoshi',
    'tsuppari','nodowa','yoritaoshi'
}

# ── Rank helper ───────────────────────────────────────────────────────────────
def rank_to_int(rank_str):
    if not rank_str:
        return 50
    s = str(rank_str).strip()
    full_map = {
        "Yokozuna": 0, "Ozeki": 4, "Sekiwake": 8,
        "Komusubi": 10, "Maegashira": 12, "Juryo": 44,
    }
    for name, base in full_map.items():
        if s.startswith(name):
            rest = s[len(name):].strip().split()
            num  = int(rest[0]) if rest and rest[0].isdigit() else 1
            side = 0 if (len(rest) > 1 and rest[-1].lower() == "east") else 1
            return base + num * 2 + side
    prefix_map = {"Y": 0, "O": 4, "S": 8, "K": 10, "M": 12, "J": 44}
    su = s.upper()
    for prefix in ("Y","O","S","K","M","J"):
        if su.startswith(prefix):
            rest = s[len(prefix):]
            num  = int(''.join(filter(str.isdigit, rest)) or 0)
            side = 0 if rest.lower().endswith('e') else 1
            return prefix_map.get(prefix, 50) + num * 2 + side
    return 50

# ── DB lookups ────────────────────────────────────────────────────────────────
def load_h2h(conn):
    """career H2H: h2h[east_id][west_id] = [east_wins, total]"""
    h2h = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    cur = conn.execute("SELECT east_id, west_id, east_win FROM bouts")
    for eid, wid, ew in cur:
        h2h[eid][wid][1] += 1
        if ew == 1:
            h2h[eid][wid][0] += 1
    return h2h

def load_oshi_ratios(conn):
    oshi  = defaultdict(int)
    total = defaultdict(int)
    cur = conn.execute("SELECT east_id, west_id, east_win, kimarite FROM bouts")
    for eid, wid, ew, km in cur:
        winner = eid if ew == 1 else wid
        total[winner] += 1
        if km and km.lower().strip() in OSHI_TECHNIQUES:
            oshi[winner] += 1
    return {rid: oshi[rid] / max(total[rid], 1) for rid in total}

def running_records(conn, basho_id, up_to_day):
    """wins/losses before up_to_day in this basho"""
    rec = defaultdict(lambda: [0, 0])
    cur = conn.execute(
        "SELECT east_id, west_id, east_win FROM bouts WHERE basho_id=? AND day<?",
        (basho_id, up_to_day)
    )
    for eid, wid, ew in cur:
        if ew == 1:
            rec[eid][0] += 1
            rec[wid][1] += 1
        else:
            rec[wid][0] += 1
            rec[eid][1] += 1
    return rec

def recent_wins(conn, basho_id, rikishi_id, up_to_day, n=3):
    cur = conn.execute("""
        SELECT east_id, east_win FROM bouts
        WHERE basho_id=? AND (east_id=? OR west_id=?) AND day<?
        ORDER BY day DESC LIMIT ?
    """, (basho_id, rikishi_id, rikishi_id, up_to_day, n))
    rows = cur.fetchall()
    total = 0
    for eid, ew in rows:
        if eid == rikishi_id:
            total += ew
        else:
            total += (1 - ew)
    return total

def rikishi_bmi(conn, rid):
    cur = conn.execute("SELECT height_cm, weight_kg FROM rikishi WHERE id=?", (rid,))
    row = cur.fetchone()
    if not row or not row[0] or not row[1]: return None
    h, w = row
    return w / (h / 100) ** 2 if h > 0 else None

def rikishi_age(conn, rid, basho_id):
    cur = conn.execute("SELECT birth_date FROM rikishi WHERE id=?", (rid,))
    row = cur.fetchone()
    if not row or not row[0]: return None
    try:
        bd = datetime.datetime.strptime(str(row[0])[:10], "%Y-%m-%d").date()
        ref = datetime.date(int(basho_id[:4]), int(basho_id[4:]), 1)
        return (ref - bd).days / 365.25
    except Exception:
        return None

# ── Build feature row ─────────────────────────────────────────────────────────
def build_feature(eid, wid, e_rank_str, w_rank_str, basho_id, day,
                  conn, h2h, oshi_ratios, records):
    er = records.get(eid, [0, 0])
    wr = records.get(wid, [0, 0])

    h = h2h[eid][wid]
    h2h_ewp = h[0] / h[1] if h[1] > 0 else 0.5
    h2h_cnt = h[1]

    e_bmi = rikishi_bmi(conn, eid)
    w_bmi = rikishi_bmi(conn, wid)
    bmi_diff = (e_bmi - w_bmi) if (e_bmi and w_bmi) else 0.0

    e_age = rikishi_age(conn, eid, basho_id)
    w_age = rikishi_age(conn, wid, basho_id)
    age_diff = (e_age - w_age) if (e_age and w_age) else 0.0

    return {
        "rank_diff":         rank_to_int(e_rank_str) - rank_to_int(w_rank_str),
        "record_diff":       (er[0] - er[1]) - (wr[0] - wr[1]),
        "h2h_east_win_pct":  h2h_ewp,
        "h2h_count":         h2h_cnt,
        "east_momentum":     recent_wins(conn, basho_id, eid, day),
        "west_momentum":     recent_wins(conn, basho_id, wid, day),
        "east_kk_safe":      1 if er[0] >= 8 else 0,
        "west_kk_safe":      1 if wr[0] >= 8 else 0,
        "east_mk_risk":      1 if er[1] >= 7 else 0,
        "west_mk_risk":      1 if wr[1] >= 7 else 0,
        "east_oshi_ratio":   oshi_ratios.get(eid, 0.3),
        "west_oshi_ratio":   oshi_ratios.get(wid, 0.3),
        "bmi_diff":          bmi_diff,
        "age_diff":          age_diff,
        "day":               day,
    }

# ── H2H export ────────────────────────────────────────────────────────────────
def build_h2h_export(conn):
    name_map = {r[0]: r[1] for r in conn.execute("SELECT id, shikona FROM rikishi") if r[1]}
    cur = conn.execute(
        "SELECT east_id, west_id, east_win, kimarite, basho_id, day FROM bouts ORDER BY basho_id, day"
    )
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

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if len(sys.argv) < 3:
        print("Usage: python scripts\\sumo_predict_json.py <basho_id> <day> [--h2h]")
        print("  e.g. python scripts\\sumo_predict_json.py 202411 10")
        sys.exit(1)

    basho_id = sys.argv[1]
    day      = int(sys.argv[2])
    do_h2h   = "--h2h" in sys.argv

    # Load model
    print(f"Loading model from {MODEL_PATH}…")
    with open(MODEL_PATH, "rb") as f:
        artifact = pickle.load(f)
    model    = artifact["model"]
    FEATURES = artifact["features"]

    # Load DB
    print(f"Opening {DB_PATH}…")
    conn = sqlite3.connect(DB_PATH)

    # Pre-load lookups
    print("Building H2H and oshi lookups…")
    h2h         = load_h2h(conn)
    oshi_ratios = load_oshi_ratios(conn)
    records     = running_records(conn, basho_id, day)
    name_map    = {r[0]: r[1] for r in conn.execute("SELECT id, shikona FROM rikishi") if r[1]}

    # Fetch today's card from API
    print(f"Fetching torikumi for basho {basho_id} day {day}…")
    try:
        r = requests.get(f"{API_BASE}/basho/{basho_id}/torikumi/{DIVISION}/{day}", timeout=15)
        r.raise_for_status()
        card = r.json()
    except Exception as e:
        print(f"ERROR fetching torikumi: {e}")
        conn.close()
        sys.exit(1)

    bouts_raw = card if isinstance(card, list) else card.get("torikumi", [])
    if not bouts_raw:
        print("No bouts returned from API.")
        conn.close()
        sys.exit(1)

    # Score each bout
    bouts_out = []
    for b in bouts_raw:
        eid = b.get("eastId")
        wid = b.get("westId")
        if not eid or not wid:
            continue

        e_rank = b.get("eastRank", "")
        w_rank = b.get("westRank", "")
        e_name = b.get("eastShikona") or name_map.get(eid, str(eid))
        w_name = b.get("westShikona") or name_map.get(wid, str(wid))

        feats = build_feature(eid, wid, e_rank, w_rank, basho_id, day,
                              conn, h2h, oshi_ratios, records)
        X = pd.DataFrame([feats])[FEATURES]
        prob_east = float(model.predict_proba(X)[0][1])

        er = records.get(eid, [0, 0])
        wr = records.get(wid, [0, 0])

        # H2H summary string (east perspective)
        h = h2h[eid][wid]
        h2h_str = f"{h[0]}-{h[1]-h[0]}" if h[1] > 0 else "—"

        if prob_east >= 0.5:
            pick, pick_prob = e_name, prob_east
        else:
            pick, pick_prob = w_name, 1 - prob_east

        bout_out = {
            "east":      e_name,
            "west":      w_name,
            "east_rank": e_rank,
            "west_rank": w_rank,
            "east_rec":  f"{er[0]}-{er[1]}",
            "west_rec":  f"{wr[0]}-{wr[1]}",
            "h2h":       h2h_str,
            "pick":      pick,
            "pick_prob": round(pick_prob, 4),
        }
        bouts_out.append(bout_out)
        conf = "🔒" if pick_prob >= 0.65 else "  "
        print(f"  {conf} {e_name} vs {w_name} → {pick} ({pick_prob*100:.1f}%)")

    output = {
        "basho_id":      basho_id,
        "day":           day,
        "generated":     datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "basho_correct": None,
        "basho_total":   None,
        "bouts":         bouts_out,
    }

    with open("predictions.json", "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"\n✓  predictions.json written — {len(bouts_out)} bouts")

    high_conf = [b for b in bouts_out if b["pick_prob"] >= 0.65]
    if high_conf:
        print(f"🔒  High-confidence picks (≥65%): {len(high_conf)}")
        for b in high_conf:
            print(f"    {b['pick']} ({b['pick_prob']*100:.1f}%)")

    if do_h2h:
        print("\nBuilding h2h.json (may take a minute)…")
        h2h_export = build_h2h_export(conn)
        with open("h2h.json", "w", encoding="utf-8") as f:
            json.dump(h2h_export, f, ensure_ascii=False)
        print(f"✓  h2h.json written — {len(h2h_export)} pairs")

    conn.close()
    print("\nNext: copy predictions.json to docs\\ and push to GitHub.")

if __name__ == "__main__":
    main()
