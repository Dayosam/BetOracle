import os
import json
import math
import time
import traceback
import httpx

from datetime import date
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import sportybet
import sportapi
import corners
import bookmakers
import msport
import convert_router as router

from cache import cached, count_call


# =========================================================
# CONFIGURATION
# =========================================================

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

MAX_STEPS = int(os.getenv("AI_MAX_STEPS", "12"))
MAX_HISTORY = int(os.getenv("AI_HISTORY_LIMIT", "20"))
TOOL_RESULT_LIMIT = int(os.getenv("AI_TOOL_RESULT_LIMIT", "3500"))

PLATFORMS = list(bookmakers.BOOKMAKERS)

SESSIONS = {}


# =========================================================
# SYSTEM INSTRUCTIONS
# =========================================================

SYSTEM = f"""You are BetOracle, a sharp, friendly sports-betting assistant for Nigerian bettors.

Today is {date.today():%A %Y-%m-%d}.

Talk like a normal person, short and direct.

Follow the user's instructions exactly.

For example, "book me weekend matches" means:
get_fixtures for Saturday and Sunday, get_markets and get_stats, add_to_ticket, then book_ticket.

If the user does not name a match, do not keep asking questions.
Look at upcoming fixtures with get_fixtures, choose the best-supported matches yourself,
and go ahead. If the user names a league (for example "Europa League"), use it as the league filter.

Be efficient with tools: for a small request such as "2 best odds", look at no more than
4 candidate matches, then pick 2. Do not call tools you do not need.

RULES:

Never invent odds, fixtures or statistics. Use tools.

For every football or basketball selection, call get_stats first.

Its reason MUST give the real numbers:
- Each team's league position and record.
- Each team's last 5 results.
- The home team's last 5 at home.
- The away team's last 5 away.
- Head-to-head.
- Explain how the evidence supports the exact market.

If a sport or market has no statistics source, say plainly that the pick rests on odds only.

Any market on SportyBet is allowed:
corners, fouls, shots, tackles, cards, player props, basketball, darts, tennis and more.

For a target like "give me 25 odds mixing football, basketball and darts":
gather real odds with get_markets for each sport, pick the best-supported selections, and stop when combined odds reach the target.

If odds are unavailable for a sport, say so. Never fill the gap with guessed odds.

Booking creates a booking code only. You never place real-money bets.

No pick is guaranteed.

You can edit tickets on request:
- Remove legs by number or odds range.
- Replace legs.
- Convert every leg to another market.
- Split into N slips with split_ticket.
- Book each with book_ticket.

When the user gives a target total odds, keep the total within a few percent of it after every edit and state the final total.

"Value picks" means avoid legs under 1.15 odds.

Start replies with the booking code when there is one.

When asked why a leg was chosen, answer from the numbers you gathered.

For corner picks call get_corner_history for both teams.

For shots, fouls, tackles or cards call get_team_stat_history.
Cite the averages and how many of the last matches cleared the line.

Use get_markets with the search keyword for the market the user wants.

Copy each chosen selection's ref exactly.

A booking code is a SportyBet slip that stakes nothing.

If the user pastes a booking code:
call read_code, leave platform empty unless they said which bookmaker. It is auto-detected.

Users never pick a bookmaker from a menu.
Understand the destination from ordinary text such as:
"convert to Bet9ja", "change this to Sporty" or "I want this on BetNaija".

Then call convert_code with that target.

To move a code to another bookmaker call convert_code.

Never remove, replace or edit a selection silently.

If any leg is suspended, not listed or missing from the created code, say exactly which and ask before continuing.

Only say a code was created if a tool returned it.

If a tool fails, tell the user plainly what is missing.

ALWAYS finish with a written reply to the user. Never end your turn with only tool calls.
"""


# =========================================================
# TOOL DEFINITIONS
# =========================================================

