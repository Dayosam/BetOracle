"""
BetOracle AI — Gemini-backed FastAPI application.

Keeps the existing frontend contract:
  POST /api/chat -> {"reply": ..., "ticket": [...], "codes": [...]}
  GET  /api/usage
  GET  /api/bookmakers
  GET  /health

Required modules: agent.py, sportybet.py, sportapi.py, corners.py,
bookmakers.py, msport.py, convert_router.py, cache.py
Optional: ticket_builder.py
"""

import os
import re
import json
import math
import logging
from datetime import date
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv()
log = logging.getLogger("betoracle.app")

# Gemini engine — this replaces the old Anthropic client.
import agent
import sportybet
import sportapi
import corners
import bookmakers
import msport
import convert_router as router
from cache import count_call

try:
    import ticket_builder
except ImportError:
    ticket_builder = None

PLATFORMS = list(bookmakers.BOOKMAKERS)
SESSION_TTL = int(os.getenv("SESSION_TTL_SECONDS", "604800"))
MAX_MESSAGE_CHARS = int(os.getenv("MAX_MESSAGE_CHARS", "2000"))
MAX_TICKET_LEGS = int(os.getenv("MAX_TICKET_LEGS", "50"))

# In-memory fallback is used only if Upstash is not configured.
SESSIONS: dict[str, dict] = {}
UPSTASH_URL = os.getenv("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")


def _session_key(session_id: str) -> str:
    return "betoracle:session:" + session_id


