"""SportyBet Nigeria adapter. SportyBet has no public API: this talks to the same JSON endpoints its own website uses.
They are unofficial and can change. Run `python test_sportybet.py` first to confirm they still answer as expected.

Changes in this version:
- HTTP 202 (SportyBet's bot-protection challenge) now fails immediately with a clear message instead of retrying.
- Optional SPORTYBET_PROXY environment variable: route SportyBet requests through a proxy
  (for example http://user:pass@host:port) when the server's own IP is blocked.
- Fetching the event list is serialised with a lock, so parallel requests no longer hammer SportyBet."""
import os, re, time, threading, httpx
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from cache import cached

BASE = "https://www.sportybet.com/api/ng"
HEAD = {"accept": "application/json", "content-type": "application/json", "current-country": "NG",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
SPORTS = {"football": "sr:sport:1", "basketball": "sr:sport:2", "baseball": "sr:sport:3", "ice hockey": "sr:sport:4", "tennis": "sr:sport:5",
          "handball": "sr:sport:6", "rugby": "sr:sport:12", "snooker": "sr:sport:19", "table tennis": "sr:sport:20",
          "cricket": "sr:sport:21", "darts": "sr:sport:22", "volleyball": "sr:sport:23"}
MAIN_IDS = "1,18,10,11,29,16,225,219"
LAGOS = timezone(timedelta(hours=1))

PROXY = os.getenv("SPORTYBET_PROXY") or None
_PX = {"proxy": PROXY} if PROXY else {}
_EV_LOCK = threading.Lock()

BLOCKED_MSG = ("SportyBet bot protection (HTTP 202) is blocking this server's connection. "
               "Run BetOracle from your own computer, or set SPORTYBET_PROXY.")

def _get(path, params=None, ttl=300):
    def call():
        for wait in (0, 6, 20):
            time.sleep(wait)
            try:
                r = httpx.get(BASE + path, params=params, headers=HEAD, timeout=15, **_PX)
            except httpx.RequestError as e:
                print(f"SPORTYBET NETWORK ERROR: {e}", flush=True)
                continue

            if r.status_code == 202:
                print("SPORTYBET BLOCKED: HTTP 202 (bot-protection challenge)", flush=True)
                raise RuntimeError(BLOCKED_MSG)

            if r.status_code == 429:
                print("SPORTYBET RATE LIMIT (429)", flush=True)
                continue

            if r.status_code != 200:
                print(f"SPORTYBET ERROR: HTTP {r.status_code} - {r.text[:200]}", flush=True)
                continue

            try:
                j = r.json()
            except Exception:
                print(f"SPORTYBET ERROR: Invalid JSON. Response text: {r.text[:500]}", flush=True)
                continue

            if j.get("bizCode") == 19411:
                print("SPORTYBET BIZCODE 19411 (Rate Limit)", flush=True)
                continue

            if j.get("bizCode") != 10000:
                print(f"SPORTYBET BIZCODE ERROR: {j.get('bizCode')} - {j.get('message')}", flush=True)
                raise RuntimeError(f"SportyBet answered {j.get('bizCode')}: {j.get('message')}")

            return j["data"]

        raise RuntimeError("SportyBet is rate limiting or blocking this server. Wait a few minutes and retry.")
    return cached("sb:" + path + repr(sorted((params or {}).items())), ttl, call)

def _events_locked(sport):
    sid = SPORTS.get(sport.lower())
    if not sid: raise ValueError(f"Unknown sport '{sport}'. Known: {', '.join(SPORTS)}")
    out, page = [], 1
    while page <= 40:
        d = _get("/factsCenter/pcUpcomingEvents", {"sportId": sid, "marketId": MAIN_IDS, "pageSize": 100, "pageNum": page, "option": 1}, 600)
        for t in d.get("tournaments") or []:
            for e in t.get("events") or []:
                out.append({"event_id": e["eventId"], "home": e["homeTeamName"], "away": e["awayTeamName"],
                            "ms": e["estimateStartTime"], "league": f'{t.get("categoryName", "")} {t.get("name", "")}'.strip()})
        if page * 100 >= d.get("totalNum", 0) or not d.get("tournaments"): break
        page += 1
    return out

def _events(sport):
    # one thread at a time: the first call fills the cache, the others reuse it
    with _EV_LOCK:
        return _events_locked(sport)

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
    d = _get("/factsCenter/event", {"eventId": ev["event_id"]}, 300)
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
        items.append({"eventId": eid, "marketId": mid, "outcomeId": oid, **({"specifier": spec} if spec else {})})
    r = httpx.post(BASE + "/orders/share", json={"selections": items}, headers=HEAD, timeout=25, **_PX)
    if r.status_code == 202: raise RuntimeError(BLOCKED_MSG)
    if r.status_code == 429: raise RuntimeError("SportyBet is rate limiting booking requests. Wait a few minutes.")
    r.raise_for_status()
    j = r.json()
    if j.get("bizCode") != 10000: raise RuntimeError(f"SportyBet refused the booking ({j.get('bizCode')}: {j.get('message')}).")
    d = j["data"]
    if d.get("unavailableOutcomes"): raise RuntimeError(f'{len(d["unavailableOutcomes"])} leg(s) are unavailable (suspended or already started). Replace them and book again.')
    outs = d.get("outcomes")
    return {"code": d["shareCode"], "total_odds": None, "url": d.get("shareURL"), "deadline": d.get("deadline"),
            "legs_confirmed": len(outs) if isinstance(outs, list) else None}

def read_code(code):
    """Load the legs of an existing SportyBet booking code."""
    d = _get(f"/orders/share/{code}", None, 60)
    legs = []
    for o in d.get("outcomes") or []:
        mk = (o.get("markets") or [{}])[0]; oc = (mk.get("outcomes") or [{}])[0]
        legs.append({"home": o.get("homeTeamName"), "away": o.get("awayTeamName"), "market": mk.get("desc"), "pick": oc.get("desc"),
                     "odds": float(oc.get("odds") or 0), "ref": f'{o.get("eventId")}|{mk.get("id")}|{mk.get("specifier") or ""}|{oc.get("id")}',
                     "reason": "Loaded from an existing booking code.", "sport": "loaded"})
    return legs
