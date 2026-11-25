import os, json, math, httpx, anthropic
from datetime import date
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import sportybet, sportapi, corners, bookmakers, msport, convert_router as router
from cache import cached, count_call

load_dotenv()
ai = anthropic.Anthropic()
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
PLATFORMS = list(bookmakers.BOOKMAKERS)
SESSIONS = {}  # session_id -> {"history": [], "ticket": [], "slips": [], "codes": []}

SYSTEM = f"""You are BetOracle, a sharp, friendly sports-betting assistant for Nigerian bettors. Today is {date.today():%A %Y-%m-%d}.
Talk like a normal person, short and direct. Follow the user's instructions exactly (e.g. "book me weekend matches" means: get_fixtures for Sat-Sun, get_markets and get_stats, add_to_ticket, then book_ticket).
Rules: never invent odds, fixtures or stats; use tools. For every football or basketball selection, call get_stats first. Its reason MUST give the real numbers: each team's league position and record, each team's last 5 results, the home team's last 5 at home and the away team's last 5 away, and the head-to-head, then explain how that supports this exact market. If a sport or market has no stats source (for example darts), say plainly that the pick rests on the odds only. Any market on SportyBet is allowed: corners, fouls, shots, tackles, cards, player props, basketball, darts, tennis and more.
For a target like 'give me 25 odds mixing football, basketball and darts', gather real odds with get_markets for each sport, pick the best-supported selections, and stop when the combined odds reach the target. If odds are unavailable for a sport, say so; never fill the gap with guessed odds. Booking creates a booking code only; you never place real-money bets. No pick is guaranteed, so say so briefly when the stake is large. You can edit tickets on request: remove legs (by number or odds range), replace them, convert every leg to another market (for each leg use get_markets, pick the equivalent market and say which legs needed a substitute), split into N slips with split_ticket, and book each with book_ticket. When the user gives a target total odds, keep the total within a few percent of it after every edit and state the final total. 'Value picks' means avoid legs under 1.15 odds. Start replies with the booking code when there is one. When asked why a leg was chosen, answer from the numbers you gathered. For corner picks call get_corner_history for both teams; for shots, fouls, tackles or cards call get_team_stat_history; cite the averages and how many of the last matches cleared the line. Use get_markets with the search keyword for the market the user wants (corner, shots, fouls, 180, total goals...) and copy each chosen selection's ref exactly. A booking code is a SportyBet slip that stakes nothing. If the user pastes a booking code, call read_code (leave platform empty unless they said which bookmaker; it is auto-detected) and show every selection. Users never pick a bookmaker from a menu: understand the destination from ordinary text such as 'convert to Bet9ja', 'change this to Sporty' or 'I want this on BetNaija', then call convert_code with that target. To move it to another bookmaker call convert_code. Never remove, replace or edit a selection silently: if any leg is suspended, not listed, or missing from the created code, say exactly which and ask before continuing. Only say a code was created if a tool returned it. If a tool fails, tell the user plainly what is missing."""

