"""
BetOracle: no AI, no Groq.

How a request works (built to use as few API calls as possible):
1. SportyBet's own match list (one cached download, with GG / Over odds included) is
   filtered by day and league and ranked by those odds. This costs ZERO SportAPI calls.
2. Only the best ~16 matches get their form checked on SportAPI (2 calls per match,
   remembered in memory, including failed lookups).
3. Matches that pass the form check are booked on SportyBet and you get the code.
"""

import os
import re
import math
import time
import traceback
import threading
import unicodedata

from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from concurrent.futures import ThreadPoolExecutor, wait

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import sportybet
import sportapi
import bookmakers
import convert_router as router

import cache
from cache import count_call


# =========================================================
# CONFIGURATION
# =========================================================

load_dotenv()

MAX_ANALYSE = int(os.getenv("MAX_ANALYSE", "32"))        # most matches whose form is checked per request
BATCH = int(os.getenv("BATCH", "8"))                     # matches checked per round (it stops once it has enough)
TOTAL_BUDGET = float(os.getenv("TOTAL_BUDGET", "80"))    # seconds for the whole request
FORM_BUDGET = float(os.getenv("FORM_BUDGET", "30"))      # seconds allowed for loading team stats
EVENTS_BUDGET = float(os.getenv("EVENTS_BUDGET", "50"))  # seconds allowed for SportyBet's match list
MARKET_BUDGET = float(os.getenv("MARKET_BUDGET", "20"))  # seconds allowed for extra SportyBet lookups
FORM_N = 10                                              # last N finished matches per team
MIN_FORM = 6                                             # need at least this many matches of data

WAT = timezone(timedelta(hours=1))                       # Nigeria time

SESSIONS = {}


# =========================================================
# SMALL HELPERS
# =========================================================

def prod(legs):
    return round(math.prod(x["odds"] for x in legs), 2) if legs else 0


def norm(x):
    return re.sub(r"[^a-z0-9]", "", str(x).lower())


