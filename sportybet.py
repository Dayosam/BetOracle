"""SportyBet Nigeria adapter for BetOracle.

SportyBet has no public API: this talks to the same JSON endpoints its own website uses.
They are unofficial and can change.

This version sends requests EXACTLY the way the working SportyTips provider does:
- Python's built-in urllib (no httpx) with the same headers: Android Chrome user agent
  plus the clientid / operid / platform headers SportyBet's own web app sends.
- The same parameters for the event list and event pages.
- HTTP 202 (SportyBet's bot-protection answer) fails immediately with a clear message.
- Optional SPORTYBET_PROXY environment variable (http://user:pass@host:port).
- The event list is fetched under a lock, so parallel requests do not hammer SportyBet.
- The event list (with the GG and Over/Under odds that come with it) is also kept in this
  process's own memory for 30 minutes, and each event page for 5 minutes, so repeat requests
  cost nothing even if cache.py does not keep them.
"""
import os, re, json, time, threading
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen, build_opener, ProxyHandler
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from cache import cached

BASE = "https://www.sportybet.com/api/ng"

HEAD = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "clientid": "web",
    "operid": "2",
    "platform": "web",
}

SPORTS = {"football": "sr:sport:1", "basketball": "sr:sport:2", "baseball": "sr:sport:3", "ice hockey": "sr:sport:4", "tennis": "sr:sport:5",
          "handball": "sr:sport:6", "rugby": "sr:sport:12", "snooker": "sr:sport:19", "table tennis": "sr:sport:20",
          "cricket": "sr:sport:21", "darts": "sr:sport:22", "volleyball": "sr:sport:23"}

MAIN_IDS = "1,18,10,11,29,16,225,219"      # markets shown by default in get_markets
LIST_IDS = "1,10,18,29"                    # markets requested for the event list (same as SportyTips)
LAGOS = timezone(timedelta(hours=1))
TIMEOUT = 20

PROXY = os.getenv("SPORTYBET_PROXY") or None
_OPEN = build_opener(ProxyHandler({"http": PROXY, "https": PROXY})).open if PROXY else urlopen
_EV_LOCK = threading.Lock()
EVENTS_TTL = int(os.getenv("SPORTYBET_EVENTS_TTL", "1800"))   # seconds the event list is reused
_EV_MEM = {}      # sport -> (time, events)
_MK_MEM = {}      # event id -> (time, event page data)

BLOCKED_MSG = ("SportyBet bot protection (HTTP 202) is blocking this server's connection. "
               "Run BetOracle from your own computer, or set SPORTYBET_PROXY.")


class _Retry(Exception):
    """Temporary problem (rate limit, network, bad JSON): worth trying again."""


def _http(method, path, params=None, body=None):
    url = BASE + path + ("?" + urlencode(params) if params else "")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = Request(url, data=data, headers=HEAD, method=method)

    try:
        with _OPEN(req, timeout=TIMEOUT) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8", "replace")
    except HTTPError as e:
        if e.code == 429:
            raise _Retry("HTTP 429 (rate limit)")
        print(f"SPORTYBET ERROR: HTTP {e.code} on {path}", flush=True)
        raise RuntimeError(f"SportyBet answered HTTP {e.code} on {path}")
    except (URLError, OSError) as e:
        raise _Retry(f"network error: {e}")

    if status == 202 or not raw.strip():
        print("SPORTYBET BLOCKED: HTTP 202 / empty body (bot protection)", flush=True)
        raise RuntimeError(BLOCKED_MSG)

    try:
        return json.loads(raw)
    except ValueError:
        raise _Retry(f"invalid JSON: {raw[:200]}")


