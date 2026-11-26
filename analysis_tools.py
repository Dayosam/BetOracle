"""Tool implementations that combine API-Football data with the pure analysis in analysis.py."""
import afb, analysis as an

def _n(x, lo=3, hi=10, default=6): return max(lo, min(int(x or default), hi))

def goal_sequences(team, n=6):
    """Goal-by-goal timelines of a team's last matches: 3-in-a-row streaks, leads (1UP/2UP) and comebacks. 1 request per match (cached)."""
    n = _n(n)
    t = afb.find_team(team)
    matches, warning, fh = [], None, []
    for f in afb.last_fixtures(t["id"], n):
        if None not in f["ht"]: fh.append(f["ht"][0] + f["ht"][1])
        try: ev = afb.events(f["fid"])
        except afb.BudgetExceeded as e: warning = str(e); break
        home = f["home_id"] == t["id"]
        tl = an.goal_timeline(ev, f["home_id"], f["away_id"])
        matches.append({"date": f["date"], "vs": f["away"] if home else f["home"], "venue": "H" if home else "A",
                        "pattern": an.match_pattern(tl, "H" if home else "A", f["ft"])})
    rep = an.goal_pattern_report(matches)
    first_half = {"matches": len(fh), "avg_goals": round(sum(fh) / len(fh), 2) if fh else None,
                  "over_0_5": an.rate(sum(g > 0.5 for g in fh), len(fh)), "over_1_5": an.rate(sum(g > 1.5 for g in fh), len(fh)),
                  "note": "From half-time scores, both teams' goals in that team's matches."}
    return {"team": t["name"], "scope": "regular time (90 min + stoppage), goal events", **rep, "first_half_goals": first_half, "n_requested": n, "partial": bool(warning) or len(matches) < n,
            "warning": warning, "note": "Own goals are credited to the opposing side (assumption). Small samples: use wilson_low, not the raw rate."}

def first_half_corners(team, n=6):
    """First-half corner history. Returns verified=False matches (never full-match numbers) when the provider gives no labelled half data."""
    n = _n(n)
    t = afb.find_team(team)
    rows, unverified, warning = [], 0, None
    for f in afb.last_fixtures(t["id"], n):
        try: raw = afb.half_stats_raw(f["fid"])
        except afb.BudgetExceeded as e: warning = str(e); break
        r = an.first_half_corners(raw, f["home_id"], f["away_id"])
        if r["verified"]: rows.append({"date": f["date"], "vs": f["away"] if f["home_id"] == t["id"] else f["home"], "venue": "H" if f["home_id"] == t["id"] else "A", **r})
        else: unverified += 1
    rep = an.corner_report(rows)
    ok = len(rows) >= 5
    return {"team": t["name"], "scope": "FIRST HALF only", "verified_matches": len(rows), "unverifiable_matches": unverified, "report": rep, "matches": rows,
            "sufficient_evidence": ok, "warning": warning,
            "verdict": "usable" if ok else "INSUFFICIENT: fewer than 5 matches with explicitly labelled first-half corner data. Do NOT use full-match corners as a substitute. Recommend NO BET on first-half corner markets."}

def evaluate_line(values, line, direction="over"):
    return an.hit_rate(values, float(line), direction if direction in ("over", "under") else "over")