def slugify(name):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def team_norm(name):
    n = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode().lower()
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    n = re.sub(r"\b(fc|cf|sc|afc|ac|as|fk|sk|club|de|the)\b", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def sim(a, b):
    return SequenceMatcher(None, team_norm(a), team_norm(b)).ratio()


def today():
    return datetime.now(WAT).date()


def iso_from_ms(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def ts_of(raw):
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def kick_short(f):
    raw = f.get("kickoff_utc") or ""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(WAT).strftime("%I:%M %p").lstrip("0").lower()
    except Exception:
        return ""


def kick_time(f):
    raw = f.get("kickoff_utc") or ""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(WAT).strftime("%a %d %b, %I:%M %p")
    except Exception:
        return raw


# =========================================================
# SPORTAPI WITH A MEMORY (saves your daily request limit)
# =========================================================

_MEM = {}
_MISS = object()


def sa_get(path, ttl):
    """sportapi.get with our own memory on top. Remembers failures too (a wrong
    team slug is not asked again), so a missing team never costs a call twice."""

    hit = _MEM.get(path)

    if hit and time.time() - hit[0] < hit[1]:
        if hit[2] is _MISS:
            raise LookupError("remembered: not found")
        return hit[2]

    try:
        data = sportapi.get(path, ttl)
    except Exception as e:
        msg = str(e)
        if "limit" in msg.lower():
            raise
        _MEM[path] = (time.time(), 86400 if "404" in msg else 900, _MISS)
        raise

    if len(_MEM) > 4000:
        _MEM.clear()

    _MEM[path] = (time.time(), ttl, data)
    return data


# =========================================================
# READING THE USER'S MESSAGE
# =========================================================

def parse_dates(t):
    d = today()

    if "tomorrow" in t:
        d = d + timedelta(days=1)
        return d, d, "tomorrow"

    if "weekend" in t:
        wd = d.weekday()
        if wd == 6:
            return d, d, "this weekend"
        sat = d + timedelta(days=(5 - wd) % 7)
        return sat, sat + timedelta(days=1), "this weekend"

    return d, d, "today"


def parse_market(t):
    if re.search(r"\b(gg|btts|bts|both teams|both team)\b", t):
        return "gg"
    if re.search(r"\bover\s*2\.?5\b|\bo2\.?5\b", t):
        return "over25"
    if re.search(r"\bover\s*1\.?5\b|\bo1\.?5\b", t):
        return "over15"
    if re.search(r"\b(double chance|dc|1x|x2|home or draw|draw or away)\b", t):
        return "dc"
    if re.search(r"\b(straight wins?|win only|wins only|home win|away win|match winner|1x2|to win|winners?)\b", t):
        return "win"
    return None


def parse_numbers(t):
    """Returns (count, target_odds). Either can be None."""

    target = None
    count = None

    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:total\s*)?odds?\b", t)
    if m:
        target = float(m.group(1))

    m = re.search(
        r"\b(\d{1,2})\s+(?:(?!odds?\b)[a-z0-9.+/]+\s+){0,3}?"
        r"(?:matches|match|games|game|picks|pick|legs|leg|teams|selections|fixtures)\b",
        t
    )
    if m:
        count = int(m.group(1))

    return count, target


# Only other continents' "Champions League" style cups are dropped for European requests.
# Nothing else is excluded: women's, youth and every other league stay in.
EXCLUDE = ("caf", "afc", "concacaf", "conmebol")

LEAGUE_GROUPS = [
    (("europa league",), ("europa league",), "Europa League"),
    (("conference league",), ("conference league", "europa conference"), "Conference League"),
    (("champions league",), ("champions league",), "Champions League"),
    (("european", "europe", "uefa", "euro comp"),
     ("champions league", "europa league", "conference league", "europa conference"),
     "European competitions"),
]

TOP_LEAGUES = {
    "premier league": ("premier league", "english premier league", "england premier league"),
    "la liga": ("la liga", "laliga", "spain la liga", "spain laliga"),
    "serie a": ("serie a", "italy serie a"),
    "bundesliga": ("bundesliga", "germany bundesliga"),
    "ligue 1": ("ligue 1", "france ligue 1"),
    "eredivisie": ("eredivisie", "netherlands eredivisie"),
    "championship": ("championship", "efl championship", "england championship"),
}


def mentions_league(t):
    for triggers, _, _ in LEAGUE_GROUPS:
        if any(k in t for k in triggers):
            return True
    return any(k in t for k in TOP_LEAGUES)


def pick_leagues(t, items):
    """Filter dicts that have a 'league_name' by the league the user mentioned.
    Returns (items, label)."""

    for triggers, words, label in LEAGUE_GROUPS:
        if any(k in t for k in triggers):
            out = []
            for f in items:
                n = f.get("league_name", "").lower()
                if any(w in n for w in words) and not any(x in n for x in EXCLUDE):
                    out.append(f)
            return out, label

    for key, variants in TOP_LEAGUES.items():
        if key in t:
            return [f for f in items if f.get("league_name", "").lower() in variants], key.title()

    names = {f.get("league_name", "") for f in items}
    hit = [n for n in names if len(n) >= 5 and n.lower() in t]
    if hit:
        return [f for f in items if f.get("league_name") in hit], ", ".join(hit)

    return items, "all leagues"


# ---- bookmakers and booking codes ----

def bookmaker_aliases():
    out = []
    for key, v in bookmakers.BOOKMAKERS.items():
        label = v.get("label", key) if isinstance(v, dict) else key
        al = {norm(key), norm(label), norm(label.split()[0])}
        if "sporty" in norm(key) or "sporty" in norm(label):
            al.add("sporty")
        out.append((key, {a for a in al if len(a) >= 4}))
    return out


def detect_bookmaker(t):
    """Find which bookmaker the user means. Text after ' to ' wins."""

    parts = re.split(r"\bto\b|\bon\b|\binto\b", t)
    zones = [parts[-1], t] if len(parts) > 1 else [t]

    for zone in zones:
        n = norm(zone)
        best = None
        for key, al in bookmaker_aliases():
            for a in al:
                if a in n and (best is None or len(a) > best[1]):
                    best = (key, len(a))
        if best:
            return best[0]
    return None


def detect_code(text):
    all_aliases = set()
    for _, al in bookmaker_aliases():
        all_aliases |= al

    for tok in re.findall(r"\b[A-Za-z0-9]{5,12}\b", text):
        if norm(tok) in all_aliases:
            continue
        if tok.lower() in ("over25", "over15", "under25"):
            continue
        if re.search(r"\d", tok) and re.search(r"[A-Za-z]", tok):
            return tok.upper()

    m = re.search(r"code[:\s]+([A-Za-z0-9]{5,12})\b", text, re.I)
    if m:
        return m.group(1).upper()

    return None


# =========================================================
# TEAM FORM FROM sportapi
# =========================================================

def lookup_slug(f, team_id):
    try:
        rows = sa_get(f"/standings/{f['league_id']}", sportapi.H6).get("data", [])
        for r in rows:
            if r["team"]["id"] == team_id:
                return r["team"]["slug"]
    except Exception:
        pass
    return None


def team_form(f, side):
    tid = f[f"{side}_id"]
    name = f[f"{side}_team"]

    def fetch_results(s):
        try:
            return sa_get(f"/teams/slug/{s}", sportapi.H6).get("results", [])
        except Exception:
            return []

    res, slug = [], None

    for s in dict.fromkeys(x for x in (f.get(f"{side}_slug"), slugify(name)) if x):
        res = fetch_results(s)
        if res:
            slug = s
            break

    if not res:
        s = lookup_slug(f, tid)
        if s:
            res = fetch_results(s)
            slug = s if res else None

    if not res:
        return None

    fin = [
        m for m in res
        if m.get("status") in sportapi.FIN
        and m.get("home_score") is not None
        and m.get("away_score") is not None
    ]
    fin.sort(key=lambda m: m.get("date", ""), reverse=True)

    rows = []

    for m in fin[:FORM_N]:
        home = m.get("home_slug") == slug or m.get("home_team") == name
        away = m.get("away_slug") == slug or m.get("away_team") == name

        if not (home or away):
            continue

        gf, ga = (
            (m["home_score"], m["away_score"]) if home
            else (m["away_score"], m["home_score"])
        )
        rows.append((gf, ga, "H" if home else "A"))

    n = len(rows)

    if n < MIN_FORM:
        return None

    sc = sum(1 for gf, ga, _ in rows if gf > 0)
    co = sum(1 for gf, ga, _ in rows if ga > 0)
    bt = sum(1 for gf, ga, _ in rows if gf > 0 and ga > 0)
    o15 = sum(1 for gf, ga, _ in rows if gf + ga >= 2)
    o25 = sum(1 for gf, ga, _ in rows if gf + ga >= 3)

    def wdl(sub):
        w = sum(1 for gf, ga, _ in sub if gf > ga)
        d = sum(1 for gf, ga, _ in sub if gf == ga)
        return {"n": len(sub), "w": w, "d": d, "l": len(sub) - w - d}

    return {
        "n": n, "sc": sc, "co": co, "bt": bt, "o15c": o15, "o25c": o25,
        "scored": sc / n, "conceded": co / n, "btts": bt / n,
        "o15": o15 / n, "o25": o25 / n,
        "all": wdl(rows),
        "H": wdl([r for r in rows if r[2] == "H"]),
        "A": wdl([r for r in rows if r[2] == "A"]),
    }


def load_forms(fixtures, budget=None):
    """Load team form in parallel, but never longer than FORM_BUDGET seconds.
    Returns (forms, unfinished_team_count)."""

    tasks = {}

    for f in fixtures:
        for side in ("home", "away"):
            tasks.setdefault(f[f"{side}_id"], (f, side))

    def work(item):
        tid, (f, side) = item
        try:
            return tid, team_form(f, side)
        except Exception as e:
            print("FORM ERROR:", f.get(f"{side}_team"), type(e).__name__, e, flush=True)
            return tid, None

    ex = ThreadPoolExecutor(max_workers=8)
    futs = [ex.submit(work, item) for item in tasks.items()]

    done, pending = wait(futs, timeout=budget or FORM_BUDGET)

    # slow requests keep running in the background and fill the memory for next time
    ex.shutdown(wait=False, cancel_futures=True)

    forms = {}

    for fu in done:
        try:
            tid, form = fu.result()
            forms[tid] = form
        except Exception:
            pass

    return forms, len(pending)


# =========================================================
# MARKETS: SCORING THE MATCHES
# =========================================================

MARKETS = {
    "gg": {
        "label": "GG (both teams to score)", "show": "BTTS Yes",
        "market": "GG/NG", "pick": "Yes",
        "search": "GG/NG,Both Teams To Score,BTTS",
    },
    "over25": {
        "label": "Over 2.5 goals", "show": "Over 2.5 Goals",
        "market": "Over/Under 2.5", "pick": "Over 2.5",
        "search": "Over/Under,Total",
    },
    "over15": {
        "label": "Over 1.5 goals", "show": "Over 1.5 Goals",
        "market": "Over/Under 1.5", "pick": "Over 1.5",
        "search": "Over/Under,Total",
    },
    "win_home": {
        "label": "Home win", "show": "Home Win",
        "market": "1X2", "pick": "Home", "search": "1X2",
    },
    "win_away": {
        "label": "Away win", "show": "Away Win",
        "market": "1X2", "pick": "Away", "search": "1X2",
    },
    "dc_1x": {
        "label": "Home or draw", "show": "Home or Draw",
        "market": "Double Chance", "pick": "Home or Draw", "search": "Double Chance",
    },
    "dc_x2": {
        "label": "Draw or away", "show": "Draw or Away",
        "market": "Double Chance", "pick": "Draw or Away", "search": "Double Chance",
    },
}

# what the user's word means -> which concrete markets are tried
GROUPS = {
    "gg": ["gg"],
    "over25": ["over25"],
    "over15": ["over15"],
    "win": ["win_home", "win_away"],
    "dc": ["dc_1x", "dc_x2"],
    "auto": ["gg", "over25", "over15", "win_home", "win_away", "dc_1x", "dc_x2"],
}

TITLES = {
    "gg": "GG ticket", "over25": "Over 2.5 ticket", "over15": "Over 1.5 ticket",
    "win": "Straight win ticket", "dc": "Double chance ticket", "auto": "Best picks ticket",
}


def venue_record(form, venue):
    """Wins/draws/losses at home (H) or away (A); falls back to all matches if fewer than 3."""
    r = form[venue]
    return (r, True) if r["n"] >= 3 else (form["all"], False)


def score_market(key, h, a, loose=False, odds=None):
    r = 0.1 if loose else 0.0          # how much to relax the thresholds

    if key == "gg":
        est = ((h["scored"] + a["conceded"]) / 2) * ((a["scored"] + h["conceded"]) / 2)
        score = 0.5 * est + 0.5 * ((h["btts"] + a["btts"]) / 2)
        thr = 0.55 - r
        ok = min(h["btts"], a["btts"]) >= 0.5 - r
    elif key == "over25":
        score = (h["o25"] + a["o25"]) / 2
        thr = 0.60 - r
        ok = min(h["o25"], a["o25"]) >= 0.5 - r
    elif key == "over15":
        score = (h["o15"] + a["o15"]) / 2
        thr = 0.80 - r
        ok = min(h["o15"], a["o15"]) >= 0.7 - r
    else:
        hr, _ = venue_record(h, "H")      # home team's record at home
        ar, _ = venue_record(a, "A")      # away team's record away
        hn, an = max(hr["n"], 1), max(ar["n"], 1)

        if key == "win_home":
            score = 0.5 * hr["w"] / hn + 0.5 * ar["l"] / an
            thr = 0.60 - r
            ok = hr["w"] / hn >= 0.5 - r and (odds is None or 1.20 <= odds <= 2.10)
        elif key == "win_away":
            score = 0.5 * ar["w"] / an + 0.5 * hr["l"] / hn
            thr = 0.60 - r
            ok = ar["w"] / an >= 0.5 - r and (odds is None or 1.20 <= odds <= 2.40)
        elif key == "dc_1x":
            score = 0.5 * (hr["w"] + hr["d"]) / hn + 0.5 * (ar["d"] + ar["l"]) / an
            thr = 0.80 - r
            ok = (hr["w"] + hr["d"]) / hn >= 0.7 - r and (odds is None or odds >= 1.12)
        else:  # dc_x2
            score = 0.5 * (ar["w"] + ar["d"]) / an + 0.5 * (hr["d"] + hr["l"]) / hn
            thr = 0.80 - r
            ok = (ar["w"] + ar["d"]) / an >= 0.7 - r and (odds is None or odds >= 1.12)

    return {"score": score, "margin": score - thr, "strong": ok and score >= thr}


def reason_text(key, f, h, a):
    H, A = f["home_team"], f["away_team"]
    nh, na = h["n"], a["n"]

    if key == "gg":
        return (
            f"{H} scored in {h['sc']}/{nh} and conceded in {h['co']}/{nh} with BTTS in "
            f"{h['bt']}/{nh}; {A} scored in {a['sc']}/{na} and conceded in {a['co']}/{na} "
            f"with BTTS in {a['bt']}/{na}. Both sides score and concede regularly, "
            f"which supports GG."
        )

    if key == "over25":
        return (
            f"{H} had 3+ goals in {h['o25c']}/{nh} of their last matches and {A} in "
            f"{a['o25c']}/{na}. Both teams are involved in high-scoring games, "
            f"which supports Over 2.5."
        )

    if key == "over15":
        return (
            f"{H} had 2+ goals in {h['o15c']}/{nh} of their last matches and {A} in "
            f"{a['o15c']}/{na}. Both teams regularly play games with at least two goals, "
            f"which supports Over 1.5."
        )

    hr, hv = venue_record(h, "H")
    ar, av = venue_record(a, "A")
    hw = "home games" if hv else "games"
    aw = "away games" if av else "games"

    if key == "win_home":
        return (
            f"{H} won {hr['w']}/{hr['n']} of their last {hw} (D{hr['d']} L{hr['l']}), while {A} "
            f"lost {ar['l']}/{ar['n']} of their last {aw}. A strong host against a "
            f"vulnerable visitor, which supports a {H} win."
        )

    if key == "win_away":
        return (
            f"{A} won {ar['w']}/{ar['n']} of their last {aw} (D{ar['d']} L{ar['l']}), while {H} "
            f"lost {hr['l']}/{hr['n']} of their last {hw}. A strong visitor against a "
            f"weak host, which supports an {A} win."
        )

    if key == "dc_1x":
        return (
            f"{H} did not lose {hr['w'] + hr['d']}/{hr['n']} of their last {hw}, and {A} "
            f"failed to win {ar['d'] + ar['l']}/{ar['n']} of their last {aw}. "
            f"That supports {H} or a draw."
        )

    return (
        f"{A} did not lose {ar['w'] + ar['d']}/{ar['n']} of their last {aw}, and {H} "
        f"failed to win {hr['d'] + hr['l']}/{hr['n']} of their last {hw}. "
        f"That supports {A} or a draw."
    )


def evaluate(f, h, a, market, ev=None, loose=False):
    """f = display dict (names, league, kickoff). ev = SportyBet event, if known."""

    best = None

    for k in GROUPS[market]:
        odds = None

        # when we know the SportyBet event, only use markets it actually lists
        if ev is not None:
            p = sportybet.market_pick(ev, k)
            if not p:
                continue
            odds = p["odds"]

        r = score_market(k, h, a, loose, odds)

        if best is None or r["margin"] > best["margin"]:
            best = {**r, "key": k}

    if best and best["strong"]:
        return {"f": f, "h": h, "a": a, "ev": ev, **best}

    return None


# =========================================================
# SPORTYBET: FALLBACK MARKET SEARCH (only if the list has no odds for a market)
# =========================================================

LABEL_KEYS = (
    "market", "market_name", "marketName", "name", "title", "desc",
    "description", "pick", "outcome", "label", "selection", "specifier"
)

BAD = re.compile(
    r"\b(1st half|2nd half|first half|second half|half|corners?|bookings?|cards?|"
    r"players?|exact|handicap|asian)\b|&|\band\b"
)


def walk(obj, ctx=""):
    """Yield (ancestor_text, own_text, selection) for every dict holding 'ref' and 'odds'."""

    if isinstance(obj, dict):
        own = " ".join(
            str(obj[k]) for k in LABEL_KEYS
            if isinstance(obj.get(k), (str, int, float)) and not isinstance(obj.get(k), bool)
        )
        full = (ctx + " " + own).strip()

        if "ref" in obj and "odds" in obj:
            yield ctx, own, obj

        for v in obj.values():
            if isinstance(v, (dict, list)):
                yield from walk(v, full)

    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v, ctx)