def _redis_request(command: list):
    if not (UPSTASH_URL and UPSTASH_TOKEN):
        return None
    try:
        response = httpx.post(
            UPSTASH_URL,
            headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"},
            json=command,
            timeout=8.0,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            raise RuntimeError(str(payload["error"]))
        return payload.get("result")
    except Exception:
        log.exception("Upstash request failed")
        return None


def _default_state() -> dict:
    return {
        "history": [],
        "ticket": [],
        "slips": [],
        "codes": [],
        "stats_seen": {},
    }


def _serialize_state(state: dict) -> dict:
    return {
        "history": agent.dump_history(state.get("history", [])),
        "ticket": state.get("ticket", []),
        "slips": state.get("slips", []),
        "codes": state.get("codes", []),
        "stats_seen": state.get("stats_seen", {}),
    }


def _deserialize_state(data: dict) -> dict:
    state = _default_state()
    state.update(data or {})
    state["history"] = agent.load_history(state.get("history", []))
    return state


def get_state(session_id: str) -> dict:
    raw = _redis_request(["GET", _session_key(session_id)])
    if isinstance(raw, str):
        try:
            return _deserialize_state(json.loads(raw))
        except Exception:
            log.warning("Stored session could not be decoded; starting a clean session.")
    if session_id not in SESSIONS:
        SESSIONS[session_id] = _default_state()
    return SESSIONS[session_id]


def save_state(session_id: str, state: dict) -> None:
    data = json.dumps(_serialize_state(state), default=str, separators=(",", ":"))
    if UPSTASH_URL and UPSTASH_TOKEN:
        # Upstash REST accepts SET key value EX seconds.
        _redis_request(["SET", _session_key(session_id), data, "EX", SESSION_TTL])
    else:
        SESSIONS[session_id] = state


def _team_key(home: str, away: str) -> str:
    clean = lambda s: re.sub(r"[^a-z0-9]+", "", str(s).lower())
    return clean(home) + "|" + clean(away)


def _product(legs: list) -> float:
    try:
        return round(math.prod(float(x["odds"]) for x in legs), 2)
    except (TypeError, ValueError, KeyError):
        return 0.0


def _names(legs: list) -> list:
    return [
        f'{x.get("home", "?")} v {x.get("away", "?")}: '
        f'{x.get("market", "?")} {x.get("pick", "?")} @ {x.get("odds", "?")}'
        for x in legs
    ]


SYSTEM = f"""You are BetOracle, a sports-betting research and booking-code assistant for Nigerian users.
Today is {date.today():%A %Y-%m-%d}. Reply in the user's language, naturally and concisely.

CORE RULES
- Never invent fixtures, odds, statistics, market names, probabilities, confidence scores, or booking codes.
- Use tools for live fixtures, markets, statistics, code reading and conversion.
- SportAPI.ai is the primary data source for football and basketball fixtures/statistics.
- SportyBet Nigeria is the default source of markets, odds and booking codes.
- Analyse match evidence independently of bookmaker odds. Use odds only to choose an actual available market.
- If evidence is insufficient, say NO BET. Never force a selection just to reach a target total.
- A combined odds target is not a probability or a guarantee.
- A booking code is not a placed bet. Never claim money has been staked.
- Do not silently remove, substitute or alter any selection. List unavailable legs and ask the user before partial conversion.
- Only claim a booking code exists when book_ticket returns one.

EVIDENCE
- For football and basketball selections, call get_stats before recommending or adding a selection.
- Explain the actual returned statistics, including standings/form/H2H where the provider supplies them.
- Do not invent missing fields. Clearly state when a statistic is unavailable.
- For football corners, call get_corner_history for both teams.
- For shots, shots on target, fouls, tackles or yellow cards, call get_team_stat_history.
- If the requested market has no relevant statistics source, say the selection is based on limited evidence or odds only; do not present it as strongly researched.
- Do not claim that a tool was used unless it actually returned usable data.

TICKETS
- Use get_markets to find the exact market and copy its ref exactly.
- Every selection added must have a real market ref, real odds, and an evidence-based reason.
- Respect the user's requested target odds, but do not add weak picks to reach it.
- When asked to remove, replace, split or convert selections, preserve every unaffected leg.
- For a booking code, first show the ticket summary and obtain clear user approval before calling book_ticket.
- If the user explicitly asks in the current message to generate/book/create the booking code for the ticket, that is approval for code generation.
- A code is only created after the user has approved and the booking tool succeeds.
- Start with the booking code when one has been returned.

PLATFORMS
- Users should not need a bookmaker menu. Understand destinations from ordinary language.
- Use convert_code when converting an existing code, and convert_ticket for the current in-memory ticket.
- Only connected bookmaker adapters can produce codes. If no adapter exists, provide a clearly labelled manual slip.
- Never state that a manual slip is a converted booking code.

If a tool fails, explain plainly what failed and what information is missing.
"""


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
                "league": {"type": "string"},
            },
            "required": ["sport", "date_from", "date_to"],
        },
    },
    {
        "name": "get_markets",
        "description": "Get SportyBet markets and current odds for one match. Use exact outcome refs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sport": {"type": "string"},
                "home": {"type": "string"},
                "away": {"type": "string"},
                "search": {"type": "string"},
            },
            "required": ["sport", "home", "away"],
        },
    },
    {
        "name": "read_code",
        "description": "Read an existing bookmaker booking code and load its selections into the current ticket.",
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "platform": {"type": "string"},
            },
            "required": ["code"],
        },
    },
    {
        "name": "get_stats",
        "description": "Retrieve football or basketball evidence: standings, recent form, venue form and H2H.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sport": {"type": "string"},
                "home_id": {"type": ["integer", "string"]},
                "away_id": {"type": ["integer", "string"]},
                "league_id": {"type": ["integer", "string"]},
                "home": {"type": "string"},
                "away": {"type": "string"},
            },
            "required": ["sport", "home_id", "away_id", "league_id", "home", "away"],
        },
    },
    {
        "name": "add_to_ticket",
        "description": "Add validated selections. Use exact refs returned by get_markets.",
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
                            "reason": {"type": "string"},
                        },
                        "required": ["home", "away", "market", "pick", "odds", "reason", "ref"],
                    },
                }
            },
            "required": ["selections"],
        },
    },
    {
        "name": "remove_legs",
        "description": "Remove ticket legs by 1-based index and/or inclusive odds range.",
        "input_schema": {
            "type": "object",
            "properties": {
                "indices": {"type": "array", "items": {"type": "integer"}},
                "odds_between": {"type": "array", "items": {"type": "number"}},
            },
        },
    },
    {
        "name": "split_ticket",
        "description": "Split the current ticket into N balanced slips.",
        "input_schema": {
            "type": "object",
            "properties": {"parts": {"type": "integer"}},
            "required": ["parts"],
        },
    },
    {
        "name": "get_team_stat_history",
        "description": "Football team history for shots, shots on target, fouls, tackles and yellow cards. No corners.",
        "input_schema": {
            "type": "object",
            "properties": {
                "team_id": {"type": "integer"},
                "league_id": {"type": "integer"},
                "n": {"type": "integer"},
            },
            "required": ["team_id", "league_id"],
        },
    },
    {
        "name": "get_corner_history",
        "description": "Football corner history for a team: corners for, against, total and averages.",
        "input_schema": {
            "type": "object",
            "properties": {"team": {"type": "string"}, "n": {"type": "integer"}},
            "required": ["team"],
        },
    },
    {
        "name": "convert_code",
        "description": "Convert an existing booking code to another connected Nigerian bookmaker.",
        "input_schema": {
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "code": {"type": "string"},
                "target": {"type": "string"},
                "allow_partial": {"type": "boolean"},
            },
            "required": ["code", "target"],
        },
    },
    {
        "name": "clear_ticket",
        "description": "Clear all current ticket legs and split slips.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "book_ticket",
        "description": "Generate SportyBet booking code(s) for the current approved ticket. Does not place a bet.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "convert_ticket",
        "description": "Convert the current ticket to a bookmaker. Only connected adapters can create codes.",
        "input_schema": {
            "type": "object",
            "properties": {"platform": {"type": "string", "enum": PLATFORMS}},
            "required": ["platform"],
        },
    },
]