def _get(path, params=None, ttl=300):
    def call():
        last = ""
        for wait in (0, 4, 10):
            time.sleep(wait)
            try:
                j = _http("GET", path, params)
            except _Retry as e:
                last = str(e)
                print(f"SPORTYBET RETRY: {last}", flush=True)
                continue

            if j.get("bizCode") == 19411:
                last = "bizCode 19411 (rate limit)"
                print("SPORTYBET BIZCODE 19411 (Rate Limit)", flush=True)
                continue

            if j.get("bizCode") not in (None, 10000):
                print(f"SPORTYBET BIZCODE ERROR: {j.get('bizCode')} - {j.get('message')}", flush=True)
                raise RuntimeError(f"SportyBet answered {j.get('bizCode')}: {j.get('message')}")

            return j.get("data")

        raise RuntimeError(f"SportyBet is rate limiting or unreachable ({last}). Wait a few minutes and retry.")
    if ttl <= 0:      # kept in this process's memory by the caller, not in cache.py / Upstash
        return call()
    return cached("sb:" + path + repr(sorted((params or {}).items())), ttl, call)


def _events_locked(sport):
    sid = SPORTS.get(sport.lower())
    if not sid: raise ValueError(f"Unknown sport '{sport}'. Known: {', '.join(SPORTS)}")
    out, page = [], 1
    while page <= 40:
        d = _get("/factsCenter/pcUpcomingEvents",
                 {"sportId": sid, "marketId": LIST_IDS, "pageSize": 100, "pageNum": page, "todayGames": "false"}, 0) or {}
        for t in d.get("tournaments") or []:
            for e in t.get("events") or []:
                out.append({"event_id": e["eventId"], "home": e["homeTeamName"], "away": e["awayTeamName"],
                            "ms": e["estimateStartTime"], "league": f'{t.get("categoryName", "")} {t.get("name", "")}'.strip(),
                            "markets": e.get("markets") or []})
        if page * 100 >= d.get("totalNum", 0) or not d.get("tournaments"): break
        page += 1
    return out


def _events(sport):
    # one thread at a time: the first call fills the cache, the others reuse it
    with _EV_LOCK:
        hit = _EV_MEM.get(sport.lower())
        if hit and time.time() - hit[0] < EVENTS_TTL:
            return hit[1]
        data = _events_locked(sport)
        _EV_MEM[sport.lower()] = (time.time(), data)
        return data


# ids from the SportyTips provider (all verified there)
PICK_SPECS = {
    "gg":     ("29", "",           "74", "GG/NG",         "Yes"),
    "over25": ("18", "total=2.5",  "12", "Over/Under 2.5", "Over 2.5"),
    "over15": ("18", "total=1.5",  "12", "Over/Under 1.5", "Over 1.5"),
}


def market_pick(ev, key):
    """Odds + ref for GG / Over 2.5 / Over 1.5 straight from the event list. None if not listed."""
    mid, spec, oid, mname, pname = PICK_SPECS[key]
    for m in ev.get("markets") or []:
        if str(m.get("id")) != mid or (m.get("specifier") or "") != spec:
            continue
        if m.get("status") not in (None, 0):
            continue
        for o in m.get("outcomes") or []:
            if str(o.get("id")) != oid or o.get("isActive") in (False, 0):
                continue
            try:
                odds = float(o["odds"])
            except Exception:
                continue
            return {"odds": odds, "market": mname, "pick": pname,
                    "ref": f'{ev["event_id"]}|{mid}|{spec}|{oid}'}
    return None


def list_events(sport, date_from, date_to, league=None):
    out = []
    for e in _events(sport):
        t = datetime.fromtimestamp(e["ms"] / 1000, LAGOS)
        if date_from <= t.strftime("%Y-%m-%d") <= date_to and (not league or league.lower() in e["league"].lower()):
            out.append({"home": e["home"], "away": e["away"], "kickoff": t.strftime("%Y-%m-%d %H:%M") + " WAT", "league": e["league"]})
    return sorted(out, key=lambda x: x["kickoff"])[:120]


def _norm(s): return re.sub(r"[^a-z0-9 ]", "", s.lower())


