"""BetOracle Gemini conversation engine."""
import os
import json
import time
import logging
import threading
log = logging.getLogger("betoracle.agent")
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash").strip()
MAX_STEPS = max(1, int(os.getenv("AI_MAX_STEPS", "7")))
MAX_TOOL_CALLS = max(1, int(os.getenv("AI_MAX_TOOL_CALLS", "20")))
HISTORY_LIMIT = max(4, int(os.getenv("AI_HISTORY_LIMIT", "16")))
RESULT_LIMIT = 10000
THINKING = os.getenv("GEMINI_THINKING_LEVEL", "").lower()
_GENERATE_LOCK = threading.Lock()
class AIUnavailable(Exception):
    pass
def configured():
    return bool(os.getenv("GEMINI_API_KEY"))
_client = None
def _get_client():
    global _client
    if _client is None:
        from google import genai
        from google.genai import types
        _client = genai.Client(
            api_key=os.environ["GEMINI_API_KEY"],
            http_options=types.HttpOptions(
                timeout=int(os.getenv("GEMINI_TIMEOUT_MS", "60000"))
            )
        )
    return _client
# ---------- Tool declarations ----------
def _clean(schema):
    if not isinstance(schema, dict):
        return {"type": "string"}
    typ = schema.get("type", "string")
    if isinstance(typ, list):
        typ = next((x for x in typ if x != "null"), "string")
    out = {"type": str(typ).lower()}
    if schema.get("description"):
        out["description"] = schema["description"]
    if schema.get("enum"):
        out["enum"] = [str(x) for x in schema["enum"]]
    if out["type"] == "object":
        props = schema.get("properties") or {}
        out["properties"] = {
            k: _clean(v) for k, v in props.items()
        }
        required = schema.get("required") or []
        if required:
            out["required"] = [
                x for x in required if x in out["properties"]
            ]
    if out["type"] == "array":
        out["items"] = _clean(
            schema.get("items") or {"type": "string"}
        )
    return out
def declarations(tool_specs):
    result = []
    for tool in tool_specs or []:
        params = _clean(
            tool.get("input_schema") or {
                "type": "object",
                "properties": {}
            }
        )
        item = {
            "name": tool["name"],
            "description": tool.get("description", "")
        }
        if params.get("properties"):
            item["parameters"] = params
        result.append(item)
    return result
def _config(types, system_text, tool_specs, no_tools=False):
    kwargs = {
        "system_instruction": system_text,
        "max_output_tokens": int(
            os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "4096")
        )
    }
    tool_declarations = declarations(tool_specs)
    if tool_declarations:
        kwargs["tools"] = [
            types.Tool(function_declarations=tool_declarations)
        ]
        if no_tools:
            kwargs["tool_config"] = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode="NONE"
                )
            )
        else:
            kwargs["automatic_function_calling"] = (
                types.AutomaticFunctionCallingConfig(
                    disable=True
                )
            )
    if THINKING in ("low", "medium", "high"):
        kwargs["thinking_config"] = types.ThinkingConfig(
            thinking_level=THINKING
        )
    return types.GenerateContentConfig(**kwargs)
# ---------- Result handling ----------
def _size(value):
    return len(json.dumps(value, default=str))
def _shrink(obj, limit=RESULT_LIMIT):
    obj = json.loads(json.dumps(obj, default=str))
    if _size(obj) <= limit:
        return obj
    def cut(value, count):
        if isinstance(value, list):
            out = [cut(x, count) for x in value[:count]]
            if len(value) > count:
                out.append(
                    f"... {len(value) - count} more items omitted"
                )
            return out
        if isinstance(value, dict):
            return {
                k: cut(v, count) for k, v in value.items()
            }
        if isinstance(value, str) and len(value) > 500:
            return value[:500] + "..."
        return value
    for count in (40, 20, 10, 5):
        smaller = cut(obj, count)
        if _size(smaller) <= limit:
            return smaller
    return {
        "note": "Result too large; ask for a narrower query.",
        "preview": json.dumps(obj, default=str)[:1500]
    }
