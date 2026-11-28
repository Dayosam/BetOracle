import os, re, time, unicodedata, httpx
from datetime import date, datetime, timedelta, timezone
from cache import cached, count_call

BASE = "https://sportapi.ai/api"
H = lambda: {"X-Api-Key": os.getenv("SPORTAPI_KEY", "")}
H1, H6, D1, D7 = 1800, 21600, 86400, 604800  # cache lifetimes in seconds

# ---------------------------------------------------------------------------
# BACKUP API: goal-api.com
# Used automatically when SportAPI says "limit reached" (HTTP 429).
# Needs the environment variable GOALAPI_KEY (Render -> Environment).
# Set FORCE_BACKUP=1 to test the backup even when SportAPI still works.
# ---------------------------------------------------------------------------
GOAL_BASE = "https://api.goal-api.com/v1"
GOAL_FIN = ("FINISHED", "AFTER_ET", "AFTER_PEN")

_LIMIT = {"day": None}      # UTC day on which SportAPI said "limit reached"
_ID_BY_SLUG = {}            # team slug -> goal-api team id (filled when fixtures are loaded)

try:
    from zoneinfo import ZoneInfo
    _BERLIN = ZoneInfo("Europe/Berlin")   # goal-api's matchDate / matchTime are in this timezone
except Exception:
    _BERLIN = None


class _Limit(Exception):
    pass


def _utc_day():
    return time.strftime("%Y-%m-%d", time.gmtime())


def _slug(name):
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _gget(path, params=None):
    key = os.getenv("GOALAPI_KEY", "")
    if not key:
        raise RuntimeError("SportAPI daily limit reached and no backup key is set (add GOALAPI_KEY).")
    r = httpx.get(GOAL_BASE + path, headers={"Authorization": "Bearer " + key}, params=params, timeout=25)
    if r.status_code == 429:
        raise RuntimeError("Both SportAPI and the backup goal-api have reached their daily limit.")
    r.raise_for_status()
    return r.json()


def _gpages(path, params, size=100, max_pages=12):
    out, off = [], 0
    for _ in range(max_pages):
        j = _gget(path, {**params, "limit": size, "offset": off})
        rows = j.get("data") or []
        out += rows
        if not rows or not (j.get("pagination") or {}).get("hasMore"):
            break
        off += size
    return out


def _kick_utc(f):
    if f.get("kickoffUtc"):
        return f["kickoffUtc"]
    d, t = f.get("matchDate"), f.get("matchTime")
    if not d or not t:
        return ""
    try:
        dt = datetime.fromisoformat(f"{d}T{t}:00")
        dt = dt.replace(tzinfo=_BERLIN if _BERLIN else timezone(timedelta(hours=2)))
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return ""


def _score(f, side):
    v = f.get(f"{side}TeamFtScore")
    if v in (None, ""):
        v = f.get(f"{side}TeamScore")
    try:
        return int(v)
    except Exception:
        return None


def _goal_fixtures(day):
    """Fixtures for one UTC day, in the same shape SportAPI returns."""
    d = date.fromisoformat(day)
    params = {"from": d.isoformat(), "to": (d + timedelta(days=1)).isoformat()}
    try:
        rows = _gpages("/fixtures", {**params, "status": "SCHEDULED"})
    except httpx.HTTPStatusError:
        rows = _gpages("/fixtures", params)

    out = []
    for f in rows:
        if f.get("matchStatus") != "SCHEDULED":
            continue
        ko = _kick_utc(f)
        if not ko or ko[:10] != day:
            continue
        hn, an = f.get("homeTeamName") or "", f.get("awayTeamName") or ""
        _ID_BY_SLUG[_slug(hn)] = f.get("homeTeamId")
        _ID_BY_SLUG[_slug(an)] = f.get("awayTeamId")
        out.append({
            "id": f.get("id"), "status": "Scheduled", "kickoff_utc": ko,
            "home_team": hn, "away_team": an,
            "home_id": f.get("homeTeamId"), "away_id": f.get("awayTeamId"),
            "league_id": f.get("leagueId"), "league_name": f.get("leagueName") or "",
        })
    return {"data": out}


def _goal_team_id(slug):
    tid = _ID_BY_SLUG.get(slug)
    if tid:
        return tid
    rows = _gget("/teams", {"search": slug.replace("-", " "), "limit": 10}).get("data") or []
    best = [r for r in rows if _slug(r.get("name")) == slug] or rows
    if not best:
        raise RuntimeError(f"backup: team '{slug}' not found")
    return best[0]["id"]