def find_selection(markets, key):
    for ctx, own, sel in walk(markets):

        combined = (ctx + " " + own).lower()
        pick = (own or combined).lower()

        if BAD.search(combined):
            continue

        if key == "gg":
            if not re.search(r"both teams to score|gg\s*/\s*ng|\bbtts\b|\bgg\b", combined):
                continue
            p = re.sub(r"gg\s*/\s*ng", " ", pick)
            if re.search(r"\b(yes|gg)\b", p) and not re.search(r"\b(no|ng)\b", p):
                return sel

        else:
            line = "2.5" if key == "over25" else "1.5"
            if line not in combined:
                continue
            if not re.search(r"over\s*/\s*under|goals|total", combined):
                continue
            p = re.sub(r"over\s*/\s*under", " ", pick)
            if re.search(r"\bover\b", p) and not re.search(r"\bunder\b", p):
                return sel

    return None


def lookup_leg(c):
    """Fallback: ask SportyBet's event page for the selection. Returns (candidate, leg_or_None, why)."""

    f = c["f"]
    spec = MARKETS[c["key"]]

    try:
        data = sportybet.get_markets(
            "football", f["home_team"], f["away_team"], spec["search"]
        )
    except Exception as e:
        return c, None, f"not available on SportyBet ({type(e).__name__})"

    sel = find_selection(data, c["key"])

    if not sel:
        print("NO SELECTION FOUND:", c["key"], f["home_team"], "v", f["away_team"], flush=True)
        return c, None, "market not found on SportyBet"

    try:
        odds = float(sel["odds"])
    except Exception:
        return c, None, "odds missing"

    return c, make_leg(c, sel["ref"], odds), ""