def _wrap(value):
    return value if isinstance(value, dict) else {
        "result": value
    }
# ---------- Error handling ----------
def _status(exc):
    for attr in ("code", "status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None
def _error_message(exc):
    message = str(exc)
    key = os.getenv("GEMINI_API_KEY")
    if key:
        message = message.replace(key, "[REDACTED]")
    return message[:1200]
def _is_timeout(exc):
    return (
        "timeout" in type(exc).__name__.lower()
        or "timed out" in str(exc).lower()
    )
def _generate(client, contents, config):
    """Retry temporary server errors, but never blindly retry 429."""
    if contents is None or contents == []:
        raise AIUnavailable(
            "There is no conversation content to send."
        )
    attempts = 3
    delay = 2.0
    for attempt in range(attempts):
        try:
            with _GENERATE_LOCK:
                return client.models.generate_content(
                    model=MODEL,
                    contents=contents,
                    config=config
                )
        except Exception as exc:
            status = _status(exc)
            details = _error_message(exc)
            if status in (401, 403):
                log.error(
                    "Gemini authentication error: %s",
                    details
                )
                raise AIUnavailable(
                    "Gemini rejected the API key or permissions. "
                    "Check GEMINI_API_KEY."
                ) from exc
            if status == 404:
                raise AIUnavailable(
                    f"Gemini model '{MODEL}' was not found. "
                    "Check GEMINI_MODEL."
                ) from exc
            if status == 429:
                log.error(
                    "GEMINI HTTP 429 RATE/QUOTA LIMIT: %s",
                    details
                )
                raise AIUnavailable(
                    "Gemini has reached an API rate or quota limit. "
                    "Please wait before trying again. "
                    "Check Google AI Studio usage and rate limits. "
                    + details[:450]
                ) from exc
            transient = (
                status in (500, 502, 503, 504)
                or _is_timeout(exc)
            )
            if transient and attempt < attempts - 1:
                log.warning(
                    "Gemini temporary error %s. "
                    "Retry %s/%s in %.1fs",
                    status,
                    attempt + 1,
                    attempts - 1,
                    delay
                )
                time.sleep(delay)
                delay *= 2
                continue
            if transient:
                log.error(
                    "Gemini temporary failure: %s",
                    details
                )
                raise AIUnavailable(
                    "Gemini is temporarily unavailable. "
                    "Please try again shortly."
                ) from exc
            log.error(
                "Gemini request rejected (%s): %s",
                status,
                details
            )
            raise AIUnavailable(
                f"Gemini request rejected ({status or type(exc).__name__}). "
                f"{details[:300]}"
            ) from exc
    raise AIUnavailable(
        "Gemini could not complete the request."
    )
# ---------- Response helpers ----------
def _candidate(response):
    candidates = getattr(response, "candidates", None) or []
    return candidates[0] if candidates else None
def _text(content):
    parts = getattr(content, "parts", None) or []
    return "".join(
        part.text
        for part in parts
        if getattr(part, "text", None)
        and not getattr(part, "thought", False)
    ).strip()
def _trim(history, limit=HISTORY_LIMIT):
    if len(history) <= limit:
        return
    def starts_turn(content):
        parts = getattr(content, "parts", None) or []
        return (
            getattr(content, "role", None) == "user"
            and any(getattr(p, "text", None) for p in parts)
            and not any(
                getattr(p, "function_response", None)
                for p in parts
            )
        )
    starts = [
        i for i, item in enumerate(history)
        if starts_turn(item)
    ]
    if not starts:
        return
    cutoff = next(
        (
            i for i in starts
            if len(history) - i <= limit
        ),
        starts[-1]
    )
    if cutoff:
        del history[:cutoff]
def dump_history(history):
    try:
        return [
            item.model_dump_json(exclude_none=True)
            for item in history
        ]
    except Exception:
        log.exception("Could not serialize conversation history")
        return []
def load_history(items):
    try:
        from google.genai import types
        return [
            types.Content.model_validate_json(x)
            for x in (items or [])
        ]
    except Exception:
        log.exception("Could not restore conversation history")
        return []
# ---------- Main conversation loop ----------
def chat(
    state,
    user_text,
    *,
    system_text,
    tool_specs,
    run_tool,
    mutating=(),
    signature=lambda value: "",
    nudge=None
):
    if not configured():
        raise AIUnavailable(
            "GEMINI_API_KEY is missing on the server."
        )
    if not isinstance(user_text, str) or not user_text.strip():
        raise AIUnavailable("Please enter a message.")
    from google.genai import types
    client = _get_client()
    history = state.setdefault("history", [])
    start = len(history)
    history.append(
        types.Content(
            role="user",
            parts=[types.Part(text=user_text.strip())]
        )
    )
    config = _config(
        types,
        system_text,
        tool_specs
    )
    completed_calls = {}
    call_count = 0
    nudged = False
    answer = ""
    try:
        for _step in range(MAX_STEPS):
            response = _generate(
                client,
                history,
                config
            )
            candidate = _candidate(response)
            if (
                candidate is None
                or candidate.content is None
                or not (candidate.content.parts or [])
            ):
                feedback = getattr(
                    response,
                    "prompt_feedback",
                    None
                )
                reason = getattr(
                    feedback,
                    "block_reason",
                    None
                )
                if reason is None and candidate is not None:
                    reason = getattr(
                        candidate,
                        "finish_reason",
                        None
                    )
                raise AIUnavailable(
                    f"Gemini returned no answer "
                    f"({reason or 'empty response'})."
                )
            history.append(candidate.content)
            calls = [
                part.function_call
                for part in candidate.content.parts
                if getattr(part, "function_call", None)
            ]
            if not calls:
                answer = _text(candidate.content)
                message = (
                    nudge(state)
                    if nudge and not nudged
                    else None
                )
                if message:
                    nudged = True
                    history.append(
                        types.Content(
                            role="user",
                            parts=[types.Part(text=message)]
                        )
                    )
                    continue
                break
            response_parts = []
            for function_call in calls:
                call_count += 1
                args = dict(function_call.args or {})
                key = (
                    function_call.name,
                    json.dumps(
                        args,
                        sort_keys=True,
                        default=str
                    ),
                    signature(state)
                    if function_call.name in mutating
                    else ""
                )
                if call_count > MAX_TOOL_CALLS:
                    result = {
                        "error": (
                            "Tool-call limit reached. "
                            "Summarise what is already known."
                        )
                    }
                elif key in completed_calls:
                    result = {
                        **_wrap(completed_calls[key]),
                        "duplicate_call_ignored": True
                    }
                else:
                    try:
                        result = run_tool(
                            function_call.name,
                            args
                        )
                    except Exception as exc:
                        log.exception(
                            "Tool '%s' failed",
                            function_call.name
                        )
                        result = {
                            "error": (
                                f"{type(exc).__name__}: "
                                f"{str(exc)[:500]}"
                            )
                        }
                    completed_calls[key] = result
                response_parts.append(
                    types.Part(
                        function_response=types.FunctionResponse(
                            id=getattr(
                                function_call,
                                "id",
                                None
                            ),
                            name=function_call.name,
                            response=_wrap(_shrink(result))
                        )
                    )
                )
            history.append(
                types.Content(
                    role="user",
                    parts=response_parts
                )
            )
        else:
            history.append(
                types.Content(
                    role="user",
                    parts=[
                        types.Part(
                            text=(
                                "[system] Tool/step limit reached. "
                                "Do not call more tools. Summarise "
                                "what you found and what remains unknown."
                            )
                        )
                    ]
                )
            )
            response = _generate(
                client,
                history,
                _config(
                    types,
                    system_text,
                    tool_specs,
                    no_tools=True
                )
            )
            candidate = _candidate(response)
            if candidate is not None and candidate.content is not None:
                history.append(candidate.content)
                answer = _text(candidate.content)
    except Exception:
        del history[start:]
        raise
    _trim(history)
    return answer or (
        "I couldn't complete that answer. "
        "Please rephrase or try again."
    )