MUTATING_TOOLS = {
    "read_code", "add_to_ticket", "remove_legs", "split_ticket",
    "clear_ticket", "book_ticket", "convert_code", "convert_ticket",
}


def run_tool(name: str, a: dict, state: dict, booking_approved: bool = False) -> Any:
    ticket = state["ticket"]
    try:
        if name == "get_fixtures":
            sport = str(a["sport"]).lower()
            if sport in ("football", "soccer", "basketball"):
                provider_sport = "basketball" if sport == "basketball" else "football"
                return sportapi.fixtures(
                    provider_sport, a["date_from"], a["date_to"], a.get("league")
                )
            return sportybet.list_events(a["sport"], a["date_from"], a["date_to"], a.get("league"))

        if name == "get_markets":
            return sportybet.get_markets(a["sport"], a["home"], a["away"], a.get("search"))

        if name == "get_stats":
            result = sportapi.stats(
                a["sport"], a["home_id"], a["away_id"], a["league_id"]
            )
            if isinstance(result, dict) and not result.get("error"):
                key = _team_key(a.get("home", ""), a.get("away", ""))
                # Keep the raw provider response as proof that stats were fetched.
                state["stats_seen"][key] = result
            return result

        if name == "read_code":
            result = router.read_for_chat(a["code"], a.get("platform"))
            if isinstance(result, dict) and "legs_list" in result:
                state["ticket"] = list(result.pop("legs_list") or [])
                state["slips"] = []
            return result

        if name == "add_to_ticket":
            selections = a.get("selections") or []
            if not selections:
                return {"error": "No selections were supplied."}
            if len(ticket) + len(selections) > MAX_TICKET_LEGS:
                return {"error": f"Ticket limit is {MAX_TICKET_LEGS} selections."}
            for item in selections:
                if not item.get("ref"):
                    return {"error": f"{item.get('home')} v {item.get('away')}: missing exact SportyBet market ref."}
                try:
                    odds = float(item["odds"])
                except (TypeError, ValueError, KeyError):
                    return {"error": "A selection has invalid odds."}
                if not math.isfinite(odds) or odds <= 1:
                    return {"error": "Every selection must have valid decimal odds greater than 1."}
                if not item.get("reason") or len(str(item["reason"])) < 40:
                    return {"error": "Each selection needs a specific evidence-based explanation."}
                if str(item.get("sport", "football")).lower() in ("football", "soccer", "basketball"):
                    key = _team_key(item.get("home", ""), item.get("away", ""))
                    if not state["stats_seen"].get(key):
                        return {
                            "error": f"No successful get_stats result is recorded for {item.get('home')} v {item.get('away')}. Fetch its stats first. Include the exact team names in get_stats."
                        }
            ticket.extend(selections)
            state["slips"] = []
            return {"ticket_size": len(ticket), "total_odds": _product(ticket), "selections": _names(ticket)}

        if name == "remove_legs":
            indices = {int(i) - 1 for i in a.get("indices", []) if int(i) > 0}
            odds_range = a.get("odds_between")
            if odds_range and len(odds_range) != 2:
                return {"error": "odds_between must contain [low, high]."}
            low, high = (float(odds_range[0]), float(odds_range[1])) if odds_range else (None, None)
            keep, removed = [], []
            for i, leg in enumerate(ticket):
                odds = float(leg.get("odds", 0))
                if i in indices or (odds_range and low <= odds <= high):
                    removed.append({"index": i + 1, "selection": _names([leg])[0]})
                else:
                    keep.append(leg)
            state["ticket"] = keep
            state["slips"] = []
            return {"removed": removed, "ticket_size": len(keep), "total_odds": _product(keep)}

        if name == "split_ticket":
            if not ticket:
                return {"error": "There are no selections to split."}
            parts = int(a["parts"])
            if parts < 2:
                return {"error": "Choose at least 2 slips."}
            if parts > len(ticket):
                return {"error": f"Cannot split {len(ticket)} selections into {parts} non-empty slips."}
            groups = [[] for _ in range(parts)]
            log_totals = [0.0] * parts
            # Greedy balancing: largest odds first, placing each into the lowest-odds group.
            for leg in sorted(ticket, key=lambda x: float(x["odds"]), reverse=True):
                idx = min(range(parts), key=lambda j: (log_totals[j], len(groups[j])))
                groups[idx].append(leg)
                log_totals[idx] += math.log(float(leg["odds"]))
            state["slips"] = groups
            return [
                {"slip": i + 1, "games": len(g), "odds": _product(g), "selections": _names(g)}
                for i, g in enumerate(groups)
            ]

        if name == "get_team_stat_history":
            return sportapi.team_history(a["team_id"], a["league_id"], a.get("n", 8))

        if name == "get_corner_history":
            return corners.team_corners(a["team"], a.get("n", 8))

        if name == "convert_code":
            result = router.convert_for_chat(
                a["code"], a.get("source"), a["target"], bool(a.get("allow_partial", False))
            )
            if isinstance(result, dict) and "ticket" in result:
                state["ticket"] = list(result.pop("ticket") or [])
                state["slips"] = []
            if isinstance(result, dict) and result.get("new_code"):
                state["codes"].append(result["new_code"])
            return result

        if name == "clear_ticket":
            state["ticket"] = []
            state["slips"] = []
            return {"status": "cleared"}

        if name == "book_ticket":
            if not booking_approved:
                return {
                    "error": "Booking-code generation requires the user's explicit approval in the current message. Show the ticket and ask for approval."
                }
            groups = state["slips"] or ([ticket] if ticket else [])
            if not groups:
                return {"error": "The ticket is empty. Add selections first."}
            created = []
            for group in groups:
                result = sportybet.book(group)
                if not isinstance(result, dict) or not result.get("code"):
                    return {
                        "error": "SportyBet did not return a confirmed booking code. No code has been recorded.",
                        "partial_codes_created": created,
                    }
                created.append({
                    "code": result["code"],
                    "games": len(group),
                    "odds": round(float(result.get("total_odds") or _product(group)), 2),
                })
            state["codes"].extend(created)
            return {"status": "created", "booking_codes": created}

        if name == "convert_ticket":
            platform = str(a["platform"]).lower()
            if not ticket:
                return {"error": "The ticket is empty."}
            if platform == "msport":
                result = msport.create_msport_code(ticket)
                if not isinstance(result, dict) or not result.get("code"):
                    return {"error": "MSport did not return a confirmed code.", "details": result}
                code_item = {"code": result["code"], "games": result.get("sent", len(ticket)), "odds": _product(ticket)}
                state["codes"].append(code_item)
                return result
            return {
                "platform": platform,
                "status": "manual_slip",
                "slip": _names(ticket),
                "note": "No live code-generation adapter is connected for this bookmaker. This is not a booking code.",
            }

        return {"error": f"Unknown tool: {name}"}

    except Exception as exc:
        log.exception("Tool %s failed", name)
        return {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}