def _find(sport, home, away):
    score = lambda e: (SequenceMatcher(None, _norm(home), _norm(e["home"])).ratio() + SequenceMatcher(None, _norm(away), _norm(e["away"])).ratio()) / 2
    best = max(_events(sport), key=score, default=None)
    if not best or score(best) < 0.6: raise LookupError(f"{home} v {away} not found on SportyBet")
    return best


def get_markets(sport, home, away, search=None):
    ev = _find(sport, home, away)
    hit = _MK_MEM.get(ev["event_id"])
    if hit and time.time() - hit[0] < 300:
        d = hit[1]
    else:
        d = _get("/factsCenter/event", {"eventId": ev["event_id"], "productId": 3}, 300) or {}
        _MK_MEM[ev["event_id"]] = (time.time(), d)
    main, rows = set(MAIN_IDS.split(",")), []
    for m in d.get("markets") or []:
        if m.get("status") != 0: continue
        name = m["desc"] + (f' ({m["specifier"]})' if m.get("specifier") else "")
        for o in m.get("outcomes") or []:
            if o.get("isActive", True):
                rows.append({"market": name, "pick": o["desc"], "odds": float(o["odds"]), "mid": str(m["id"]),
                             "ref": f'{ev["event_id"]}|{m["id"]}|{m.get("specifier") or ""}|{o["id"]}'})
    if search:
        kws = [k.strip().lower() for k in search.split(",") if k.strip()]
        shown = [r for r in rows if any(k in r["market"].lower() for k in kws)][:80]
    else:
        shown = [r for r in rows if r["mid"] in main][:80]
    names = sorted({r["market"].split(" (")[0] for r in rows})[:150]
    for r in shown: r.pop("mid")
    return {"event": f'{ev["home"]} v {ev["away"]}', "markets": shown, "all_market_names": names}


def book(selections):
    refs = [s.get("ref") for s in selections]
    if not refs or not all(refs): raise ValueError("Every selection needs the ref returned by get_markets.")
    if len(refs) > 50: raise ValueError("SportyBet allows at most 50 legs per code. Split the ticket first.")
    items = []
    for ref in refs:
        eid, mid, spec, oid = ref.split("|")
        items.append({"eventId": eid, "marketId": mid, "specifier": spec, "outcomeId": oid})

    last = ""
    for wait in (0, 4):
        time.sleep(wait)
        try:
            j = _http("POST", "/orders/share", None, {"selections": items})
            break
        except _Retry as e:
            last = str(e)
    else:
        raise RuntimeError(f"SportyBet booking failed ({last}). Wait a minute and try again.")

    if j.get("bizCode") not in (None, 10000): raise RuntimeError(f"SportyBet refused the booking ({j.get('bizCode')}: {j.get('message')}).")
    d = j.get("data") or {}
    if d.get("unavailableOutcomes"): raise RuntimeError(f'{len(d["unavailableOutcomes"])} leg(s) are unavailable (suspended or already started). Replace them and book again.')
    code = d.get("shareCode") or d.get("code")
    if not code: raise RuntimeError("SportyBet did not return a booking code.")
    outs = d.get("outcomes")
    return {"code": code, "total_odds": None, "url": d.get("shareURL"), "deadline": d.get("deadline"),
            "legs_confirmed": len(outs) if isinstance(outs, list) else None}


def read_code(code):
    """Load the legs of an existing SportyBet booking code."""
    d = _get(f"/orders/share/{code}", None, 60) or {}
    legs = []
    for o in d.get("outcomes") or []:
        mk = (o.get("markets") or [{}])[0]; oc = (mk.get("outcomes") or [{}])[0]
        legs.append({"home": o.get("homeTeamName"), "away": o.get("awayTeamName"), "market": mk.get("desc"), "pick": oc.get("desc"),
                     "odds": float(oc.get("odds") or 0), "ref": f'{o.get("eventId")}|{mk.get("id")}|{mk.get("specifier") or ""}|{oc.get("id")}',
                     "reason": "Loaded from an existing booking code.", "sport": "loaded"})
    return legs
