"""
BetOracle: no AI, no Groq.
The server reads what the user types with simple rules, checks real form
stats from sportapi, picks the strongest matches, books them on SportyBet
and returns the booking code. It can also read and convert booking codes.
"""

import os
import re
import math
import traceback
import unicodedata

from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, wait

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import sportybet
import sportapi
import bookmakers
import convert_router as router

from cache import count_call


# =========================================================
# CONFIGURATION
# =========================================================

load_dotenv()

MAX_ANALYSE = int(os.getenv("MAX_ANALYSE", "24"))   # max matches to analyse per request
FORM_BUDGET = float(os.getenv("FORM_BUDGET", "40"))     # seconds allowed for loading team stats
MARKET_BUDGET = float(os.getenv("MARKET_BUDGET", "25")) # seconds allowed for SportyBet odds lookups
FORM_N = 10                                         # last N finished matches per team
MIN_FORM = 6                                        # need at least this many matches of data

WAT = timezone(timedelta(hours=1))                  # Nigeria time

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


def today():
    return datetime.now(WAT).date()


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
    return None


def parse_numbers(t):
    """Returns (count, target_odds). Either can be None."""

    target = None
    count = None

    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:total\s*)?odds?\b", t)
    if m:
        target = float(m.group(1))

    m = re.search(
        r"\b(\d{1,2})\s*(?:best\s*|safe\s*|strong\s*)?"
        r"(?:matches|match|games|game|picks|pick|legs|leg|teams|selections|fixtures)\b",
        t
    )
    if m:
        count = int(m.group(1))

    return count, target


EXCLUDE = (
    "caf", "afc", "concacaf", "conmebol", "women",
    "u17", "u19", "u20", "u21", "u23", "youth"
)

LEAGUE_GROUPS = [
    (("europa league",), ("europa league",), "Europa League"),
    (("conference league",), ("conference league", "europa conference"), "Conference League"),
    (("champions league",), ("champions league",), "Champions League"),
    (("european", "europe", "uefa", "euro comp"),
     ("champions league", "europa league", "conference league", "europa conference"),
     "European competitions"),
]

TOP_LEAGUES = {
    "premier league": ("premier league", "english premier league"),
    "la liga": ("la liga", "laliga"),
    "serie a": ("serie a",),
    "bundesliga": ("bundesliga",),
    "ligue 1": ("ligue 1",),
    "eredivisie": ("eredivisie",),
    "championship": ("championship", "efl championship"),
}


def mentions_league(t):
    for triggers, _, _ in LEAGUE_GROUPS:
        if any(k in t for k in triggers):
            return True
    return any(k in t for k in TOP_LEAGUES)


def pick_leagues(t, fixtures):
    """Filter fixtures by the league the user mentioned. Returns (fixtures, label)."""

    for triggers, words, label in LEAGUE_GROUPS:
        if any(k in t for k in triggers):
            out = []
            for f in fixtures:
                n = f.get("league_name", "").lower()
                if any(w in n for w in words) and not any(x in n for x in EXCLUDE):
                    out.append(f)
            return out, label

    for key, variants in TOP_LEAGUES.items():
        if key in t:
            return [f for f in fixtures if f.get("league_name", "").lower() in variants], key.title()

    names = {f.get("league_name", "") for f in fixtures}
    hit = [n for n in names if len(n) >= 5 and n.lower() in t]
    if hit:
        return [f for f in fixtures if f.get("league_name") in hit], ", ".join(hit)

    return fixtures, "all leagues"


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
        rows = sportapi.get(f"/standings/{f['league_id']}", sportapi.H6).get("data", [])
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
            return sportapi.get(f"/teams/slug/{s}", sportapi.H6).get("results", [])
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
        rows.append((gf, ga))

    n = len(rows)

    if n < MIN_FORM:
        return None

    sc = sum(1 for gf, ga in rows if gf > 0)
    co = sum(1 for gf, ga in rows if ga > 0)
    bt = sum(1 for gf, ga in rows if gf > 0 and ga > 0)
    o15 = sum(1 for gf, ga in rows if gf + ga >= 2)
    o25 = sum(1 for gf, ga in rows if gf + ga >= 3)

    return {
        "n": n, "sc": sc, "co": co, "bt": bt, "o15c": o15, "o25c": o25,
        "scored": sc / n, "conceded": co / n, "btts": bt / n,
        "o15": o15 / n, "o25": o25 / n,
    }


def load_forms(fixtures):
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
            print("FORM ERROR:", f.get(f"{side}_team"), type(e).__name__, e)
            return tid, None

    ex = ThreadPoolExecutor(max_workers=8)
    futs = [ex.submit(work, item) for item in tasks.items()]

    done, pending = wait(futs, timeout=FORM_BUDGET)

    # let slow requests finish in the background (they fill the cache for next time)
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
        "label": "GG (both teams to score)",
        "market": "GG/NG", "pick": "Yes",
        "search": "GG/NG,Both Teams To Score,BTTS",
    },
    "over25": {
        "label": "Over 2.5 goals",
        "market": "Over/Under 2.5", "pick": "Over 2.5",
        "search": "Over/Under,Total",
    },
    "over15": {
        "label": "Over 1.5 goals",
        "market": "Over/Under 1.5", "pick": "Over 1.5",
        "search": "Over/Under,Total",
    },
}