def _goal_results(slug):
    tid = _goal_team_id(slug)
    rows = _gget(f"/teams/{tid}/results", {"limit": 15}).get("data") or []
    out = []
    for m in rows:
        if m.get("matchStatus") not in GOAL_FIN:
            continue
        hs, as_ = _score(m, "home"), _score(m, "away")
        if hs is None or as_ is None:
            continue
        hn, an = m.get("homeTeamName") or "", m.get("awayTeamName") or ""
        out.append({
            "status": "FT", "date": m.get("matchDate") or "",
            "home_team": hn, "away_team": an,
            "home_slug": _slug(hn), "away_slug": _slug(an),
            "home_score": hs, "away_score": as_,
        })
    return {"results": out}


def _goal_standings(league_id):
    if str(league_id).isdigit():
        raise RuntimeError("backup: league id belongs to the other API")
    rows = _gget(f"/leagues/{league_id}/standings").get("data") or []
    data = []
    for r in rows:
        name = r.get("teamName") or ""
        _ID_BY_SLUG[_slug(name)] = r.get("teamId")
        data.append({"team": {"id": r.get("teamId"), "slug": _slug(name), "name": name}})
    return {"data": data}


def _goal_get(path):
    m = re.match(r"^/fixtures/date/(\d{4}-\d{2}-\d{2})$", path)
    if m:
        return _goal_fixtures(m.group(1))
    m = re.match(r"^/teams/slug/(.+)$", path)
    if m:
        return _goal_results(m.group(1))
    m = re.match(r"^/standings/(.+)$", path)
    if m:
        return _goal_standings(m.group(1))
    raise RuntimeError(f"backup API does not support {path}")


# ---------------------------------------------------------------------------
# MAIN GET (SportAPI first, goal-api backup)
# ---------------------------------------------------------------------------
def get(path, ttl):
    def call():
        use_primary = _LIMIT["day"] != _utc_day() and os.getenv("FORCE_BACKUP") != "1"

        if use_primary:
            try:
                count_call()
                r = httpx.get(BASE + path, headers=H(), timeout=20)
                if r.status_code == 429:
                    raise _Limit()
                r.raise_for_status()
                return r.json()
            except _Limit:
                _LIMIT["day"] = _utc_day()
                print("SPORTAPI LIMIT REACHED: switching to goal-api backup", flush=True)

        print("BACKUP API:", path, flush=True)
        return _goal_get(path)
    return cached("sportapi:" + path, ttl, call)


# ---------------------------------------------------------------------------
# everything below is unchanged
# ---------------------------------------------------------------------------
def days(a, b):
    d, e = date.fromisoformat(a), date.fromisoformat(b)
    return [(d + timedelta(n)).isoformat() for n in range(min((e - d).days + 1, 7))]

def fixtures(sport, date_from, date_to, league=None):
    out = []
    for d in days(date_from, date_to):
        if sport == "football":
            for f in get(f"/fixtures/date/{d}", H1).get("data", []):
                if f["status"] == "Scheduled":
                    out.append({"home": f["home_team"], "away": f["away_team"], "home_id": f["home_id"], "away_id": f["away_id"],
                                "kickoff": f["kickoff_utc"], "league": f["league_name"], "league_id": f["league_id"]})
        else:
            for g in get(f"/basketball/games/date/{d}", H1).get("data", []):
                if g["status"] == "NS":
                    out.append({"home": g["home"]["name"], "away": g["away"]["name"], "home_id": g["home"]["id"], "away_id": g["away"]["id"],
                                "kickoff": g["start"], "league": g["league"]["name"], "league_id": g["league"]["slug"]})
    if league: out = [x for x in out if league.lower() in x["league"].lower()]
    return out[:80]

FIN = ("FT", "AET", "PENS", "AOT")

def _fb_form(results, slug, side=None):
    rows = []
    for m in results:
        if m.get("status") not in FIN or m.get("home_score") is None: continue
        home = m.get("home_slug") == slug
        if m.get("home_slug") != slug and m.get("away_slug") != slug: continue
        if (side == "home" and not home) or (side == "away" and home): continue
        gf, ga = (m["home_score"], m["away_score"]) if home else (m["away_score"], m["home_score"])
        rows.append({"date": m["date"], "venue": "H" if home else "A", "vs": m["away_team"] if home else m["home_team"],
                     "score": f"{gf}-{ga}", "r": "W" if gf > ga else "D" if gf == ga else "L"})
    return sorted(rows, key=lambda x: x["date"], reverse=True)[:5]

