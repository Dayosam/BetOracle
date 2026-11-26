"""Pure football statistics for BetOracle: no network, so every rule here can be tested directly.
The AI is told to use these numbers instead of doing arithmetic itself."""
import math, statistics

def wilson_low(h, n, z=1.96):
    """Lower end of the 95% Wilson interval for h hits in n trials. Small samples give a low number on purpose."""
    if n <= 0: return 0.0
    p = h / n
    return round((p + z * z / (2 * n) - z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)) / (1 + z * z / n), 3)

def rate(h, n): return {"hits": h, "n": n, "rate": round(h / n, 3) if n else None, "wilson_low": wilson_low(h, n)}

def hit_rate(values, line, direction="over"):
    vals = [v for v in values if v is not None]
    hits = sum(1 for v in vals if (v > line if direction == "over" else v < line))
    return {**rate(hits, len(vals)), "line": line, "direction": direction, "values": vals}

def goals_summary(rows):
    """rows: [{'gf':int,'ga':int}] -> results, goal averages, over/under rates, BTTS, clean sheets, consistency."""
    n = len(rows)
    if not n: return {"n": 0}
    tot = [r["gf"] + r["ga"] for r in rows]
    return {"n": n, "W": sum(r["gf"] > r["ga"] for r in rows), "D": sum(r["gf"] == r["ga"] for r in rows), "L": sum(r["gf"] < r["ga"] for r in rows),
            "goals_for_avg": round(sum(r["gf"] for r in rows) / n, 2), "goals_against_avg": round(sum(r["ga"] for r in rows) / n, 2),
            "total_goals_avg": round(sum(tot) / n, 2), "total_goals_stdev": round(statistics.pstdev(tot), 2) if n > 1 else 0.0,
            **{f"over_{str(l).replace('.', '_')}": rate(sum(t > l for t in tot), n) for l in (0.5, 1.5, 2.5, 3.5)},
            "btts": rate(sum(r["gf"] > 0 and r["ga"] > 0 for r in rows), n), "clean_sheets": rate(sum(r["ga"] == 0 for r in rows), n),
            "failed_to_score": rate(sum(r["gf"] == 0 for r in rows), n)}

# ---------- goal sequences (3 in a row, 1UP, 2UP, comebacks) ----------
def goal_timeline(events, home_id, away_id):
    """Regular-time goals in order: [(minute, 'H'|'A', is_own_goal)]. Missed penalties are excluded; own goals are credited to the
    opposing side (ASSUMPTION: the provider lists an own goal under the team of the player who scored it)."""
    out = []
    for e in events or []:
        if e.get("type") != "Goal": continue
        d = (e.get("detail") or "").lower()
        t = e.get("time") or {}
        if "missed" in d or t.get("elapsed") is None or t["elapsed"] > 90: continue
        tid = (e.get("team") or {}).get("id")
        side = "H" if tid == home_id else "A" if tid == away_id else None
        if side is None: continue
        own = "own" in d
        if own: side = "A" if side == "H" else "H"
        out.append(((t["elapsed"], t.get("extra") or 0), side, own))
    return sorted(out, key=lambda x: x[0])

def longest_run(sides, only=None):
    best = cur = 0
    prev = None
    for s in sides:
        cur = cur + 1 if s == prev else 1
        prev = s
        if only is None or s == only: best = max(best, cur)
    return best