TOOLS = [
    {
        "name": "get_fixtures",
        "description": "List fixtures for a sport between two dates (YYYY-MM-DD).",
        "input_schema": {
            "type": "object",
            "properties": {
                "sport": {"type": "string"},
                "date_from": {"type": "string"},
                "date_to": {"type": "string"},
                "league": {"type": "string"}
            },
            "required": ["sport", "date_from", "date_to"]
        }
    },
    {
        "name": "get_markets",
        "description": "All SportyBet markets and odds for one match.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sport": {"type": "string"},
                "home": {"type": "string"},
                "away": {"type": "string"},
                "search": {
                    "type": "string",
                    "description": "Comma-separated market keywords."
                }
            },
            "required": ["sport", "home", "away"]
        }
    },
    {
        "name": "read_code",
        "description": "Load an existing booking code.",
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "platform": {"type": "string"}
            },
            "required": ["code"]
        }
    },
    {
        "name": "get_stats",
        "description": "Evidence for football or basketball: table position, form and head-to-head.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sport": {"type": "string"},
                "home_id": {"type": "integer"},
                "away_id": {"type": "integer"},
                "league_id": {"type": ["integer", "string"]}
            },
            "required": ["sport", "home_id", "away_id", "league_id"]
        }
    },
    {
        "name": "add_to_ticket",
        "description": "Add picks to the user's ticket. Each needs a reason.",
        "input_schema": {
            "type": "object",
            "properties": {
                "selections": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "home": {"type": "string"},
                            "away": {"type": "string"},
                            "kickoff": {"type": "string"},
                            "sport": {"type": "string"},
                            "ref": {"type": "string"},
                            "market": {"type": "string"},
                            "pick": {"type": "string"},
                            "odds": {"type": "number"},
                            "reason": {"type": "string"}
                        },
                        "required": [
                            "home",
                            "away",
                            "market",
                            "pick",
                            "odds",
                            "reason",
                            "ref"
                        ]
                    }
                }
            },
            "required": ["selections"]
        }
    },
    {
        "name": "remove_legs",
        "description": "Remove ticket legs by number or odds range.",
        "input_schema": {
            "type": "object",
            "properties": {
                "indices": {
                    "type": "array",
                    "items": {"type": "integer"}
                },
                "odds_between": {
                    "type": "array",
                    "items": {"type": "number"}
                }
            }
        }
    },
    {
        "name": "split_ticket",
        "description": "Split the ticket into N slips.",
        "input_schema": {
            "type": "object",
            "properties": {
                "parts": {"type": "integer"}
            },
            "required": ["parts"]
        }
    },
    {
        "name": "get_team_stat_history",
        "description": "Football team history for shots, fouls, tackles and cards.",
        "input_schema": {
            "type": "object",
            "properties": {
                "team_id": {"type": "integer"},
                "league_id": {"type": "integer"},
                "n": {"type": "integer"}
            },
            "required": ["team_id", "league_id"]
        }
    },
    {
        "name": "get_corner_history",
        "description": "Team corner history and averages.",
        "input_schema": {
            "type": "object",
            "properties": {
                "team": {"type": "string"},
                "n": {"type": "integer"}
            },
            "required": ["team"]
        }
    },
    {
        "name": "convert_code",
        "description": "Convert a booking code from one Nigerian bookmaker to another.",
        "input_schema": {
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "code": {"type": "string"},
                "target": {"type": "string"},
                "allow_partial": {"type": "boolean"}
            },
            "required": ["code", "target"]
        }
    },
    {
        "name": "clear_ticket",
        "description": "Empty the ticket.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "book_ticket",
        "description": "Book the ticket on SportyBet and return booking codes.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "convert_ticket",
        "description": "Convert the ticket for another platform: " + ", ".join(PLATFORMS),
        "input_schema": {
            "type": "object",
            "properties": {
                "platform": {
                    "type": "string",
                    "enum": PLATFORMS
                }
            },
            "required": ["platform"]
        }
    }
]


# =========================================================
# GROQ TOOL FORMAT
# =========================================================

GROQ_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["input_schema"]
        }
    }
    for tool in TOOLS
]