def make_leg(c, ref, odds):
    f = c["f"]
    spec = MARKETS[c["key"]]

    return {
        "home": f["home_team"],
        "away": f["away_team"],
        "kickoff": f.get("kickoff_utc", ""),
        "sport": "football",
        "ref": ref,
        "market": spec["market"],
        "pick": spec["pick"],
        "show": spec["show"],
        "odds": odds,
        "reason": reason_text(c["key"], f, c["h"], c["a"]),
        "league": f.get("league_name", ""),
        "kickoff_label": kick_time(f),
        "kickoff_short": kick_short(f),
    }


def warm_sportybet():
    """Load SportyBet's match list once. Returns (status, message):
    ("ok", ""), ("timeout", msg) or ("error", msg)."""

    fn = getattr(sportybet, "_events", None)

    if fn is None:
        return "ok", ""

    ex = ThreadPoolExecutor(max_workers=1)
    fu = ex.submit(fn, "football")
    done, _ = wait([fu], timeout=EVENTS_BUDGET)
    ex.shutdown(wait=False)

    if not done:
        return "timeout", "SportyBet's match list is still loading"

    try:
        fu.result()
        return "ok", ""
    except Exception as e:
        return "error", (str(e) or type(e).__name__)


# =========================================================
# BUILDING A TICKET
# =========================================================