def _explicit_booking_request(message: str) -> bool:
    text = message.lower()
    patterns = (
        r"\b(book|generate|create)\b.{0,35}\b(code|booking code|slip)\b",
        r"\b(book|generate|create)\s+(it|this|the ticket|the slip)\b",
        r"\bbooking code\b",
    )
    return any(re.search(pattern, text) for pattern in patterns)


class Chat(BaseModel):
    session: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


app = FastAPI(title="BetOracle AI", version="1.0.0")


@app.post("/api/chat")
def chat(c: Chat):
    session_id = c.session.strip()
    message = c.message.strip()
    if not session_id or not message:
        raise HTTPException(status_code=400, detail="Session and message are required.")

    state = get_state(session_id)
    approved = _explicit_booking_request(message)

    try:
        reply = agent.chat(
            state,
            message,
            system_text=SYSTEM,
            tool_specs=TOOLS,
            run_tool=lambda name, args: run_tool(name, args, state, approved),
            mutating=MUTATING_TOOLS,
            signature=lambda current: json.dumps({
                "ticket": current.get("ticket", []),
                "slips": current.get("slips", []),
                "codes": current.get("codes", []),
            }, sort_keys=True, default=str),
        )
    except agent.AIUnavailable as exc:
        save_state(session_id, state)
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        log.exception("Chat request failed")
        save_state(session_id, state)
        raise HTTPException(status_code=500, detail="BetOracle could not complete this request. Please try again.")

    save_state(session_id, state)
    return {
        "reply": reply,
        "ticket": state["ticket"],
        "codes": state["codes"],
    }


@app.get("/api/usage")
def usage():
    try:
        return {"sportapi_calls_today": max(0, count_call() - 1)}
    except Exception:
        return {"sportapi_calls_today": None, "note": "Usage counter unavailable."}


@app.get("/api/bookmakers")
def bookmaker_status():
    result = {}
    for key, value in bookmakers.BOOKMAKERS.items():
        try:
            caps = bookmakers.caps(key)
        except Exception:
            caps = {"connected": False}
        result[key] = {"label": value.get("label", key), **caps}
    return result


@app.get("/health")
def health():
    return {
        "ok": True,
        "ai": "gemini",
        "gemini_configured": agent.configured(),
        "session_storage": "upstash" if UPSTASH_URL and UPSTASH_TOKEN else "memory_only",
    }


static_dir = Path(__file__).resolve().parent / "static"
if static_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
else:
    log.warning("Static directory not found: %s", static_dir)