TOOLS = [
 {"name": "get_fixtures", "description": "List fixtures for a sport between two dates (YYYY-MM-DD).",
  "input_schema": {"type": "object", "properties": {"sport": {"type": "string"}, "date_from": {"type": "string"}, "date_to": {"type": "string"}, "league": {"type": "string"}}, "required": ["sport", "date_from", "date_to"]}},
 {"name": "get_markets", "description": "All SportyBet markets and odds for one match.",
  "input_schema": {"type": "object", "properties": {"sport": {"type": "string"}, "home": {"type": "string"}, "away": {"type": "string"}, "search": {"type": "string", "description": "Comma-separated market keywords, e.g. 'corner', 'shots', '180', 'total goals'. Omit for the main markets."}}, "required": ["sport", "home", "away"]}},
 {"name": "read_code", "description": "Load an existing booking code into the ticket so it can be edited, converted or split.", "input_schema": {"type": "object", "properties": {"code": {"type": "string"}, "platform": {"type": "string", "description": "Only if the user said which bookmaker the code is from; otherwise leave empty."}}, "required": ["code"]}},
 {"name": "get_stats", "description": "Evidence for football or basketball: table position, form and head-to-head. Use ids from get_fixtures. Not available for darts or other sports.",
  "input_schema": {"type": "object", "properties": {"sport": {"type": "string"}, "home_id": {"type": "integer"}, "away_id": {"type": "integer"}, "league_id": {"type": ["integer", "string"]}}, "required": ["sport", "home_id", "away_id", "league_id"]}},
 {"name": "add_to_ticket", "description": "Add picks to the user's ticket. Each needs a reason.",
  "input_schema": {"type": "object", "properties": {"selections": {"type": "array", "items": {"type": "object", "properties": {"home": {"type": "string"}, "away": {"type": "string"}, "kickoff": {"type": "string"}, "sport": {"type": "string"}, "ref": {"type": "string", "description": "Exactly the ref returned by get_markets"}, "market": {"type": "string"}, "pick": {"type": "string"}, "odds": {"type": "number"}, "reason": {"type": "string"}}, "required": ["home", "away", "market", "pick", "odds", "reason", "ref"]}}}, "required": ["selections"]}},
 {"name": "remove_legs", "description": "Remove legs from the ticket by 1-based number and/or by an odds range (e.g. all legs between 1.03 and 1.13).",
  "input_schema": {"type": "object", "properties": {"indices": {"type": "array", "items": {"type": "integer"}}, "odds_between": {"type": "array", "items": {"type": "number"}, "description": "[low, high]"}}}},
 {"name": "split_ticket", "description": "Split the ticket into N slips with similar game counts and similar total odds. Book them afterwards with book_ticket.",
  "input_schema": {"type": "object", "properties": {"parts": {"type": "integer"}}, "required": ["parts"]}},
 {"name": "get_team_stat_history", "description": "Football: a team's last matches with shots, shots on target, fouls, tackles and yellow cards (for and against) plus averages. No corners.",
  "input_schema": {"type": "object", "properties": {"team_id": {"type": "integer"}, "league_id": {"type": "integer"}, "n": {"type": "integer"}}, "required": ["team_id", "league_id"]}},
 {"name": "get_corner_history", "description": "Football: a team's corners for, against and total over its last matches, with averages. Use for every corner pick.",
  "input_schema": {"type": "object", "properties": {"team": {"type": "string"}, "n": {"type": "integer"}}, "required": ["team"]}},
 {"name": "convert_code", "description": "Convert a booking code from one Nigerian bookmaker to another. Only connected bookmakers work.",
  "input_schema": {"type": "object", "properties": {"source": {"type": "string", "description": "Only if the user said which bookmaker the code is from."}, "code": {"type": "string"}, "target": {"type": "string", "description": "The bookmaker the user wants, in their own words."}, "allow_partial": {"type": "boolean", "description": "Only true after the user agreed to skip the listed unconvertible selections."}}, "required": ["code", "target"]}},
 {"name": "clear_ticket", "description": "Empty the ticket.", "input_schema": {"type": "object", "properties": {}}},
 {"name": "book_ticket", "description": "Book the ticket on SportyBet (or each split slip) and return the booking codes.", "input_schema": {"type": "object", "properties": {}}},
 {"name": "convert_ticket", "description": "Convert the ticket for another platform: " + ", ".join(PLATFORMS),
  "input_schema": {"type": "object", "properties": {"platform": {"type": "string", "enum": PLATFORMS}}, "required": ["platform"]}},
]

def names(legs): return [f'{x["home"]} v {x["away"]}: {x["market"]} {x["pick"]} @ {x["odds"]}' for x in legs]

def prod(legs): return round(math.prod(x["odds"] for x in legs), 2)

