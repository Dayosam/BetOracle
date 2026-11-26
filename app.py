import os
import json
import math
import re
import time
import threading
import traceback
import httpx
import concurrent.futures

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
import prediction_engine

from cache import count_call


# =========================================================
# CONFIGURATION
# =========================================================

load_dotenv()

# GROQ IS TEMPORARILY DISABLED
# Change to True when you want to restore Groq.
GROQ_ENABLED = False

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

MAX_STEPS = int(os.getenv("AI_MAX_STEPS", "5"))
MAX_HISTORY = int(os.getenv("AI_HISTORY_LIMIT", "10"))
TOOL_RESULT_LIMIT = int(os.getenv("AI_TOOL_RESULT_LIMIT", "1800"))

MAX_OUT = int(os.getenv("GROQ_MAX_OUT", "700"))

GROQ_TIMEOUT = float(os.getenv("GROQ_TIMEOUT_SECONDS", "20"))
GROQ_RETRIES = int(os.getenv("GROQ_RETRIES", "1"))

PLATFORMS = list(bookmakers.BOOKMAKERS)

SESSIONS = {}

SESSION_LOCKS = {}
SESSION_LOCKS_GUARD = threading.Lock()


def get_session_lock(session_id):

    with SESSION_LOCKS_GUARD:

        if session_id not in SESSION_LOCKS:

            SESSION_LOCKS[session_id] = threading.Lock()

        return SESSION_LOCKS[session_id]


# =========================================================
# PREDICTION REQUEST DETECTION
# =========================================================

def detect_prediction_request(message):

    text = message.lower().strip()

    excluded = [
        "why did you choose",
        "why did you select",
        "explain your prediction",
        "explain this pick",
        "analyse this",
        "analyze this",
        "convert",
        "booking code",
        "book this ticket",
        "book my ticket",
        "remove",
        "replace",
        "clear my ticket",
        "edit my ticket",
        "change my ticket",
        "what does this mean",
        "how does this work"
    ]

    if any(word in text for word in excluded):
        return None

    patterns = [
        r"(?:give me|get me|find|show me|generate)\s+(\d+)\s+(?:best\s+)?(?:predictions|picks|tips|odds)",
        r"(?:best|strongest)\s+(\d+)\s+(?:predictions|picks|tips)",
        r"(\d+)\s+(?:best\s+)?(?:predictions|picks|tips)\s+(?:for|from|tonight|today)"
    ]

    for pattern in patterns:

        match = re.search(pattern, text)

        if match:

            return {
                "count": min(int(match.group(1)), 20),
                "league": None
            }

    general_requests = [
        "give me predictions",
        "best predictions",
        "strongest picks",
        "today's predictions",
        "tonight's predictions",
        "give me your best picks",
        "find the best picks"
    ]

    if any(term in text for term in general_requests):

        return {
            "count": 2,
            "league": None
        }

    return None


# =========================================================
# SYSTEM INSTRUCTIONS
# =========================================================

