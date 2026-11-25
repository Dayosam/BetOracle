"""Routes a pasted booking code to the right reader and a requested destination to the right creator.
Direct adapters (SportyBet, MSport) copy Sportradar ids. Everything else goes through the Manage Bet Codes provider.
Nothing is dropped silently: every skipped, suspended or missing selection is named in the result."""
import math, re
from difflib import SequenceMatcher
import bookmakers as bm, sportybet, msport, managebetcodes as mbc

class Ambiguous(Exception): pass

DIRECT_READ = {"sportybet": lambda c: {"legs": sportybet.read_code(c), "suspended": [], "not_listed": 0},
               "msport": lambda c: (lambda r: {"legs": r["legs"], "suspended": r["suspended"], "not_listed": r["not_listed_by_msport"]})(msport.read_msport_code(c))}
DIRECT_CREATE = {"sportybet": sportybet.book, "msport": msport.create_msport_code}

def prod(legs): return round(math.prod(x["odds"] for x in legs), 2) if legs else 0
def names(legs): return [f'{x["home"]} v {x["away"]}: {x["market"]} {x["pick"]} @ {x["odds"]}' for x in legs]

def _provider_ok(p): return mbc.enabled() and mbc.provider_key(p, bm.label(p)) is not None
def can_read(p): return p in DIRECT_READ or _provider_ok(p)
def can_create(p): return p in DIRECT_CREATE or _provider_ok(p)

def why_not(p):
    if p in bm.NO_ROUTE: return f"No route to {bm.label(p)} exists yet: no provider I found offers it and no direct endpoint is documented."
    if not mbc.enabled(): return f"{bm.label(p)} goes through the Manage Bet Codes provider, and its key (MBC_API_KEY) is not set."
    return f"{bm.label(p)} could not be matched to a provider bookmaker."

def _read_one(p, code):
    if p in DIRECT_READ: return DIRECT_READ[p](code)
    v = mbc.view(code, mbc.provider_key(p, bm.label(p)), bm.label(p))
    legs = [l for l in v["legs"] if l["bookable"]]; susp = [l for l in v["legs"] if not l["bookable"]]
    return {"legs": legs, "suspended": susp, "not_listed": 0}

def read(code, given=None):
    """-> (bookmaker key, slip). Uses the bookmaker the user named, otherwise tries every bookmaker whose code format fits."""
    if given:
        p = bm.canon(given)
        if not p: raise ValueError(f"I don't recognise '{given}' as a bookmaker.")
        if not can_read(p): raise ValueError(why_not(p))
        return p, _read_one(p, code)
    hits, errs, skipped = {}, {}, []
    for p in bm.candidates(code):
        if not can_read(p): skipped.append(bm.label(p)); continue
        try: hits[p] = _read_one(p, code)
        except Exception as e: errs[p] = f"{type(e).__name__}: {e}"
    if len(hits) == 1: return next(iter(hits.items()))
    if hits: raise Ambiguous(list(hits))
    hint = f" Formats that fit but are not available yet: {', '.join(skipped)}." if skipped else ""
    raise LookupError(f"That code was not found on any bookmaker I could try.{hint} Details: {errs}")

def _n(s): return re.sub(r"[^a-z0-9 ]", "", (s or "").lower())
def _same(a, b):
    """Strict team-name match: near-identical, or one name's words all appear in the other ('Brugge' / 'Club Brugge')."""
    a, b = _n(a), _n(b)
    if not a or not b: return False
    ta, tb = set(a.split()), set(b.split())
    return SequenceMatcher(None, a, b).ratio() >= 0.8 or ta <= tb or tb <= ta

def fixtures_missing(src, tgt):
    """Source selections whose fixture cannot be matched in the new code. This errs towards reporting: if team names are spelled
    very differently (e.g. 'Man Utd' / 'Manchester United') a present match is listed as missing so the user checks it."""
    return [f'{l["home"]} v {l["away"]}: {l["market"]} {l["pick"]}' for l in src
            if not any(_same(l["home"], t["home"]) and _same(l["away"], t["away"]) for t in tgt)]

