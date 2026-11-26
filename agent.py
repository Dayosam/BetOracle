"""
BetOracle Mistral conversation engine.

Features:
- Mistral Chat Completions API
- Multiple function/tool calls
- Correct tool-call ID matching
- Duplicate-call prevention
- Conversation history
- Tool-result size limits
- Retry handling for temporary server errors
- Rate-limit detection
"""

import os
import json
import time
import logging
import random
import httpx

log = logging.getLogger("betoracle.agent")

MODEL = os.getenv("MISTRAL_MODEL", "mistral-small-latest").strip()

API_URL = os.getenv(
    "MISTRAL_API_URL",
    "https://api.mistral.ai/v1/chat/completions"
).strip()

MAX_STEPS = int(os.getenv("AI_MAX_STEPS", "7"))
MAX_TOOL_CALLS = int(os.getenv("AI_MAX_TOOL_CALLS", "20"))
HISTORY_LIMIT = int(os.getenv("AI_HISTORY_LIMIT", "16"))

RESULT_LIMIT = 10000
TIMEOUT = float(os.getenv("MISTRAL_TIMEOUT_SECONDS", "60"))


class AIUnavailable(Exception):
    """An AI error that can be safely reported to the user."""


def configured():
    return bool(os.getenv("MISTRAL_API_KEY"))


# =========================================================
# TOOL DECLARATIONS
# =========================================================

def _clean_schema(schema):
    """Clean JSON schema for Mistral function declarations."""

    if not isinstance(schema, dict):
        return {"type": "string"}

    allowed = {
        "type",
        "description",
        "properties",
        "required",
        "items",
        "enum",
        "additionalProperties",
        "minimum",
        "maximum",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "pattern",
        "format"
    }

    result = {}

    for key, value in schema.items():

        if key not in allowed:
            continue

        if key == "properties" and isinstance(value, dict):
            result[key] = {
                name: _clean_schema(child)
                for name, child in value.items()
            }

        elif key == "items":
            result[key] = _clean_schema(value)

        elif key == "additionalProperties" and isinstance(value, dict):
            result[key] = _clean_schema(value)

        else:
            result[key] = value

    result.setdefault("type", "object")

    return result


def declarations(tool_specs):

    tools = []

    for spec in tool_specs or []:

        name = spec.get("name")

        if not name:
            continue

        parameters = _clean_schema(
            spec.get("input_schema") or {
                "type": "object",
                "properties": {}
            }
        )

        if parameters.get("type") != "object":

            parameters = {
                "type": "object",
                "properties": {
                    "value": parameters
                },
                "required": ["value"]
            }

        tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": spec.get("description", ""),
                "parameters": parameters
            }
        })

    return tools


# =========================================================
# TOOL RESULT HANDLING
# =========================================================

def _shrink(obj, limit=RESULT_LIMIT):

    obj = json.loads(json.dumps(obj, default=str))

    def size(value):
        return len(
            json.dumps(
                value,
                ensure_ascii=False,
                default=str
            )
        )

    if size(obj) <= limit:
        return obj

    def cut(value, count):

        if isinstance(value, list):

            result = [
                cut(item, count)
                for item in value[:count]
            ]

            if len(value) > count:
                result.append(
                    f"... {len(value) - count} more items omitted"
                )

            return result

        if isinstance(value, dict):
            return {
                key: cut(item, count)
                for key, item in value.items()
            }

        if isinstance(value, str) and len(value) > 500:
            return value[:500] + "..."

        return value

    for count in (40, 20, 10, 5):

        smaller = cut(obj, count)

        if size(smaller) <= limit:
            return smaller

    return {
        "note": "Result too large; ask for a narrower query.",
        "preview": json.dumps(
            obj,
            ensure_ascii=False,
            default=str
        )[:1500]
    }


def _wrap(value):

    if isinstance(value, dict):
        return value

    return {"result": value}


# =========================================================
# HISTORY MANAGEMENT
# =========================================================

def dump_history(history):

    try:

        return [
            json.dumps(
                item,
                ensure_ascii=False,
                default=str
            )
            for item in (history or [])
            if isinstance(item, dict)
        ]

    except Exception:

        log.exception(
            "Could not serialize Mistral conversation history"
        )

        return []