SYSTEM = f"""You are BetOracle, a sharp, friendly sports-betting assistant for Nigerian bettors.

Today is {date.today():%A %Y-%m-%d}.

Talk like a normal person, short and direct.

Follow the user's instructions exactly.

IMPORTANT BOOKING RULES:

Generating predictions does not mean the user wants a ticket created.

Adding selections to a ticket requires the user's request.

Creating a SportyBet booking code requires an explicit booking request.

Booking creates a booking code only. You never place real-money bets.

You must never automatically book predictions.

For ordinary prediction requests, the Python prediction engine handles the request.

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

If a sport or market has no statistics source, say plainly if the pick rests on odds only.

Any market on SportyBet is allowed:
corners, fouls, shots, tackles, cards, player props, basketball, darts, tennis and more.

For a target like "give me 25 odds mixing football, basketball and darts":
gather real odds with get_markets for each sport, pick the best-supported selections, and stop when combined odds reach the target.

If odds are unavailable for a sport, say so. Never fill the gap with guessed odds.

You can edit tickets on request:
- Remove legs by number or odds range.
- Replace legs.
- Convert every leg to another market.
- Split into N slips with split_ticket.
- Book each with book_ticket when explicitly requested.

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
        "description": "Book the ticket on SportyBet and return booking codes. Use only after an explicit booking request.",
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
        f'{x["home"]} v {x["away"]}: '
        f'{x["market"]} {x["pick"]} @ {x["odds"]}'
        for x in legs
    ]


def prod(legs):

    return round(
        math.prod(x["odds"] for x in legs),
        2
    )


# =========================================================
# CHAT HELPERS
# =========================================================

def compact(result, limit=TOOL_RESULT_LIMIT):

    text = json.dumps(
        result,
        default=str,
        separators=(",", ":")
    )

    if len(text) > limit:
        text = text[:limit] + '..."[truncated]"'

    return text


def trim_history(msgs):

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

            return {
                "ticket_size": len(t)
            }

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

                s["codes"].append(
                    out.pop("new_code")
                )

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
                    and len(rng) >= 2
                    and rng[0] <= x["odds"] <= rng[1]
                )
            ]

            t[:] = [
                x for i, x in enumerate(t)
                if i not in idx
                and not (
                    rng
                    and len(rng) >= 2
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

            if not t:

                return {
                    "error": "The ticket is empty."
                }

            n = max(
                2,
                min(int(a["parts"]), len(t))
            )

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

                return {
                    "error": "The ticket is empty. Add selections first."
                }

            return out

        if name == "convert_ticket" and a["platform"] == "msport":

            if not t:

                return {
                    "error": "The ticket is empty."
                }

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

        return {
            "error": f"Unknown tool: {name}"
        }

    except Exception as e:

        return {
            "error": f"{type(e).__name__}: {e}"
        }


# =========================================================
# GROQ API REQUEST
# KEPT FOR FUTURE RE-ENABLEMENT
# =========================================================

def groq_request(messages, use_tools=True):

    if not GROQ_ENABLED:

        raise RuntimeError(
            "Groq is disabled for testing."
        )

    if not GROQ_API_KEY:

        raise RuntimeError(
            "GROQ_API_KEY is missing from Render environment variables."
        )

    payload = {
        "model": MODEL,
        "messages": messages,
        "max_completion_tokens": MAX_OUT,
        "temperature": 0.2
    }

    if MODEL.startswith("openai/gpt-oss"):

        payload["reasoning_effort"] = "low"

    if use_tools:

        payload["tools"] = GROQ_TOOLS
        payload["tool_choice"] = "auto"
        payload["parallel_tool_calls"] = False

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }

    last_error = ""

    for attempt in range(GROQ_RETRIES + 1):

        try:

            response = httpx.post(
                GROQ_URL,
                headers=headers,
                json=payload,
                timeout=httpx.Timeout(
                    GROQ_TIMEOUT,
                    connect=5
                )
            )

        except httpx.TimeoutException:

            last_error = (
                f"Groq timed out after {GROQ_TIMEOUT} seconds."
            )

            if attempt < GROQ_RETRIES:

                time.sleep(1)
                continue

            break

        except httpx.RequestError as e:

            last_error = f"Groq connection error: {e}"

            if attempt < GROQ_RETRIES:

                time.sleep(1)
                continue

            break

        if response.status_code == 429:

            last_error = (
                "Groq rate limit reached. "
                + response.text[:300]
            )

            if attempt < GROQ_RETRIES:

                time.sleep(2)
                continue

            break

        if (
            response.status_code == 400
            and "tool_use_failed" in response.text
        ):

            last_error = (
                f"Groq tool_use_failed: {response.text[:300]}"
            )

            if attempt < GROQ_RETRIES:

                time.sleep(1)
                continue

            break

        if response.status_code >= 400:

            raise RuntimeError(
                f"Groq HTTP {response.status_code}: "
                f"{response.text[:800]}"
            )

        try:

            data = response.json()

        except ValueError:

            raise RuntimeError(
                "Groq returned invalid JSON."
            )

        if not data.get("choices"):

            last_error = "Groq returned an empty response."

            if attempt < GROQ_RETRIES:
                continue

            break

        return data["choices"][0]["message"]

    raise RuntimeError(
        last_error or "Groq request failed."
    )


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

    if not GROQ_ENABLED:

        return {
            "success": False,
            "enabled": False,
            "message": "Groq is temporarily disabled."
        }

    started = time.time()

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
            "reply": result.get("content"),
            "elapsed_seconds": round(
                time.time() - started,
                2
            )
        }

    except Exception as e:

        return {
            "success": False,
            "model": MODEL,
            "error": str(e),
            "elapsed_seconds": round(
                time.time() - started,
                2
            )
        }


# =========================================================
# CHAT ENDPOINT
# GROQ DISABLED / PYTHON PREDICTIONS ACTIVE
# =========================================================

@app.post("/api/chat")
def chat(c: Chat):

    started = time.time()

    session_id = c.session.strip()
    message = c.message.strip()

    if not session_id:

        raise HTTPException(
            status_code=400,
            detail="Session ID is required."
        )

    if not message:

        raise HTTPException(
            status_code=400,
            detail="Message cannot be empty."
        )

    lock = get_session_lock(session_id)

    # FIX 1: Add a timeout so the lock cannot get stuck forever.
    # It will wait up to 60 seconds for the previous request to finish.
    if not lock.acquire(timeout=60):

        raise HTTPException(
            status_code=409,
            detail="A request is already processing. Please wait."
        )

    try:

        s = SESSIONS.setdefault(
            session_id,
            {
                "history": [],
                "ticket": [],
                "slips": [],
                "codes": []
            }
        )

        # -------------------------------------------------
        # 1. CHECK PREDICTION REQUEST
        # -------------------------------------------------

        prediction_request = detect_prediction_request(message)

        if prediction_request:

            try:
                # FIX 2: Run the prediction engine in a separate thread
                # with a hard 50-second timeout. If your external API
                # (SportyBet/SportAPI) hangs, this stops the entire
                # server from locking up.
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(
                        prediction_engine.predict,
                        sportapi_module=sportapi,
                        sportybet_module=sportybet,
                        count=prediction_request["count"],
                        league=prediction_request["league"]
                    )
                    result = future.result(timeout=50)

                if not isinstance(result, dict):

                    raise ValueError(
                        "Prediction engine returned an invalid response."
                    )

            except concurrent.futures.TimeoutError:
                raise HTTPException(
                    status_code=504,
                    detail="The prediction engine took too long to respond. Please try again."
                )
            except Exception as e:

                traceback.print_exc()

                raise HTTPException(
                    status_code=502,
                    detail=f"Prediction engine error: {str(e)}"
                )

            return {
                "reply": result.get(
                    "reply",
                    "I couldn't find suitable predictions."
                ),
                "ticket": s["ticket"],
                "codes": s["codes"],
                "predictions": result.get("ticket", []),
                "booking_created": False,
                "ai_enabled": False,
                "prediction_engine": "Python",
                "elapsed_seconds": round(
                    time.time() - started,
                    2
                )
            }

        # -------------------------------------------------
        # 2. GROQ IS DISABLED
        # -------------------------------------------------

        if not GROQ_ENABLED:

            return {
                "reply": (
                    "BetOracle is currently running in no-AI test mode. "
                    "Football predictions are handled by the Python "
                    "prediction engine. Natural-language chat, ticket "
                    "commands and booking-code conversion are temporarily "
                    "unavailable through chat."
                ),
                "ticket": s["ticket"],
                "codes": s["codes"],
                "predictions": [],
                "booking_created": False,
                "ai_enabled": False,
                "elapsed_seconds": round(
                    time.time() - started,
                    2
                )
            }

        # -------------------------------------------------
        # 3. NORMAL CHAT GOES TO GROQ
        # THIS SECTION RUNS ONLY WHEN GROQ_ENABLED = TRUE
        # -------------------------------------------------

        msgs = s["history"]
        previous_length = len(msgs)

        msgs.append({
            "role": "user",
            "content": message
        })

        reply = ""

        for step in range(MAX_STEPS):

            r = groq_request(
                [
                    {
                        "role": "system",
                        "content": SYSTEM
                    }
                ] + msgs
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

                reply = (
                    r.get("content") or ""
                ).strip()

                break

            for call in tool_calls:

                function = call.get("function", {})
                tool_name = function.get("name")

                raw_args = (
                    function.get("arguments") or "{}"
                )

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

        # -------------------------------------------------
        # 4. FINAL RESPONSE
        # -------------------------------------------------

        if not reply:

            msgs.append({
                "role": "user",
                "content": (
                    "Now reply in plain text. Include booking codes "
                    "only if a booking tool actually created one. "
                    "List the picks, odds and available statistics. "
                    "Explain any failed operations."
                )
            })

            final = groq_request(
                [
                    {
                        "role": "system",
                        "content": SYSTEM
                    }
                ] + msgs,
                use_tools=False
            )

            reply = (
                final.get("content") or ""
            ).strip()

            msgs.append({
                "role": "assistant",
                "content": reply
            })

        if not reply:

            reply = (
                "I couldn't finish that. Try a more specific request."
            )

        trim_history(msgs)

        return {
            "reply": reply,
            "ticket": s["ticket"],
            "codes": s["codes"],
            "elapsed_seconds": round(
                time.time() - started,
                2
            )
        }

    except HTTPException:

        raise

    except Exception as e:

        traceback.print_exc()

        print("CHAT ERROR:", e)

        if "msgs" in locals():

            del msgs[previous_length:]

        raise HTTPException(
            status_code=502,
            detail=str(e)
        )

    finally:

        lock.release()


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
        key: {
            "label": value["label"],
            **bookmakers.caps(key)
        }
        for key, value in bookmakers.BOOKMAKERS.items()
    }


# =========================================================
# HEALTH CHECK
# =========================================================

@app.get("/health")
def health():

    return {
        "ok": True,
        "ai": "Disabled for testing",
        "model": MODEL,
        "groq_enabled": GROQ_ENABLED,
        "chat_engine": "Disabled",
        "prediction_engine": "Python",
        "api_key_configured": bool(GROQ_API_KEY),
        "groq_timeout_seconds": GROQ_TIMEOUT,
        "groq_retries": GROQ_RETRIES,
        "max_tool_steps": MAX_STEPS
    }


# =========================================================
# FRONTEND
# =========================================================

STATIC_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "static"
)

if os.path.isdir(STATIC_DIR):

    app.mount(
        "/",
        StaticFiles(
            directory=STATIC_DIR,
            html=True
        )
    )


# =========================================================
# RUN LOCALLY
# =========================================================

# uvicorn app:app --reload