def _bb_form(games, tid, side=None):
    rows = []
    for g in games:
        if g.get("status") not in FIN or g["home"]["score"] is None: continue
        home = g["home"]["id"] == tid
        if g["home"]["id"] != tid and g["away"]["id"] != tid: continue
        if (side == "home" and not home) or (side == "away" and home): continue
        pf, pa = (g["home"]["score"], g["away"]["score"]) if home else (g["away"]["score"], g["home"]["score"])
        rows.append({"date": g["start"][:10], "venue": "H" if home else "A", "vs": g["away"]["name"] if home else g["home"]["name"],
                     "score": f"{pf}-{pa}", "r": "W" if pf > pa else "L"})
    return sorted(rows, key=lambda x: x["date"], reverse=True)[:5]

def stats(sport, home_id, away_id, league_id):
    """Table numbers, last 5 for both teams (overall), home team's last 5 at home, away team's last 5 away, and H2H.
    Everything is cached, so repeat questions cost 0 requests."""
    if sport == "football":
        table = get(f"/standings/{league_id}", H6).get("data", [])
        h2h = get(f"/fixtures/h2h/{home_id}/{away_id}", D7).get("data", [])[:5]
        out = {}
        for key, tid, side in (("home", home_id, "home"), ("away", away_id, "away")):
            r = next((r for r in table if r["team"]["id"] == tid), None)
            if not r: out[key] = {"note": "team not found in league table (cup match or uncovered league)"}; continue
            res = get(f"/teams/slug/{r['team']['slug']}", H6).get("results", [])
            slug = r["team"]["slug"]
            out[key] = {"team": r["team"]["name"], "position": r["position"], "played": r["played"], "won": r["won"], "draw": r["draw"],
                        "lost": r["lost"], "goals_for": r["goals_for"], "goals_against": r["goals_against"],
                        "last5_overall": _fb_form(res, slug), ("last5_at_home" if side == "home" else "last5_away"): _fb_form(res, slug, side),
                        "results_available": len(res)}
        out["h2h"] = [f'{m["home_team"]} {m["home_score"]}-{m["away_score"]} {m["away_team"]} ({m["date"]})' for m in h2h]
        return out
    table = [r for grp in get(f"/basketball/standings/{league_id}", H6).get("data", []) for r in grp["table"]]
    h2h = get(f"/basketball/games/h2h/{home_id}/{away_id}?limit=5", D7).get("data", [])
    out = {}
    for key, tid, side in (("home", home_id, "home"), ("away", away_id, "away")):
        r = next((r for r in table if r["team"]["id"] == tid), None)
        games = get(f"/basketball/teams/{tid}/games?limit=30", H6).get("data", [])
        out[key] = {**({"team": r["team"]["name"], "position": r["position"], "won": r["won"], "lost": r["lost"],
                        "points_for": r["points_for"], "points_against": r["points_against"], "streak": r["streak"]} if r else {}),
                    "last5_overall": _bb_form(games, tid), ("last5_at_home" if side == "home" else "last5_away"): _bb_form(games, tid, side)}
    out["h2h"] = [f'{m["home"]["name"]} {m["home"]["score"]}-{m["away"]["score"]} {m["away"]["name"]}' for m in h2h if m["home"]["score"] is not None]
    return out

KEYS = ("shots", "shots_on_target", "fouls", "tackles", "yellow_cards")

def _team_slug(league_id, team_id):
    for r in get(f"/standings/{league_id}", H6).get("data", []):
        if r["team"]["id"] == team_id: return r["team"]["slug"], r["team"]["name"]
    raise LookupError("team not found in that league table")

def team_history(team_id, league_id, n=8):
    """Per-match shots, shots on target, fouls, tackles and yellow cards (for and against) over a team's last n matches.
    Finished matches never change, so each match's stats are cached for 30 days."""
    slug, name = _team_slug(league_id, team_id)
    res = [m for m in get(f"/teams/slug/{slug}", H6).get("results", []) if m.get("status") in FIN]
    rows = []
    for m in sorted(res, key=lambda m: m["date"], reverse=True)[:n]:
        st = get(f"/fixtures/{m['id']}/stats", 2592000)
        if not st.get("available"): continue
        home = m["home_slug"] == slug
        mine, opp = st["data"]["home" if home else "away"], st["data"]["away" if home else "home"]
        rows.append({"date": m["date"], "venue": "H" if home else "A", "vs": m["away_team"] if home else m["home_team"],
                     **{f"{k}_for": mine.get(k) for k in KEYS}, **{f"{k}_against": opp.get(k) for k in KEYS}})
    avg = {f"avg_{k}_for": round(sum(r[f"{k}_for"] or 0 for r in rows) / len(rows), 1) for k in KEYS} if rows else {}
    return {"team": name, "matches": rows, **avg, "note": "sportapi.ai has no corner data; use get_corner_history for corners."}