def book_legs(legs, s):
    r = sportybet.book(legs)
    item = {
        "code": r["code"],
        "games": len(legs),
        "odds": round(r.get("total_odds") or prod(legs), 2),
    }
    s["codes"].append(item)
    return item


def format_legs(legs):
    blocks = []

    for i, x in enumerate(legs, 1):
        when_txt = x.get("kickoff_short") or x.get("kickoff_label") or ""
        place = " · ".join(p for p in (x.get("league", ""), when_txt) if p)
        shown = x.get("show") or f"{x.get('market', '')} {x.get('pick', '')}".strip()

        lines = [f"**{i}. {x['home']} vs {x['away']}**"]

        if place:
            lines.append(f"🌍 {place}")

        lines.append(f"🎯 **{shown} @{x['odds']:.2f}**")

        if x.get("reason"):
            lines.append(f"> {x['reason']}")

        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)


def sa_fixtures(d):
    data = sa_get(f"/fixtures/date/{d.isoformat()}", sportapi.H1).get("data", [])
    return [f for f in data if f.get("status") == "Scheduled"]


def match_fixture(ev, fixtures):
    """Find the SportAPI fixture that is the same match as a SportyBet event."""

    best, best_score = None, 0.0

    for f in fixtures:
        ts = ts_of(f.get("kickoff_utc"))
        if ts is None or abs(ts * 1000 - ev["ms"]) > 30 * 60 * 1000:
            continue
        score = (sim(ev["home"], f["home_team"]) + sim(ev["away"], f["away_team"])) / 2
        if score > best_score:
            best, best_score = f, score

    return best if best_score >= 0.70 else None


def sb_window_events(d_from, d_to):
    """SportyBet football events kicking off in the window (Nigeria time), not started yet."""

    start = datetime.combine(d_from, datetime.min.time(), tzinfo=WAT).timestamp() * 1000
    end = datetime.combine(d_to + timedelta(days=1), datetime.min.time(), tzinfo=WAT).timestamp() * 1000
    now_ms = time.time() * 1000

    return [
        e for e in sportybet._events("football")
        if max(start, now_ms) <= e["ms"] < end
    ]


def rank_events(evs, market, limit):
    """Order the events by how promising SportyBet's own odds make them (no SportAPI calls)."""

    def lowest(ev, group):
        odds = [p["odds"] for p in (sportybet.market_pick(ev, k) for k in GROUPS[group]) if p]
        return min(odds) if odds else None

    def proxy(ev, group):
        o = lowest(ev, group)
        if o:
            return o
        o25 = lowest(ev, "over25")           # high-scoring games are good for GG and Over 1.5 too
        return (o25 + 0.3) if o25 and group in ("gg", "over15") else 99

    groups = ["gg", "over25", "win"] if market == "auto" else [market]
    lists = [sorted(evs, key=lambda e, g=group: proxy(e, g)) for group in groups]

    out, seen = [], set()

    for i in range(max(len(x) for x in lists)):
        for lst in lists:
            if i < len(lst) and lst[i]["event_id"] not in seen:
                seen.add(lst[i]["event_id"])
                out.append(lst[i])
        if len(out) >= limit:
            break

    return out[:limit]


def enough(cands, count, target):
    if count:
        return len(cands) >= count + 2
    if not cands:
        return False
    odds = [sportybet.market_pick(c["ev"], c["key"])["odds"] for c in cands]
    return math.prod(odds) >= target


def display_fixture(ev):
    return {
        "home_team": ev["home"],
        "away_team": ev["away"],
        "league_name": ev["league"],
        "kickoff_utc": iso_from_ms(ev["ms"]),
    }