def run_tool(name, a, s):
    t = s["ticket"]
    try:
        if name == "get_fixtures":
            if a["sport"].lower() in ("football", "soccer", "basketball"):
                return sportapi.fixtures("basketball" if a["sport"].lower() == "basketball" else "football", a["date_from"], a["date_to"], a.get("league"))
            return sportybet.list_events(a["sport"], a["date_from"], a["date_to"], a.get("league"))
        if name == "get_markets":
            return sportybet.get_markets(a["sport"], a["home"], a["away"], a.get("search"))
        if name == "read_code":
            out = router.read_for_chat(a["code"], a.get("platform"))
            if "legs_list" in out: t[:] = out.pop("legs_list"); s["slips"] = []
            return out
        if name == "get_stats": return sportapi.stats(a["sport"], a["home_id"], a["away_id"], a["league_id"])
        if name == "add_to_ticket":
            for x in a["selections"]:
                if not x.get("ref"): return {"error": f"Rejected {x['home']} v {x['away']}: missing ref. Call get_markets and copy the ref of the exact selection."}
                r = x.get("reason", "")
                if x.get("sport", "football") in ("football", "basketball") and (len(r) < 150 or sum(c.isdigit() for c in r) < 8):
                    return {"error": f"Rejected {x['home']} v {x['away']}: reason must cite table positions and records, both teams' last 5, home/away form and H2H with real numbers. Call get_stats and rewrite."}
            t.extend(a["selections"]); return {"ticket_size": len(t)}
        if name == "clear_ticket": t.clear(); return "cleared"
        if name == "get_team_stat_history": return sportapi.team_history(a["team_id"], a["league_id"], a.get("n", 8))
        if name == "get_corner_history": return corners.team_corners(a["team"], a.get("n", 8))
        if name == "convert_code":
            out = router.convert_for_chat(a["code"], a.get("source"), a["target"], a.get("allow_partial", False))
            if "ticket" in out: t[:] = out.pop("ticket"); s["slips"] = []
            if "new_code" in out: s["codes"].append(out.pop("new_code"))
            return out
        if name == "remove_legs":
            idx, rng = {i - 1 for i in a.get("indices", [])}, a.get("odds_between")
            gone = [x for i, x in enumerate(t) if i in idx or (rng and rng[0] <= x["odds"] <= rng[1])]
            t[:] = [x for x in t if x not in gone]; s["slips"] = []
            return {"removed": len(gone), "ticket_size": len(t), "total_odds": prod(t)}
        if name == "split_ticket":
            n = max(2, int(a["parts"])); cap = math.ceil(len(t) / n)
            groups, logs = [[] for _ in range(n)], [0.0] * n
            for x in sorted(t, key=lambda x: -x["odds"]):
                i = min((j for j in range(n) if len(groups[j]) < cap), key=lambda j: logs[j])
                groups[i].append(x); logs[i] += math.log(x["odds"])
            s["slips"] = groups
            return [{"slip": i + 1, "games": len(g), "odds": prod(g)} for i, g in enumerate(groups)]
        if name == "book_ticket":
            out = []
            for g in (s["slips"] or [t]):
                r = sportybet.book(g)
                out.append({"code": r["code"], "games": len(g), "odds": round(r.get("total_odds") or prod(g), 2)})
            s["codes"].extend(out); return out
        if name == "convert_ticket" and a["platform"] == "msport":
            r = msport.create_msport_code(t); s["codes"].append({"code": r["code"], "games": r["sent"], "odds": prod(t)}); return r
        if name == "convert_ticket":
            # Add a real adapter per platform in converters/<name>.py; until then you get a clean manual slip.
            lines = [f"{x['home']} v {x['away']} | {x['market']}: {x['pick']} @ {x['odds']}" for x in t]
            return {"platform": a["platform"], "status": "manual_slip", "slip": lines,
                    "note": "No live adapter for this platform yet. Market names may differ; match each line by hand."}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}

class Chat(BaseModel):
    session: str
    message: str

app = FastAPI()

@app.post("/api/chat")
def chat(c: Chat):
    s = SESSIONS.setdefault(c.session, {"history": [], "ticket": [], "slips": [], "codes": []})
    msgs = s["history"]; msgs.append({"role": "user", "content": c.message})
    for _ in range(12):
        r = ai.messages.create(model=MODEL, max_tokens=2000, system=SYSTEM, tools=TOOLS, messages=msgs)
        msgs.append({"role": "assistant", "content": r.content})
        if r.stop_reason != "tool_use": break
        res = [{"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(run_tool(b.name, b.input, s), default=str)[:12000]}
               for b in r.content if b.type == "tool_use"]
        msgs.append({"role": "user", "content": res})
    reply = "".join(b.text for b in r.content if b.type == "text")
    return {"reply": reply, "ticket": s["ticket"], "codes": s["codes"]}

@app.get("/api/usage")
def usage():
    return {"sportapi_calls_today": count_call() - 1}

@app.get("/api/bookmakers")
def bookmaker_status():
    return {k: {"label": v["label"], **bookmakers.caps(k)} for k, v in bookmakers.BOOKMAKERS.items()}

@app.get("/health")
def health():
    return {"ok": True}

app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"), html=True))
# run: uvicorn app:app --reload   then open http://localhost:8000