def score_market(key, h, a):
    if key == "gg":
        est = ((h["scored"] + a["conceded"]) / 2) * ((a["scored"] + h["conceded"]) / 2)
        score = 0.5 * est + 0.5 * ((h["btts"] + a["btts"]) / 2)
        thr = 0.55
        ok = h["btts"] >= 0.5 and a["btts"] >= 0.5
    elif key == "over25":
        score = (h["o25"] + a["o25"]) / 2
        thr = 0.60
        ok = min(h["o25"], a["o25"]) >= 0.5
    else:
        score = (h["o15"] + a["o15"]) / 2
        thr = 0.80
        ok = min(h["o15"], a["o15"]) >= 0.7

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

    return (
        f"{H} had 2+ goals in {h['o15c']}/{nh} of their last matches and {A} in "
        f"{a['o15c']}/{na}. Both teams regularly play games with at least two goals, "
        f"which supports Over 1.5."
    )


def evaluate(f, h, a, market):
    keys = ["gg", "over25", "over15"] if market == "auto" else [market]

    best = None

    for k in keys:
        r = score_market(k, h, a)
        if best is None or r["margin"] > best["margin"]:
            best = {**r, "key": k}

    if best and best["strong"]:
        return {"f": f, "h": h, "a": a, **best}

    return None


# =========================================================
# SPORTYBET: FINDING THE RIGHT SELECTION
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


def warm_sportybet():
    """Load SportyBet's event list once. Returns '' if OK, otherwise the reason it failed."""

    fn = getattr(sportybet, "_events", None)

    if fn is None:
        return ""

    ex = ThreadPoolExecutor(max_workers=1)
    fu = ex.submit(fn, "football")
    done, _ = wait([fu], timeout=MARKET_BUDGET)
    ex.shutdown(wait=False)

    if not done:
        return "SportyBet did not answer in time"

    try:
        fu.result()
        return ""
    except Exception as e:
        return str(e) or type(e).__name__


def lookup_leg(c):
    """Find the SportyBet selection for one candidate. Returns (candidate, leg_or_None, why)."""

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
        print("NO SELECTION FOUND:", c["key"], f["home_team"], "v", f["away_team"])
        return c, None, "market not found on SportyBet"

    try:
        odds = float(sel["odds"])
    except Exception:
        return c, None, "odds missing"

    leg = {
        "home": f["home_team"],
        "away": f["away_team"],
        "kickoff": f.get("kickoff_utc", ""),
        "sport": "football",
        "ref": sel["ref"],
        "market": spec["market"],
        "pick": spec["pick"],
        "odds": odds,
        "reason": reason_text(c["key"], f, c["h"], c["a"]),
        "league": f.get("league_name", ""),
    }

    return c, leg, ""


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
    lines = []
    for i, x in enumerate(legs, 1):
        lines.append(
            f"{i}. {x['home']} vs {x['away']}\n"
            f"   {x.get('league', '')} · {x.get('kickoff_label', '')}\n"
            f"   {x['market']}: {x['pick']} @ {x['odds']:.2f}\n"
            f"   {x['reason']}"
        )
    return "\n\n".join(lines)