def load_history(items):

    restored = []

    try:

        for item in items or []:

            if isinstance(item, dict):
                message = item

            elif isinstance(item, str):
                message = json.loads(item)

            else:
                continue

            if (
                isinstance(message, dict)
                and message.get("role") in {
                    "system",
                    "user",
                    "assistant",
                    "tool"
                }
            ):
                restored.append(message)

    except Exception:

        log.warning(
            "Previous conversation history could not be restored. "
            "It may use the old Gemini format."
        )

        return []

    return restored


def _trim(history, limit=HISTORY_LIMIT):

    if len(history) <= limit:
        return

    starts = [
        index
        for index, message in enumerate(history)
        if message.get("role") == "user"
    ]

    for index in starts:

        if len(history) - index <= limit:

            del history[:index]
            return

    if starts:
        del history[:starts[-1]]


# =========================================================
# MISTRAL API REQUEST
# =========================================================

def _request(messages, tools=None, no_tools=False):

    api_key = os.getenv("MISTRAL_API_KEY")

    if not api_key:

        raise AIUnavailable(
            "MISTRAL_API_KEY is missing from the server environment."
        )

    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": float(
            os.getenv("MISTRAL_TEMPERATURE", "0.2")
        ),
        "max_tokens": int(
            os.getenv("MISTRAL_MAX_TOKENS", "4096")
        )
    }

    if tools and not no_tools:

        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    elif no_tools:

        payload["tool_choice"] = "none"

    delay = 2.0

    for attempt in range(3):

        try:

            with httpx.Client(timeout=TIMEOUT) as client:

                response = client.post(
                    API_URL,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        "Accept": "application/json"
                    },
                    json=payload
                )

            if response.status_code == 200:

                data = response.json()

                choices = data.get("choices") or []

                if not choices:
                    raise AIUnavailable(
                        "Mistral returned an empty response."
                    )

                return choices[0].get("message") or {}

            try:

                error_data = response.json()

            except Exception:

                error_data = {
                    "message": response.text[:1000]
                }

            error_text = json.dumps(
                error_data,
                ensure_ascii=False
            )[:1400]

            # Authentication errors

            if response.status_code in (401, 403):

                log.error(
                    "Mistral authentication error: %s",
                    error_text
                )

                raise AIUnavailable(
                    "Mistral rejected the API key or account permissions. "
                    "Check MISTRAL_API_KEY and API access."
                )

            # Invalid model or endpoint

            if response.status_code == 404:

                raise AIUnavailable(
                    f"Mistral model or endpoint not found. "
                    f"Check MISTRAL_MODEL: {MODEL}"
                )

            # Rate limit or exhausted quota

            if response.status_code == 429:

                log.error(
                    "Mistral rate/quota limit: %s",
                    error_text
                )

                raise AIUnavailable(
                    "Mistral rate or usage limit reached. "
                    "Check your Mistral account limits and billing."
                )

            # Temporary server errors

            if response.status_code in (500, 502, 503, 504):

                if attempt < 2:

                    wait = delay + random.uniform(0, 0.5)

                    log.warning(
                        "Mistral temporary server error %s. "
                        "Retrying in %.1fs",
                        response.status_code,
                        wait
                    )

                    time.sleep(wait)

                    delay *= 2

                    continue

                raise AIUnavailable(
                    "Mistral is temporarily unavailable. "
                    "Please try again."
                )

            log.error(
                "Mistral rejected request (%s): %s",
                response.status_code,
                error_text
            )

            raise AIUnavailable(
                f"Mistral rejected the request "
                f"({response.status_code}): {error_text[:350]}"
            )

        except httpx.TimeoutException as exc:

            if attempt < 2:

                wait = delay + random.uniform(0, 0.5)

                log.warning(
                    "Mistral timed out. Retrying in %.1fs",
                    wait
                )

                time.sleep(wait)

                delay *= 2

                continue

            raise AIUnavailable(
                "Mistral took too long to respond."
            ) from exc

        except httpx.RequestError as exc:

            if attempt < 2:

                wait = delay + random.uniform(0, 0.5)

                log.warning(
                    "Mistral network error. Retrying in %.1fs",
                    wait
                )

                time.sleep(wait)

                delay *= 2

                continue

            log.exception("Mistral network request failed")

            raise AIUnavailable(
                "Could not connect to Mistral."
            ) from exc

    raise AIUnavailable(
        "Mistral could not complete the request."
    )


# =========================================================
# RESPONSE HELPERS
# =========================================================

def _message_text(message):

    content = message.get("content")

    if isinstance(content, str):
        return content.strip()

    if isinstance(content, list):

        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict)
            and part.get("type") == "text"
        ).strip()

    return ""