# =========================================================
# TICKET HELPERS
# =========================================================

def names(legs):
    return [
        f'{x["home"]} v {x["away"]}: {x["market"]} {x["pick"]} @ {x["odds"]}'
        for x in legs
    ]


def prod(legs):
    return round(math.prod(x["odds"] for x in legs), 2)


# =========================================================
# CHAT HELPERS
# =========================================================

def compact(result, limit=TOOL_RESULT_LIMIT):
    """Serialise a tool result and cap its size to save tokens."""

    text = json.dumps(result, default=str, separators=(",", ":"))

    if len(text) > limit:
        text = text[:limit] + '..."[truncated]"'

    return text


def trim_history(msgs):
    """Keep history short, and never start it with a tool/assistant fragment."""

    if len(msgs) > MAX_HISTORY:
        del msgs[:-MAX_HISTORY]

    while msgs and msgs[0]["role"] != "user":
        del msgs[0]


# =========================================================
# TOOL EXECUTION
# =========================================================

def run_tool(name, a, s):

    t = s["ticket"]

    try:

        if name == "get_fixtures":

            if a["sport"].lower() in (
                "football",
                "soccer",
                "basketball"
            ):

                sport = (
                    "basketball"
                    if a["sport"].lower() == "basketball"
                    else "football"
                )

                return sportapi.fixtures(
                    sport,
                    a["date_from"],
                    a["date_to"],
                    a.get("league")
                )

            return sportybet.list_events(
                a["sport"],
                a["date_from"],
                a["date_to"],
                a.get("league")
            )

        if name == "get_markets":

            return sportybet.get_markets(
                a["sport"],
                a["home"],
                a["away"],
                a.get("search")
            )

        if name == "read_code":

            out = router.read_for_chat(
                a["code"],
                a.get("platform")
            )

            if "legs_list" in out:
                t[:] = out.pop("legs_list")
                s["slips"] = []

            return out

        if name == "get_stats":

            return sportapi.stats(
                a["sport"],
                a["home_id"],
                a["away_id"],
                a["league_id"]
            )

        if name == "add_to_ticket":

            for x in a["selections"]:

                if not x.get("ref"):

                    return {
                        "error": (
                            f"Rejected {x['home']} v {x['away']}: "
                            "missing ref. Call get_markets and copy "
                            "the exact selection ref."
                        )
                    }

                reason = x.get("reason", "")

                if (
                    x.get("sport", "football")
                    in ("football", "basketball")
                    and (
                        len(reason) < 150
                        or sum(c.isdigit() for c in reason) < 8
                    )
                ):

                    return {
                        "error": (
                            f"Rejected {x['home']} v {x['away']}: "
                            "reason must cite real statistics. "
                            "Call get_stats and rewrite."
                        )
                    }

            t.extend(a["selections"])

            return {"ticket_size": len(t)}

        if name == "clear_ticket":

            t.clear()
            s["slips"] = []

            return "cleared"

        if name == "get_team_stat_history":

            return sportapi.team_history(
                a["team_id"],
                a["league_id"],
                a.get("n", 8)
            )

        if name == "get_corner_history":

            return corners.team_corners(
                a["team"],
                a.get("n", 8)
            )

        if name == "convert_code":

            out = router.convert_for_chat(
                a["code"],
                a.get("source"),
                a["target"],
                a.get("allow_partial", False)
            )

            if "ticket" in out:
                t[:] = out.pop("ticket")
                s["slips"] = []

            if "new_code" in out:
                s["codes"].append(out.pop("new_code"))

            return out

        if name == "remove_legs":

            idx = {
                i - 1
                for i in a.get("indices", [])
            }

            rng = a.get("odds_between")

            gone = [
                x for i, x in enumerate(t)
                if i in idx
                or (
                    rng
                    and rng[0] <= x["odds"] <= rng[1]
                )
            ]

            t[:] = [
                x for i, x in enumerate(t)
                if i not in idx
                and not (
                    rng
                    and rng[0] <= x["odds"] <= rng[1]
                )
            ]

            s["slips"] = []

            return {
                "removed": len(gone),
                "ticket_size": len(t),
                "total_odds": prod(t) if t else 0
            }

        if name == "split_ticket":

            n = max(2, int(a["parts"]))
            cap = math.ceil(len(t) / n)

            groups = [[] for _ in range(n)]
            logs = [0.0] * n

            for x in sorted(
                t,
                key=lambda x: -x["odds"]
            ):

                i = min(
                    (
                        j for j in range(n)
                        if len(groups[j]) < cap
                    ),
                    key=lambda j: logs[j]
                )

                groups[i].append(x)
                logs[i] += math.log(x["odds"])

            s["slips"] = groups

            return [
                {
                    "slip": i + 1,
                    "games": len(g),
                    "odds": prod(g) if g else 0
                }
                for i, g in enumerate(groups)
            ]

        if name == "book_ticket":

            out = []

            for g in (s["slips"] or [t]):

                if not g:
                    continue

                r = sportybet.book(g)

                item = {
                    "code": r["code"],
                    "games": len(g),
                    "odds": round(
                        r.get("total_odds") or prod(g),
                        2
                    )
                }

                out.append(item)
                s["codes"].append(item)

            if not out:
                return {"error": "The ticket is empty. Add selections first."}

            return out

        if name == "convert_ticket" and a["platform"] == "msport":

            r = msport.create_msport_code(t)

            item = {
                "code": r["code"],
                "games": r["sent"],
                "odds": prod(t)
            }

            s["codes"].append(item)

            return r

        if name == "convert_ticket":

            lines = [
                (
                    f'{x["home"]} v {x["away"]} | '
                    f'{x["market"]}: {x["pick"]} @ {x["odds"]}'
                )
                for x in t
            ]

            return {
                "platform": a["platform"],
                "status": "manual_slip",
                "slip": lines,
                "note": (
                    "No live adapter for this platform yet. "
                    "Market names may differ; match each line by hand."
                )
            }

        return {"error": f"Unknown tool: {name}"}

    except Exception as e:

        return {
            "error": f"{type(e).__name__}: {e}"
        }