def build_ticket(t, s):
    t0 = time.time()

    d_from, d_to, when = parse_dates(t)
    market = parse_market(t) or "auto"
    count, target = parse_numbers(t)
    loose = bool(re.search(r"\b(risky|risk|loose|any|don'?t mind|dont mind)\b", t))

    if not count and not target:
        count = 5

    status, sb_msg = warm_sportybet()

    if status == "timeout":
        return (
            "SportyBet's match list is still loading (the first load after a restart takes "
            "up to a minute). Ask me again in a minute and it will answer fast."
        )

    if status == "error":
        return stats_only(t, d_from, d_to, when, market, count, 12, sb_msg)

    # ---- 1. SportyBet's own list, no SportAPI calls
    evs = sb_window_events(d_from, d_to)

    pseudo = [{"league_name": e["league"], "_ev": e} for e in evs]
    pseudo, league_label = pick_leagues(t, pseudo)
    evs = [p["_ev"] for p in pseudo]

    if not evs:
        return (
            f"SportyBet has no {'' if league_label == 'all leagues' else league_label + ' '}matches for {when}. "
            f"Try another day or league, for example: "
            f"\"Give me 5 GG matches from tomorrow's Premier League\"."
        )

    total_found = len(evs)
    ranked = rank_events(evs, market, 250)          # best-looking first, by SportyBet's own odds

    # ---- 2. SportAPI fixtures for those days (1 call per day, remembered)
    dates = sorted({datetime.fromtimestamp(e["ms"] / 1000, timezone.utc).date() for e in ranked})

    fixtures = []
    for d in dates:
        try:
            fixtures += sa_fixtures(d)
        except Exception as e:
            if "limit" in str(e).lower():
                return f"SportAPI daily limit reached. It resets at 00:00 UTC. ({e})"
            print("FIXTURES ERROR:", d, e, flush=True)

    # keep only matches the stats provider knows (free: no API calls), still in ranked order
    pairs_all, unmatched = [], 0

    for ev in ranked:
        fx = match_fixture(ev, fixtures)
        if fx:
            pairs_all.append((ev, fx))
        else:
            unmatched += 1

    if not pairs_all:
        return (
            f"None of the {len(ranked)} best-looking {league_label} matches for {when} are covered by "
            f"my stats provider ({len(fixtures)} fixtures loaded), so I cannot check their form. "
            f"Try naming a league, for example \"Premier League\" or \"Europa League\"."
        )

    # ---- 3. check form in rounds, stop as soon as there are enough strong matches
    cands, checked, no_stats, unfinished = [], 0, 0, 0
    pairs_all = pairs_all[:MAX_ANALYSE]

    for i in range(0, len(pairs_all), BATCH):
        remaining = TOTAL_BUDGET - (time.time() - t0)

        if i > 0 and remaining < 15:
            break

        pairs = pairs_all[i:i + BATCH]

        forms, unf = load_forms([fx for _, fx in pairs], max(8, min(FORM_BUDGET, remaining - 8)))
        unfinished += unf

        for ev, fx in pairs:
            h, a = forms.get(fx["home_id"]), forms.get(fx["away_id"])
            if not h or not a:
                no_stats += 1
                continue
            checked += 1
            c = evaluate(display_fixture(ev), h, a, market, ev, loose)
            if c:
                cands.append(c)

        if enough(cands, count, target):
            break

    cands.sort(key=lambda c: -c["margin"])

    if not cands:
        return (
            f"I checked the form of {checked} {league_label} matches for {when} "
            f"({total_found} on SportyBet; {no_stats} covered matches had no usable form data), "
            f"but none passed the stats check. I would rather give you nothing than weak picks. "
            f"Say \"risky\" if you want me to relax the check."
        )

    # ---- 4. build the ticket with the odds from SportyBet's list
    legs, skipped = [], []
    fallback_left = (count or 8) + 3

    for c in cands:
        if count and len(legs) >= count:
            break
        if target and legs and prod(legs) >= target:
            break

        pick = sportybet.market_pick(c["ev"], c["key"])

        if pick:
            legs.append(make_leg(c, pick["ref"], pick["odds"]))
            continue

        if fallback_left <= 0:
            continue

        fallback_left -= 1
        _, leg, why = lookup_leg(c)

        if leg:
            legs.append(leg)
        else:
            f = c["f"]
            skipped.append(f"{f['home_team']} vs {f['away_team']}: {why}")

    if not legs:
        return (
            "Matches passed the stats check, but I could not read their odds on SportyBet.\n"
            + "\n".join(skipped[:5])
        )

    s["ticket"][:] = legs
    s["slips"] = []

    # ---- 5. book it
    title = TITLES.get(market, "Ticket")
    total = prod(legs)

    if count and len(legs) < count:
        head = f"🎟 **{title} — {len(legs)} of {count} {when}, strongest picks only**"
    else:
        head = f"🎟 **{title} — {len(legs)} legs, {total} odds**"

    try:
        item = book_legs(legs, s)
        code_line = f"📋 Code: **{item['code']}** — {item['games']} games, {item['odds']} odds."
    except Exception as e:
        code_line = (
            f"Ticket built, but SportyBet booking failed: {type(e).__name__}: {e}\n"
            f"Total odds {total}"
        )

    notes = []

    if count and len(legs) < count:
        notes.append(
            f"Safe picks only take the strongest matches, so only {len(legs)} cleared, "
            f"not the {count} you asked for. Say \"risky\" if you want me to relax the check."
        )

    if target and total < target * 0.9:
        notes.append(
            f"Total odds are {total}, below your {target:g} target, because not enough "
            f"matches passed the stats check. Say \"risky\" to relax it."
        )

    if unfinished:
        notes.append(
            f"The stats server was slow, so {unfinished} teams were not checked in time. "
            f"Ask again in a minute: what was loaded is remembered, so it will be faster."
        )

    parts = [head]

    if notes:
        parts.append("\n".join(notes))

    parts.append(code_line)
    parts.append("📊 **Picks**\n" + format_legs(legs))

    reply = "\n\n".join(parts)

    if skipped:
        reply += "\n\nSkipped (strong stats, but no usable market on SportyBet):\n" + "\n".join(skipped[:5])

    scope = "" if league_label == "all leagues" else f"{league_label} "

    reply += (
        f"\n\nChecked the form of {checked} {scope}matches "
        f"({total_found} on SportyBet for {when})"
        + (f"; {no_stats} had no form data." if no_stats else ".")
    )

    reply += "\nSay \"receipt\" for a shareable image, or \"convert to Bet9ja\" to move this code."

    return reply