def _create(code, src, tgt, legs, skipped):
    if src in DIRECT_READ and tgt in DIRECT_CREATE:
        return {**DIRECT_CREATE[tgt](legs), "route": "direct"}
    ks, kt = mbc.provider_key(src, bm.label(src)), mbc.provider_key(tgt, bm.label(tgt))
    if not (mbc.enabled() and ks and kt): raise RuntimeError(why_not(tgt if not kt else src))
    r = mbc.convert(code, ks, kt)
    out = {"code": r["code"], "route": "provider", "provider_counts": r["counts"], "provider_partial": r["partial"], "provider_verified": r["verified"]}
    try:  # independent read-back of the new code
        back = mbc.view(r["code"], kt, bm.label(tgt))["legs"]
        out.update(verified=True, missing=fixtures_missing(legs + skipped, back),
                   new_code_selections=[f'{t["home"]} v {t["away"]}: {t["market"]} {t["pick"]} @ {t["odds"]}' for t in back])
    except Exception as e:
        out.update(verified=False, verify_error=f"{type(e).__name__}: {e}")
    return out

def read_for_chat(code, platform=None):
    try: src, slip = read(code, platform)
    except Ambiguous as e: return {"needs_confirmation": True, "message": f"That code is valid on {', '.join(bm.label(p) for p in e.args[0])}. Ask the user which bookmaker it is from."}
    legs = slip["legs"]
    return {"platform": bm.label(src), "legs": len(legs), "total_odds": prod(legs), "selections": names(legs), "suspended_legs": names(slip["suspended"]),
            "not_listed_by_source": slip["not_listed"], "legs_list": legs}

def convert_for_chat(code, source, target, allow_partial=False):
    tgt = bm.canon(target)
    if not tgt: return {"error": f"I don't recognise '{target}' as a bookmaker. Known: {', '.join(v['label'] for v in bm.BOOKMAKERS.values())}."}
    try: src, slip = read(code, source)
    except Ambiguous as e: return {"needs_confirmation": True, "message": f"That code is valid on {', '.join(bm.label(p) for p in e.args[0])}. Ask the user which bookmaker it is from."}
    legs, skipped, unlisted = slip["legs"], slip["suspended"], slip["not_listed"]
    if src == tgt: return {"note": f"That code is already a {bm.label(src)} code."}
    if not can_create(tgt):
        return {"ticket": legs, "error": f"Cannot create a {bm.label(tgt)} code yet. {why_not(tgt)} The {bm.label(src)} code was read: show its selections and say so plainly.", "selections": names(legs)}
    if not legs: return {"error": "None of the legs in that code can be booked (suspended, started or not listed).", "suspended_legs": names(skipped)}
    if (skipped or unlisted) and not allow_partial:
        return {"ticket": legs, "needs_confirmation": True, "bookable_legs": names(legs), "suspended_legs": names(skipped), "not_listed_by_source": unlisted,
                "message": "Some selections cannot be converted. Tell the user exactly which, and only retry with allow_partial=true if they agree."}
    res = _create(code, src, tgt, legs, skipped)
    warn = []
    if res.get("missing"): warn.append(f"{len(res['missing'])} selection(s) could not be found in the new code (check each; team names may be spelled differently): {res['missing']}")
    if res.get("verified") is False: warn.append(f"The new code could not be read back, so its legs are unconfirmed ({res.get('verify_error', 'direct route')}).")
    if res.get("provider_partial"): warn.append(f"The provider reports a partial conversion: {res.get('provider_counts')}.")
    new = {"code": res["code"], "games": len(legs), "odds": prod(legs)}
    return {**new, "from": bm.label(src), "to": bm.label(tgt), "route": res["route"], "converted_legs": names(legs), "skipped_suspended_legs": names(skipped),
            "not_listed_by_source": unlisted, "verified": res.get("verified", res.get("legs_confirmed") is not None), "missing_from_new_code": res.get("missing", []),
            "new_code_selections": res.get("new_code_selections"), "warnings": warn,
            "odds_note": "Odds shown are the source bookmaker's; the target may price them differently.", "ticket": legs, "new_code": new}