def build_ticket(t, s):
    d_from, d_to, when = parse_dates(t)
    market = parse_market(t) or "auto"
    count, target = parse_numbers(t)

    if not count and not target:
        count = 5

    fixtures = []
    d = d_from
    while d <= d_to:
        data = sportapi.get(f"/fixtures/date/{d.isoformat()}", sportapi.H1).get("data", [])
        fixtures += [f for f in data if f.get("status") == "Scheduled"]
        d += timedelta(days=1)

    seen, unique = set(), []
    for f in fixtures:
        k = (f.get("home_id"), f.get("away_id"), f.get("kickoff_utc"))
        if k not in seen:
            seen.add(k)
            unique.append(f)
    fixtures = unique

    fixtures, league_label = pick_leagues(t, fixtures)

    if not fixtures:
        return (
            f"I found no scheduled {league_label} matches for {when}. "
            f"Try another day or league, for example: "
            f"\"Give me 5 GG matches from tomorrow's Premier League\"."
        )

    fixtures.sort(key=lambda f: f.get("kickoff_utc", ""))
    total_found = len(fixtures)
    fixtures = fixtures[:MAX_ANALYSE]

    forms, unfinished = load_forms(fixtures)

    cands, no_data = [], 0

    for f in fixtures:
        h, a = forms.get(f["home_id"]), forms.get(f["away_id"])
        if not h or not a:
            no_data += 1
            continue
        c = evaluate(f, h, a, market)
        if c:
            cands.append(c)

    cands.sort(key=lambda c: -c["margin"])

    if not cands:
        return (
            f"I checked {len(fixtures)} {league_label} matches for {when}, but none passed "
            f"the stats check ({no_data} had no usable form data). "
            f"I would rather give you nothing than weak picks."
        )

    # look up real SportyBet odds for the best candidates (in parallel)
    top = cands[: (count + 4) if count else 15]

    sb_error = warm_sportybet()

    looked = []

    if sb_error:
        looked = [(c, None, sb_error) for c in top]
    else:
        ex = ThreadPoolExecutor(max_workers=3)
        futs = [ex.submit(lookup_leg, c) for c in top]
        done, pending = wait(futs, timeout=MARKET_BUDGET)
        ex.shutdown(wait=False, cancel_futures=True)

        for c, fu in zip(top, futs):
            if fu in done:
                try:
                    looked.append(fu.result())
                except Exception as e:
                    looked.append((c, None, f"error ({type(e).__name__})"))
            else:
                looked.append((c, None, "SportyBet was too slow"))

    legs, skipped = [], []

    for c, leg, why in looked:
        f = c["f"]
        if not leg:
            skipped.append(f"{f['home_team']} vs {f['away_team']}: {why}")
            continue

        leg["kickoff_label"] = kick_time(f)

        if count and len(legs) >= count:
            break
        if target and legs and prod(legs) >= target:
            break

        legs.append(leg)

    if not legs:
        picks = []
        for i, c in enumerate(cands[: (count or 8)], 1):
            f = c["f"]
            picks.append(
                f"{i}. {f['home_team']} vs {f['away_team']}\n"
                f"   {f.get('league_name', '')} · {kick_time(f)}\n"
                f"   Market: {MARKETS[c['key']]['label']}\n"
                f"   {reason_text(c['key'], f, c['h'], c['a'])}"
            )

        why = sb_error or (skipped[0].split(": ", 1)[-1] if skipped else "SportyBet gave no data")

        return (
            "The stats check worked, but SportyBet did not return any odds, so I cannot "
            "make a booking code right now. These are the matches that passed:\n\n"
            + "\n\n".join(picks)
            + "\n\nWhy no code: " + why
            + (
                "\n\nSportyBet is blocking this server's connection. Run BetOracle on your own "
                "computer (home internet), or set a SPORTYBET_PROXY on Render."
                if any(w in why.lower() for w in ("blocking", "bot protection", "202"))
                else ""
            )
        )

    s["ticket"][:] = legs
    s["slips"] = []

    # book it
    try:
        item = book_legs(legs, s)
        code_line = f"🎟 BOOKING CODE: {item['code']}\n{item['games']} games · total odds {item['odds']}"
    except Exception as e:
        code_line = (
            f"Ticket built, but SportyBet booking failed: {type(e).__name__}: {e}\n"
            f"Total odds {prod(legs)}"
        )

    notes = []

    if count and len(legs) < count:
        notes.append(
            f"Only {len(legs)} matches passed the stats check, not the {count} you asked for. "
            f"I left the weak ones out on purpose."
        )

    if target and prod(legs) < target * 0.9:
        notes.append(
            f"Total odds are {prod(legs)}, below your {target} target, because not enough "
            f"matches passed the stats check."
        )

    if unfinished:
        notes.append(
            f"The stats server was slow, so {unfinished} teams were not checked in time. "
            f"Ask again in a minute: what was already loaded is cached, so it will be faster."
        )

    notes.append(
        f"Checked {len(fixtures)} of {total_found} {league_label} matches for {when}"
        + (f" ({no_data} had no form data)." if no_data else ".")
    )

    reply = code_line + "\n\n" + "\n".join(notes) + "\n\n" + format_legs(legs)

    if skipped:
        reply += "\n\nSkipped (strong stats, but no usable market on SportyBet):\n" + "\n".join(skipped[:5])

    reply += "\n\nSay \"convert to Bet9ja\" (or MSport, BetNaija...) to move this code."

    return reply


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

    return f"🎟 BOOKING CODE: {item['code']}\n{item['games']} games · total odds {item['odds']}"


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
        + format_legs([{**x, "league": x.get("league", ""), "kickoff_label": ""} for x in legs])
        + "\n\nSay \"book this ticket\" for a new code."
    )


def help_text(lead=""):
    names = ", ".join(v.get("label", k) for k, v in bookmakers.BOOKMAKERS.items())
    return (
        (lead + "\n\n" if lead else "")
        + "I build tickets from real form stats and give you the booking code. Try:\n\n"
        "• Give me 10 matches that can play GG from today's European competitions\n"
        "• Give me 25 odds from tomorrow's Premier League\n"
        "• Over 2.5 picks for the weekend\n"
        "• Paste a booking code to read it\n"
        "• convert MX96ZD to Bet9ja\n"
        "• convert this to MSport (uses your last code)\n"
        "• remove leg 3 / remove odds below 1.2 / book this ticket\n\n"
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

    import time as _t
    t0 = _t.time()

    try:
        data = sportybet.get_markets("football", home, away, search or None)
        return {"seconds": round(_t.time() - t0, 1), "type": type(data).__name__, "data": data}
    except Exception as e:
        return {"seconds": round(_t.time() - t0, 1), "error": f"{type(e).__name__}: {e}"}


@app.get("/api/usage")
def usage():
    return {"sportapi_calls_today": count_call() - 1}


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