# =========================================================
# GROQ API REQUEST (with retries)
# =========================================================

def groq_request(messages, use_tools=True):

    if not GROQ_API_KEY:
        raise RuntimeError(
            "GROQ_API_KEY is missing from Render environment variables."
        )

    payload = {
        "model": MODEL,
        "messages": messages,
        "max_completion_tokens": 2000,
        "temperature": 0.2
    }

    if use_tools:
        payload["tools"] = GROQ_TOOLS
        payload["tool_choice"] = "auto"
        payload["parallel_tool_calls"] = False

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }

    last_error = ""

    for attempt in range(4):

        try:

            response = httpx.post(
                GROQ_URL,
                headers=headers,
                json=payload,
                timeout=90
            )

        except httpx.TimeoutException:
            last_error = "Groq request timed out."
            continue

        except httpx.RequestError as e:
            last_error = f"Groq connection error: {e}"
            continue

        # Rate limited: wait, then retry
        if response.status_code == 429:

            try:
                wait = float(response.headers.get("retry-after", 5))
            except ValueError:
                wait = 5

            last_error = f"Groq rate limit: {response.text[:300]}"
            time.sleep(min(wait, 20))
            continue

        # Model produced a malformed tool call: retry
        if response.status_code == 400 and "tool_use_failed" in response.text:

            last_error = f"Groq tool_use_failed: {response.text[:300]}"
            continue

        if response.status_code >= 400:

            raise RuntimeError(
                f"Groq HTTP {response.status_code}: {response.text[:800]}"
            )

        data = response.json()

        if not data.get("choices"):
            last_error = "Groq returned an empty response."
            continue

        return data["choices"][0]["message"]

    raise RuntimeError(last_error or "Groq failed after retries.")


