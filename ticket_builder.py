"""Target-odds ticket builder. Pure maths: picks the combination of vetted candidates whose real product of odds lands closest to the
target while keeping the joint probability as high as possible. It never forces a weak selection to hit a number."""
import math
from decimal import Decimal, ROUND_HALF_UP

def product(odds):
    p = Decimal(1)
    for o in odds: p *= Decimal(str(o))
    return float(p.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

def _quality(x):
    p = x.get("probability")
    q = p if isinstance(p, (int, float)) and 0 < p < 1 else (x.get("confidence", 50) / 100) * 0.9
    return math.log(min(0.99, max(0.01, q)))

def _match_key(x): return " ".join(f'{x["home"]} {x["away"]}'.lower().split())

def build(candidates, target, tolerance=0.06, max_legs=12, existing=None, per_match=1, min_confidence=50):
    """-> {'reached': bool, 'legs': [...], 'combined_odds': float, ...}. `existing` legs are kept and counted in the product."""
    existing = existing or []
    base = product([x["odds"] for x in existing]) if existing else 1.0
    if target <= 1: return {"reached": False, "reason": "The target must be above 1.00."}
    if base > target * (1 + tolerance):
        return {"reached": False, "legs": [], "combined_odds": base, "reason": f"The existing ticket is already {base:.2f}, above the {target:.2f} target. Remove selections instead."}
    need = target / base
    seen_match = {_match_key(x) for x in existing}
    pool = [x for x in candidates if x["odds"] > 1.0 and x.get("confidence", 0) >= min_confidence]
    if per_match == 1: pool = [x for x in pool if _match_key(x) not in seen_match]
    pool.sort(key=lambda x: -x.get("confidence", 0))
    pool = pool[:60]
    STEP, lim = 0.004, math.log(need * (1 + tolerance) * 1.2)
    states = {0: (0.0, 0.0, ())}          # bucket -> (joint quality, log product, picked indices)
    for i, x in enumerate(pool):
        lo, q = math.log(x["odds"]), _quality(x)
        add = {}
        for b, (sc, tl, idx) in states.items():
            if len(idx) >= max_legs or tl + lo > lim: continue
            if per_match == 1 and any(_match_key(pool[j]) == _match_key(x) for j in idx): continue
            nt = tl + lo
            nb = round(nt / STEP)
            if (nb not in states or sc + q > states[nb][0]) and (nb not in add or sc + q > add[nb][0]): add[nb] = (sc + q, nt, idx + (i,))
        states.update(add)
    scored = []
    for sc, tl, idx in states.values():
        if not idx: continue
        total = product([pool[j]["odds"] for j in idx])
        scored.append((abs(math.log(total / need)), -sc, len(idx), idx, total))
    inside = [s for s in scored if s[0] <= math.log(1 + tolerance) + 1e-9]
    if inside:
        best = min(inside, key=lambda s: (s[1], s[2]))
        legs = [pool[j] for j in best[3]]
        combined = product([x["odds"] for x in existing + legs])
        return {"reached": True, "legs": legs, "combined_odds": combined, "target": target, "deviation_pct": round((combined / target - 1) * 100, 2),
                "legs_used": len(legs), "candidates_considered": len(pool)}
    near = min(scored, key=lambda s: (s[0], s[1])) if scored else None
    out = {"reached": False, "target": target, "candidates_considered": len(pool),
           "reason": "No combination of the vetted candidates lands within the tolerance without using weaker selections or breaking the one-per-match rule."}
    if near:
        legs = [pool[j] for j in near[3]]
        combined = product([x["odds"] for x in existing + legs])
        out.update(closest={"legs": legs, "combined_odds": combined, "deviation_pct": round((combined / target - 1) * 100, 2)})
    return out
