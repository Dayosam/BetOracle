"""API-Football (api-sports.io) client. Every call is cached (finished matches never change, so they are kept 30 days), counted against a
daily budget, and limited to a few concurrent requests. A budget or rate-limit problem raises BudgetExceeded so callers can return partial data."""
import os, re, threading, httpx
from cache import cached, count_call, peek_calls

BASE = "https://v3.football.api-sports.io"
DAY_LIMIT = int(os.getenv("API_FOOTBALL_DAILY_LIMIT", "100"))   # set this to YOUR plan's daily limit
_SEM = threading.BoundedSemaphore(max(1, int(os.getenv("API_FOOTBALL_MAX_CONCURRENT", "3"))))
DAY, MONTH = 21600, 2592000

class BudgetExceeded(RuntimeError): pass
class NotConfigured(RuntimeError): pass

def enabled(): return bool(os.getenv("API_FOOTBALL_KEY"))
def used_today(): return peek_calls("apifootball")
def _n(s): return re.sub(r"[^a-z0-9]", "", (s or "").lower())

def _g(path, params, ttl):
    key = os.getenv("API_FOOTBALL_KEY")
    if not key: raise NotConfigured("API-Football is not configured (API_FOOTBALL_KEY is missing).")
    def call():
        if used_today() >= DAY_LIMIT:
            raise BudgetExceeded(f"The API-Football daily budget ({DAY_LIMIT} requests) is used up. Only cached data is available until tomorrow (UTC).")
        count_call("apifootball")
        with _SEM:
            r = httpx.get(BASE + path, params=params, headers={"x-apisports-key": key}, timeout=25, proxy=os.getenv("API_FOOTBALL_PROXY") or None)
        if r.status_code == 429: raise BudgetExceeded("API-Football is rate limiting this server. Try again in a minute.")
        r.raise_for_status()
        j = r.json()
        if j.get("errors"):
            e = str(j["errors"])
            if any(w in e.lower() for w in ("limit", "rate", "quota", "requests")): raise BudgetExceeded(f"API-Football refused the request: {e}")
            raise RuntimeError(f"API-Football error: {e}")
        return j.get("response")
    return cached("af:" + path + repr(sorted(params.items())), ttl, call)

def find_team(name):
    found = _g("/teams", {"search": name}, 7 * DAY) or []
    if not found: raise LookupError(f"'{name}' was not found on API-Football.")
    t = next((x for x in found if _n(x["team"]["name"]) == _n(name)), found[0])["team"]
    return {"id": t["id"], "name": t["name"]}

def last_fixtures(team_id, n):
    """A team's last n finished matches (any competition), newest first. Scores are regular-time (90 minutes)."""
    rows = []
    for f in _g("/fixtures", {"team": team_id, "last": n}, DAY) or []:
        if f["fixture"]["status"]["short"] not in ("FT", "AET", "PEN"): continue
        sc = f.get("score") or {}
        ft = sc.get("fulltime") or {}
        if ft.get("home") is None: ft = f.get("goals") or {}
        ht = sc.get("halftime") or {}
        rows.append({"fid": f["fixture"]["id"], "date": f["fixture"]["date"][:10], "league": f["league"]["name"],
                     "home_id": f["teams"]["home"]["id"], "away_id": f["teams"]["away"]["id"],
                     "home": f["teams"]["home"]["name"], "away": f["teams"]["away"]["name"],
                     "ft": (ft.get("home"), ft.get("away")), "ht": (ht.get("home"), ht.get("away"))})
    return sorted(rows, key=lambda r: r["date"], reverse=True)

def _num(v):
    if v is None: return None
    try: return int(str(v).replace("%", "").strip())
    except ValueError: return None

def stats_map(fid):
    """Full-match statistics: {team_id: {'Corner Kicks': 5, 'Total Shots': 12, ...}}. Empty dict if the provider has none."""
    out = {}
    for e in _g("/fixtures/statistics", {"fixture": fid}, MONTH) or []:
        out[e["team"]["id"]] = {s["type"]: _num(s["value"]) for s in e.get("statistics") or []}
    return out

def half_stats_raw(fid):
    """Raw response of /fixtures/statistics?half=true (shape is not documented well enough to assume; see analysis.first_half_corners)."""
    return _g("/fixtures/statistics", {"fixture": fid, "half": "true"}, MONTH) or []

def events(fid):
    return _g("/fixtures/events", {"fixture": fid}, MONTH) or []