# =========================================================
# FASTAPI APPLICATION
# =========================================================

class Chat(BaseModel):
    session: str
    message: str


app = FastAPI()


# =========================================================
# GROQ DIRECT TEST
# =========================================================

@app.get("/api/test-groq")
def test_groq():

    try:

        result = groq_request(
            [
                {
                    "role": "user",
                    "content": "Reply with: Groq is working."
                }
            ],
            use_tools=False
        )

        return {
            "success": True,
            "model": MODEL,
            "reply": result.get("content")
        }

    except Exception as e:

        return {
            "success": False,
            "model": MODEL,
            "error": str(e)
        }


# =========================================================
# CHAT ENDPOINT
# =========================================================

@app.post("/api/chat")
def chat(c: Chat):

    s = SESSIONS.setdefault(
        c.session,
        {
            "history": [],
            "ticket": [],
            "slips": [],
            "codes": []
        }
    )

    msgs = s["history"]

    previous_length = len(msgs)

    msgs.append({
        "role": "user",
        "content": c.message
    })

    try:

        reply = ""

        for step in range(MAX_STEPS):

            r = groq_request(
                [{"role": "system", "content": SYSTEM}] + msgs
            )

            assistant_message = {
                "role": "assistant",
                "content": r.get("content") or ""
            }

            tool_calls = r.get("tool_calls") or []

            if tool_calls:
                assistant_message["tool_calls"] = tool_calls

            msgs.append(assistant_message)

            if not tool_calls:

                reply = (r.get("content") or "").strip()
                break

            for call in tool_calls:

                function = call.get("function", {})

                tool_name = function.get("name")

                raw_args = function.get("arguments") or "{}"

                try:

                    if isinstance(raw_args, str):
                        arguments = json.loads(raw_args)
                    else:
                        arguments = raw_args

                    if not isinstance(arguments, dict):
                        arguments = {}

                    result = run_tool(
                        tool_name,
                        arguments,
                        s
                    )

                except Exception as e:

                    result = {
                        "error": f"{type(e).__name__}: {e}"
                    }

                msgs.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": compact(result)
                })

        # The model returned no text, or ran out of steps:
        # force a plain-text answer with no tools.
        if not reply:

            msgs.append({
                "role": "user",
                "content": (
                    "Now reply to me in plain text. Start with the "
                    "booking code if one exists, then list the picks "
                    "with odds and the stats reason for each. "
                    "If something failed, say exactly what."
                )
            })

            final = groq_request(
                [{"role": "system", "content": SYSTEM}] + msgs,
                use_tools=False
            )

            reply = (final.get("content") or "").strip()

            msgs.append({
                "role": "assistant",
                "content": reply
            })

        if not reply:
            reply = (
                "I couldn't finish that. Try a more specific request, "
                "for example: 'Give me 2 odds from Europa League tonight'."
            )

        trim_history(msgs)

        return {
            "reply": reply,
            "ticket": s["ticket"],
            "codes": s["codes"]
        }

    except Exception as e:

        traceback.print_exc()
        print("CHAT ERROR:", e)

        del msgs[previous_length:]

        raise HTTPException(
            status_code=502,
            detail=str(e)
        )


# =========================================================
# USAGE ENDPOINT
# =========================================================

@app.get("/api/usage")
def usage():

    return {
        "sportapi_calls_today": count_call() - 1
    }


# =========================================================
# BOOKMAKER ENDPOINT
# =========================================================

@app.get("/api/bookmakers")
def bookmaker_status():

    return {
        k: {
            "label": v["label"],
            **bookmakers.caps(k)
        }
        for k, v in bookmakers.BOOKMAKERS.items()
    }


# =========================================================
# HEALTH CHECK
# =========================================================

@app.get("/health")
def health():

    return {
        "ok": True,
        "ai": "Groq",
        "model": MODEL,
        "api_key_configured": bool(GROQ_API_KEY)
    }


# =========================================================
# FRONTEND
# =========================================================

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