def match_pattern(tl, team_side, ft):
    """One match from one team's view. Returns None when the goal events do not add up to the final score (data not trustworthy)."""
    if ft[0] is None or ft[1] is None or len(tl) != ft[0] + ft[1]: return None
    sides = [s for _, s, _ in tl]
    opp = "A" if team_side == "H" else "H"
    diff = best = worst = 0
    for s in sides:
        diff += 1 if s == team_side else -1
        best, worst = max(best, diff), min(worst, diff)
    final = ft[0] - ft[1] if team_side == "H" else ft[1] - ft[0]
    return {"any_3_in_a_row": longest_run(sides) >= 3, "team_run_for": longest_run(sides, team_side), "team_run_against": longest_run(sides, opp),
            "led_by_1": best >= 1, "led_by_2": best >= 2, "won": final > 0, "up2_win": best >= 2 or final > 0,
            "scored_first": bool(sides) and sides[0] == team_side, "conceded_first": bool(sides) and sides[0] == opp,
            "comeback_win": worst <= -1 and final > 0, "trailed_then_avoided_defeat": worst <= -1 and final >= 0,
            "own_goals": sum(o for _, _, o in tl), "goals": "".join(sides)}

def goal_pattern_report(matches):
    """matches: [{'date','vs','venue','pattern': match_pattern()|None}] -> rates with sample sizes and Wilson lower bounds."""
    ok = [m for m in matches if m["pattern"]]
    n = len(ok)
    r = lambda k: rate(sum(1 for m in ok if m["pattern"][k]), n)
    return {"matches_analysed": n, "matches_excluded_goal_data_inconsistent": len(matches) - n,
            "either_team_scored_3_in_a_row": r("any_3_in_a_row"),
            "this_team_scored_3_in_a_row": rate(sum(1 for m in ok if m["pattern"]["team_run_for"] >= 3), n),
            "this_team_conceded_3_in_a_row": rate(sum(1 for m in ok if m["pattern"]["team_run_against"] >= 3), n),
            "led_by_1_at_any_point": r("led_by_1"), "led_by_2_at_any_point": r("led_by_2"), "won_in_regular_time": r("won"),
            "led_by_2_or_won (2UP wins)": r("up2_win"), "scored_first": r("scored_first"), "conceded_first": r("conceded_first"),
            "comeback_wins": r("comeback_win"), "avoided_defeat_after_trailing": r("trailed_then_avoided_defeat"),
            "own_goals_in_sample": sum(m["pattern"]["own_goals"] for m in ok),
            "per_match": [{"date": m["date"], "vs": m["vs"], "venue": m["venue"], "goal_order(team=own side letter)": m["pattern"]["goals"] if m["pattern"] else None,
                           "flags": ({k: v for k, v in m["pattern"].items() if k in ("any_3_in_a_row", "led_by_1", "led_by_2", "won")} if m["pattern"] else "goal events do not match final score")}
                          for m in matches]}

# ---------- first-half corners ----------
def _corner(stat_list):
    for s in stat_list or []:
        if s.get("type") == "Corner Kicks":
            try: return int(str(s.get("value")).strip())
            except (TypeError, ValueError): return None
    return None

def first_half_corners(half_raw, home_id, away_id):
    """First-half corners for one match, or an 'unverified' result. We ONLY accept values that the response labels as first-half data.
    Plain 'statistics' of a half=true call are not trusted: their meaning is not documented enough to assume, and full-match
    numbers must never be passed off as first-half numbers."""
    found = {}
    for e in half_raw or []:
        tid = (e.get("team") or {}).get("id")
        for k, v in e.items():
            if k in ("team", "statistics") or not isinstance(v, list): continue
            if any(w in k.lower() for w in ("half", "1h", "first")):
                c = _corner(v)
                if c is not None: found[tid] = c
    if home_id in found and away_id in found:
        return {"home": found[home_id], "away": found[away_id], "total": found[home_id] + found[away_id], "verified": True}
    return {"verified": False, "reason": "the response did not contain explicitly labelled first-half corner data"}

def corner_report(rows, lines=(3.5, 4.5, 5.5)):
    """rows: [{'total': int}] of verified matches."""
    vals = [r["total"] for r in rows]
    return {"matches": len(vals), "avg_total": round(sum(vals) / len(vals), 2) if vals else None,
            **{f"over_{str(l).replace('.', '_')}": hit_rate(vals, l, "over") for l in lines}} if vals else {"matches": 0}