def stats_only(t, d_from, d_to, when, market, count, k, why):
    """SportyBet is unavailable: still show the stats-based picks (no odds, no code)."""

    fixtures = []
    d = d_from
    while d <= d_to:
        try:
            fixtures += sa_fixtures(d)
        except Exception as e:
            print("FIXTURES ERROR:", d, e, flush=True)
        d += timedelta(days=1)

    fixtures, league_label = pick_leagues(t, fixtures)

    if not fixtures:
        return f"I found no scheduled {league_label} matches for {when}."

    fixtures.sort(key=lambda f: f.get("kickoff_utc", ""))
    fixtures = fixtures[:k]

    forms, _ = load_forms(fixtures)

    cands = []

    for f in fixtures:
        h, a = forms.get(f["home_id"]), forms.get(f["away_id"])
        if not h or not a:
            continue
        c = evaluate(f, h, a, market)
        if c:
            cands.append(c)

    cands.sort(key=lambda c: -c["margin"])

    if not cands:
        return (
            f"SportyBet is not answering ({why}), and none of the {len(fixtures)} matches "
            f"I checked passed the stats check."
        )

    shown = cands[: (count or 8)]

    picks = []
    for i, c in enumerate(shown, 1):
        f = c["f"]
        picks.append(
            f"{i}. {f['home_team']} vs {f['away_team']}\n"
            f"   {f.get('league_name', '')} · {kick_time(f)}\n"
            f"   Market: {MARKETS[c['key']]['label']}\n"
            f"   {reason_text(c['key'], f, c['h'], c['a'])}"
        )

    return (
        "SportyBet did not answer, so I cannot make a booking code right now. "
        "These matches passed the stats check:\n\n"
        + "\n\n".join(picks)
        + "\n\nQuick list to add by hand in the SportyBet app:\n"
        + "\n".join(
            f"• {c['f']['home_team']} v {c['f']['away_team']} - "
            f"{MARKETS[c['key']]['market']}: {MARKETS[c['key']]['pick']}"
            for c in shown
        )
        + f"\n\nWhy no code: {why}"
    )


# =========================================================
# CODES: READ, CONVERT, BOOK, EDIT
# =========================================================

def describe(out):
    lines = []
    for k, v in out.items():
        if isinstance(v, (str, int, float)) and not isinstance(v, bool):
            lines.append(f"{k}: {v}")
        elif isinstance(v, list) and v and all(isinstance(i, str) for i in v):
            lines.append(f"{k}:\n  " + "\n  ".join(v[:15]))
    return "\n".join(lines)


def do_read(code, s):
    try:
        out = router.read_for_chat(code, None)
    except Exception as e:
        return f"I could not read code {code}: {type(e).__name__}: {e}"

    legs = out.pop("legs_list", None)

    if legs:
        s["ticket"][:] = legs
        s["slips"] = []

    reply = f"Loaded code {code}."

    if legs:
        reply += f" {len(legs)} games, total odds {prod(legs)}.\n\n"
        reply += "\n".join(
            f"{i}. {x['home']} vs {x['away']}: {x['market']} {x['pick']} @ {x['odds']}"
            for i, x in enumerate(legs, 1)
        )

    extra = describe(out)

    if extra:
        reply += "\n\n" + extra

    reply += "\n\nTell me where to send it, e.g. \"convert to Bet9ja\"."

    return reply


def do_convert(code, target, s):
    try:
        out = router.convert_for_chat(code, None, target, False)
    except Exception as e:
        return f"Conversion failed: {type(e).__name__}: {e}"

    if "ticket" in out:
        s["ticket"][:] = out.pop("ticket")
        s["slips"] = []

    new = out.pop("new_code", None)

    label = bookmakers.BOOKMAKERS.get(target, {}).get("label", target)

    if new:
        s["codes"].append(new if isinstance(new, dict) else {"code": new, "games": len(s["ticket"]), "odds": prod(s["ticket"])})
        shown = new.get("code") if isinstance(new, dict) else new
        reply = f"🎟 {label} CODE: {shown}"
    else:
        reply = f"I could not create a {label} code."

    extra = describe(out)

    if extra:
        reply += "\n\n" + extra

    return reply


def do_book(s):
    legs = s["ticket"]

    if not legs:
        return "Your ticket is empty. Ask me for matches first, e.g. \"Give me 5 GG matches today\"."

    try:
        item = book_legs(legs, s)
    except Exception as e:
        return f"SportyBet booking failed: {type(e).__name__}: {e}"

    return f"📋 Code: **{item['code']}** — {item['games']} games, {item['odds']} odds."


def do_remove(t, s):
    legs = s["ticket"]

    if not legs:
        return "Your ticket is empty."

    gone = 0

    m = re.search(r"(?:below|under|less than|lower than)\s*(\d+(?:\.\d+)?)", t)

    if m:
        lim = float(m.group(1))
        keep = [x for x in legs if x["odds"] >= lim]
        gone = len(legs) - len(keep)
        legs[:] = keep
    else:
        idx = {int(n) - 1 for n in re.findall(r"\b\d{1,2}\b", t)}
        keep = [x for i, x in enumerate(legs) if i not in idx]
        gone = len(legs) - len(keep)
        legs[:] = keep

    if not gone:
        return "I did not remove anything. Say e.g. \"remove leg 3\" or \"remove odds below 1.2\"."

    if not legs:
        return "Removed. Your ticket is now empty."

    return (
        f"Removed {gone} leg(s). {len(legs)} games left, total odds {prod(legs)}.\n\n"
        + format_legs([{**x, "league": x.get("league", ""), "kickoff_label": x.get("kickoff_label", "")} for x in legs])
        + "\n\nSay \"book this ticket\" for a new code."
    )