def _parse_arguments(raw):

    if isinstance(raw, dict):
        return raw

    if not raw:
        return {}

    try:

        parsed = json.loads(raw)

        if isinstance(parsed, dict):
            return parsed

        return {"value": parsed}

    except (TypeError, ValueError):

        raise ValueError(
            "Mistral returned invalid JSON tool arguments."
        )


# =========================================================
# MAIN CONVERSATION ENGINE
# =========================================================

def chat(
    state,
    user_text,
    *,
    system_text,
    tool_specs,
    run_tool,
    mutating=(),
    signature=lambda s: "",
    nudge=None
):
    """
    One user message -> final response.

    Keeps the original app.py interface.
    """

    if not configured():

        raise AIUnavailable(
            "The AI is not configured: "
            "MISTRAL_API_KEY is missing on the server."
        )

    if not isinstance(user_text, str) or not user_text.strip():

        raise AIUnavailable(
            "Please enter a message."
        )

    history = state.setdefault("history", [])

    history[:] = [
        item
        for item in history
        if isinstance(item, dict)
    ]

    start = len(history)

    history.append({
        "role": "user",
        "content": user_text.strip()
    })

    tools = declarations(tool_specs)

    done = {}

    n_calls = 0
    nudged = False
    answer = ""

    try:

        for _step in range(MAX_STEPS):

            messages = []

            if system_text:

                messages.append({
                    "role": "system",
                    "content": system_text
                })

            messages.extend(history)

            assistant_message = _request(
                messages,
                tools=tools,
                no_tools=False
            )

            # Preserve the complete assistant response,
            # including its function calls.

            history.append(assistant_message)

            tool_calls = (
                assistant_message.get("tool_calls") or []
            )

            # No tools requested: normal answer.

            if not tool_calls:

                answer = _message_text(
                    assistant_message
                )

                message = (
                    nudge(state)
                    if nudge and not nudged
                    else None
                )

                if message:

                    nudged = True

                    history.append({
                        "role": "user",
                        "content": str(message)
                    })

                    continue

                break

            # Execute requested tools.

            for tool_call in tool_calls:

                call_id = tool_call.get("id")

                function = (
                    tool_call.get("function") or {}
                )

                function_name = function.get("name", "")

                raw_args = function.get(
                    "arguments",
                    "{}"
                )

                n_calls += 1

                try:

                    args = _parse_arguments(raw_args)

                except Exception as exc:

                    args = {}

                    result = {
                        "error": str(exc)
                    }

                else:

                    key = (
                        function_name,

                        json.dumps(
                            args,
                            sort_keys=True,
                            default=str
                        ),

                        signature(state)
                        if function_name in mutating
                        else ""
                    )

                    if n_calls > MAX_TOOL_CALLS:

                        result = {
                            "error": (
                                "Tool-call limit reached. "
                                "Summarise the information already retrieved."
                            )
                        }

                    elif key in done:

                        result = {
                            **_wrap(done[key]),
                            "duplicate_call_ignored": True
                        }

                    else:

                        try:

                            result = run_tool(
                                function_name,
                                args
                            )

                        except Exception as exc:

                            log.exception(
                                "Tool %s failed",
                                function_name
                            )

                            result = {
                                "error": (
                                    f"{type(exc).__name__}: "
                                    f"{str(exc)[:500]}"
                                )
                            }

                        done[key] = result

                # Return each tool result with the original call ID.

                history.append({
                    "role": "tool",
                    "name": function_name,
                    "tool_call_id": call_id,
                    "content": json.dumps(
                        _wrap(_shrink(result)),
                        ensure_ascii=False,
                        default=str
                    )
                })

        else:

            # Maximum AI steps reached.
            # Ask Mistral for a final answer without tools.

            history.append({
                "role": "user",
                "content": (
                    "[system] Step limit reached. "
                    "Do not call any more tools. "
                    "Summarise what you found, "
                    "what is still missing, "
                    "and what the user should do next."
                )
            })

            messages = []

            if system_text:

                messages.append({
                    "role": "system",
                    "content": system_text
                })

            messages.extend(history)

            final_message = _request(
                messages,
                tools=tools,
                no_tools=True
            )

            history.append(final_message)

            answer = _message_text(
                final_message
            )

    except Exception:

        # Remove unfinished conversation turn.

        del history[start:]

        raise

    _trim(history)

    return answer or (
        "I couldn't put an answer together. "
        "Please rephrase or try again."
    )
