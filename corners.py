"""Corner history from API-Football (api-sports.io), the source that carries 'Corner Kicks' per match.
Needs API_FOOTBALL_KEY in .env. The free plan is small (about 100 requests/day), so everything is cached."""
import os, re, httpx
from cache import cached

BASE = "https://v3.football.api-sports.io"

def _g(path, params, ttl):
    key = os.getenv("API_FOOTBALL_KEY")
    if not key: raise RuntimeError("Corner stats need an API-Football key. Add API_FOOTBALL_KEY to .env.")
    def call():
        r = httpx.get(BASE + path, params=params, headers={"x-apisports-key": key}, timeout=25,
                      proxy=os.getenv("API_FOOTBALL_PROXY") or None)  # route through your dedicated egress service
        r.raise_for_status(); j = r.json()
        if j.get("errors"): raise RuntimeError(f"API-Football: {j['errors']}")
        return j["response"]
    return cached("af:" + path + repr(sorted(params.items())), ttl, call)

def _n(s): return re.sub(r"[^a-z0-9]", "", s.lower())

def team_corners(team, n=8):
    found = _g("/teams", {"search": team}, 604800)
    if not found: raise LookupError(f"{team} not found on API-Football")
    t = next((x for x in found if _n(x["team"]["name"]) == _n(team)), found[0])["team"]
    rows = []
    for f in _g("/fixtures", {"team": t["id"], "last": n}, 21600):
        mine = opp = None
        for s in _g("/fixtures/statistics", {"fixture": f["fixture"]["id"]}, 2592000):
            c = next((x["value"] for x in s["statistics"] if x["type"] == "Corner Kicks"), None)
            if s["team"]["id"] == t["id"]: mine = c
            else: opp = c
        if mine is None or opp is None: continue
        home = f["teams"]["home"]["id"] == t["id"]
        rows.append({"date": f["fixture"]["date"][:10], "venue": "H" if home else "A", "vs": f["teams"]["away" if home else "home"]["name"],
                     "corners_for": mine, "corners_against": opp, "total": mine + opp})
    avg = lambda k: round(sum(r[k] for r in rows) / len(rows), 1) if rows else None
    return {"team": t["name"], "matches": rows, "avg_total": avg("total"), "avg_for": avg("corners_for"), "avg_against": avg("corners_against")}