def help_text(lead=""):
    names = ", ".join(v.get("label", k) for k, v in bookmakers.BOOKMAKERS.items())
    return (
        (lead + "\n\n" if lead else "")
        + "I build tickets from real form stats and give you the booking code. Try:\n\n"
        "• Give me 10 matches that can play GG from today's European competitions\n"
        "• Straight win 10 matches today\n"
        "• Double chance 5 matches tomorrow\n"
        "• Over 2.5 picks for the weekend\n"
        "• Give me your best 10 odds (mixes every market I know)\n"
        "• Add \"risky\" to relax the stats check\n"
        "• receipt (makes the shareable image of your ticket)\n"
        "• Paste a booking code to read it, or: convert this to Bet9ja\n"
        "• remove leg 3 / remove odds below 1.2 / book this ticket\n\n"
        "Markets I can build from stats right now: GG, Over 1.5, Over 2.5, straight win, double chance.\n"
        f"Bookmakers: {names}"
    )


# =========================================================
# ROUTING: WHAT DOES THE USER WANT?
# =========================================================

def handle(msg, s):
    text = (msg or "").strip()
    t = text.lower()

    if not t or re.match(r"^(hi+|hello|hey|yo|help|start|menu)\b", t) or "what can you do" in t:
        return help_text("Hi! I'm BetOracle.")

    code = detect_code(text)
    bk = detect_bookmaker(t)
    conv = re.search(r"\b(convert|change|move|switch|transfer|send|put)\b", t)
    readw = re.search(r"\b(read|check|load|open|show|see|view|code|booking)\b", t)

    # 1. a booking code was pasted
    if code and (conv or bk or readw or norm(text) == norm(code)):
        return do_convert(code, bk, s) if bk else do_read(code, s)

    # 2. "convert this to Bet9ja" with no code: use the last code
    if bk and conv:
        if s["codes"]:
            return do_convert(s["codes"][-1]["code"], bk, s)
        return "Paste the booking code you want to convert, or ask me for a ticket first."

    # 3. clear
    if re.search(r"\b(clear|reset|empty|start over)\b", t):
        s["ticket"].clear()
        s["slips"] = []
        return "Ticket cleared."

    # 4. remove legs
    if re.search(r"\b(remove|delete|drop)\b", t) and s["ticket"]:
        return do_remove(t, s)

    market = parse_market(t)
    count, target = parse_numbers(t)

    # 5. book the current ticket
    if (
        re.search(r"\bbook\b", t)
        and re.search(r"\b(this|it|that|ticket|slip|now)\b", t)
        and not market
        and not re.search(r"\d", t)
    ):
        return do_book(s)

    # 6. build a new ticket
    if market or count or target or mentions_league(t) or re.search(
        r"\b(match|matches|game|games|pick|picks|odds?|ticket|slip|tips?|predict\w*|banker|safe|book|today|tomorrow|weekend|tonight)\b", t
    ):
        return build_ticket(t, s)

    return help_text("I did not catch that.")


# =========================================================
# FASTAPI APPLICATION
# =========================================================

class Chat(BaseModel):
    session: str
    message: str


app = FastAPI()


@app.on_event("startup")
def preload_sportybet():
    """Load SportyBet's match list in the background so the first request is fast."""

    def go():
        try:
            sportybet._events("football")
            print("SportyBet match list preloaded", flush=True)
        except Exception as e:
            print("Preload failed:", e, flush=True)

    threading.Thread(target=go, daemon=True).start()


@app.post("/api/chat")
def chat(c: Chat):

    s = SESSIONS.setdefault(
        c.session,
        {"ticket": [], "slips": [], "codes": []}
    )

    try:
        reply = handle(c.message, s)

    except Exception as e:
        traceback.print_exc()
        reply = f"Something went wrong: {type(e).__name__}: {e}"

    return {
        "reply": reply,
        "ticket": s["ticket"],
        "codes": s["codes"]
    }


@app.get("/api/debug-markets")
def debug_markets(home: str, away: str, search: str = ""):
    """Shows the raw SportyBet market data for a match. Use it to fix market matching."""

    t0 = time.time()

    try:
        data = sportybet.get_markets("football", home, away, search or None)
        return {"seconds": round(time.time() - t0, 1), "type": type(data).__name__, "data": data}
    except Exception as e:
        return {"seconds": round(time.time() - t0, 1), "error": f"{type(e).__name__}: {e}"}


def calls_today():
    """How many real SportAPI calls were made today (read only, does not add to the count)."""

    try:
        day = time.strftime("calls:%Y-%m-%d", time.gmtime())

        if getattr(cache, "URL", None):
            return int(cache._redis("GET", day) or 0)

        row = cache._db.execute("select v from c where k=?", (day,)).fetchone()
        return int(row[0]) if row else 0

    except Exception:
        return -1


@app.get("/api/usage")
def usage():
    return {
        "sportapi_calls_today": calls_today(),
        "cache": "Upstash Redis (survives restarts)" if getattr(cache, "URL", None)
                 else "local file (wiped on every Render restart or deploy)",
    }


@app.get("/api/bookmakers")
def bookmaker_status():
    return {
        k: {"label": v["label"], **bookmakers.caps(k)}
        for k, v in bookmakers.BOOKMAKERS.items()
    }


@app.get("/health")
def health():
    return {"ok": True, "mode": "rule-based (no AI)"}


app.mount(
    "/",
    StaticFiles(
        directory=os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "static"
        ),
        html=True
    )
)


# Run locally:
# uvicorn app:app --reload
