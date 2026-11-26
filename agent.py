"""Gemini conversation engine (official google-genai SDK).
Handles: function declarations, multiple tool calls per reply, correct FunctionResponse parts (matching id and name), thought signatures
(the model's full reply is stored untouched), duplicate-call prevention, loop limits, retries for rate limits and timeouts, and history trimming."""
import os, json, time, logging

log = logging.getLogger("betoracle.agent")
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
MAX_STEPS = int(os.getenv("AI_MAX_STEPS", "14"))
MAX_TOOL_CALLS = int(os.getenv("AI_MAX_TOOL_CALLS", "60"))
HISTORY_LIMIT = int(os.getenv("AI_HISTORY_LIMIT", "40"))
RESULT_LIMIT = 10000
THINKING = os.getenv("GEMINI_THINKING_LEVEL", "").lower()   # optional: low | medium | high (3.x models reject 'minimal')

class AIUnavailable(Exception):
    """A problem the user should be told about in plain words."""

def configured(): return bool(os.getenv("GEMINI_API_KEY"))

_client = None
def _get_client():
    global _client
    if _client is None:
        from google import genai
        from google.genai import types
        _client = genai.Client(api_key=os.environ["GEMINI_API_KEY"], http_options=types.HttpOptions(timeout=int(os.getenv("GEMINI_TIMEOUT_MS", "60000"))))
    return _client

# ---------- tool declarations ----------
def _clean(s):
    """JSON-schema -> the subset Gemini accepts (single types, no unions, nothing exotic)."""
    if not isinstance(s, dict): return {"type": "string"}
    t = s.get("type", "string")
    if isinstance(t, list): t = next((x for x in t if x != "null"), "string")
    out = {"type": str(t).lower()}
    if s.get("description"): out["description"] = s["description"]
    if s.get("enum"): out["enum"] = [str(x) for x in s["enum"]]
    if out["type"] == "object":
        out["properties"] = {k: _clean(v) for k, v in (s.get("properties") or {}).items()}
        if s.get("required"): out["required"] = [r for r in s["required"] if r in out["properties"]]
    if out["type"] == "array": out["items"] = _clean(s.get("items") or {"type": "string"})
    return out

def declarations(tool_specs):
    out = []
    for t in tool_specs:
        params = _clean(t.get("input_schema") or {"type": "object", "properties": {}})
        d = {"name": t["name"], "description": t["description"]}
        if params.get("properties"): d["parameters"] = params      # tools with no arguments declare none
        out.append(d)
    return out

def _config(types, system_text, tool_specs, no_tools=False):
    kw = dict(system_instruction=system_text, max_output_tokens=8192,
              tools=[types.Tool(function_declarations=declarations(tool_specs))],
              automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
    if no_tools: kw["tool_config"] = types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="NONE"))
    if THINKING in ("low", "medium", "high"): kw["thinking_config"] = types.ThinkingConfig(thinking_level=THINKING)
    return types.GenerateContentConfig(**kw)

# ---------- helpers ----------
def _shrink(obj, limit=RESULT_LIMIT):
    """Make a tool result safe to send: plain JSON, and small enough not to flood the model."""
    obj = json.loads(json.dumps(obj, default=str))
    def size(o): return len(json.dumps(o, default=str))
    if size(obj) <= limit: return obj
    def cut(o, n):
        if isinstance(o, list): return [cut(x, n) for x in o[:n]] + ([f"... {len(o) - n} more items omitted"] if len(o) > n else [])
        if isinstance(o, dict): return {k: cut(v, n) for k, v in o.items()}
        return o[:500] + "..." if isinstance(o, str) and len(o) > 500 else o
    for n in (40, 20, 10, 5):
        small = cut(obj, n)
        if size(small) <= limit: return small
    return {"note": "Result too large to show; ask for a narrower query.", "preview": json.dumps(obj, default=str)[:2000]}

def _wrap(x): return x if isinstance(x, dict) else {"result": x}

def _status(e):
    for k in ("code", "status_code", "status"):
        v = getattr(e, k, None)
        if isinstance(v, int): return v
    return None

def _generate(client, contents, config):
    delay = 1.5
    for attempt in range(4):
        try:
            return client.models.generate_content(model=MODEL, contents=contents, config=config)
        except Exception as e:
            code, name = _status(e), type(e).__name__
            if code in (401, 403): raise AIUnavailable("Gemini rejected the API key. Check GEMINI_API_KEY on the server.") from e
            if code == 404: raise AIUnavailable(f"The Gemini model '{MODEL}' was not found. Check GEMINI_MODEL.") from e
            retry = code in (429, 500, 502, 503, 504) or "Timeout" in name or "timed out" in str(e).lower()
            if retry and attempt < 3:
                log.warning("Gemini call failed (%s %s), retrying in %.1fs", code, name, delay)
                time.sleep(delay); delay *= 2.5; continue
            if code == 429: raise AIUnavailable("Gemini is rate limiting this server right now. Please wait a minute and try again.") from e
            if retry: raise AIUnavailable("Gemini is busy or took too long. Please try again.") from e
            log.error("Gemini request rejected: %s %s", name, str(e)[:300])
            raise AIUnavailable(f"The AI request was rejected ({code or name}). {str(e)[:160]}") from e

def _candidate(resp):
    c = getattr(resp, "candidates", None) or []
    return c[0] if c else None

def _text(content):
    return "".join(p.text for p in (content.parts or []) if getattr(p, "text", None) and not getattr(p, "thought", False)).strip()

def _trim(history, limit=HISTORY_LIMIT):
    """Drop the oldest turns, always cutting at the start of a user turn so no tool call is separated from its result."""
    if len(history) <= limit: return
    def starts_turn(c):
        ps = c.parts or []
        return c.role == "user" and any(getattr(p, "text", None) for p in ps) and not any(getattr(p, "function_response", None) for p in ps)
    starts = [i for i, c in enumerate(history) if starts_turn(c)]
    for i in starts:
        if len(history) - i <= limit: del history[:i]; return
    if starts: del history[:starts[-1]]

def dump_history(history):
    try: return [c.model_dump_json(exclude_none=True) for c in history]
    except Exception: return []

def load_history(items):
    try:
        from google.genai import types
        return [types.Content.model_validate_json(x) for x in items or []]
    except Exception:
        return []

# ---------- the conversation loop ----------
def chat(state, user_text, *, system_text, tool_specs, run_tool, mutating=(), signature=lambda s: "", nudge=None):
    """One user message -> final reply text. `run_tool(name, args)` executes a tool and returns a JSON-able result."""
    if not configured(): raise AIUnavailable("The AI is not set up yet: GEMINI_API_KEY is missing on the server.")
    from google.genai import types
    client = _get_client()
    history = state.setdefault("history", [])
    start = len(history)
    history.append(types.Content(role="user", parts=[types.Part(text=user_text)]))
    config = _config(types, system_text, tool_specs)
    done, n_calls, nudged, text = {}, 0, False, ""
    try:
        for _ in range(MAX_STEPS):
            resp = _generate(client, history, config)
            cand = _candidate(resp)
            if cand is None or cand.content is None or not (cand.content.parts or []):
                reason = getattr(getattr(resp, "prompt_feedback", None), "block_reason", None) or getattr(cand, "finish_reason", None)
                raise AIUnavailable(f"The AI returned no answer ({reason or 'empty response'}). Please rephrase and try again.")
            history.append(cand.content)
            calls = [p.function_call for p in cand.content.parts if getattr(p, "function_call", None)]
            if not calls:
                text = _text(cand.content)
                msg = nudge(state) if (nudge and not nudged) else None
                if msg:
                    nudged = True
                    history.append(types.Content(role="user", parts=[types.Part(text=msg)]))
                    continue
                break
            parts = []
            for fc in calls:                       # one response part per call, same order, same id and name
                n_calls += 1
                args = dict(fc.args or {})
                key = (fc.name, json.dumps(args, sort_keys=True, default=str), signature(state) if fc.name in mutating else "")
                if n_calls > MAX_TOOL_CALLS: result = {"error": "Tool-call limit for this message reached. Summarise what you have."}
                elif key in done: result = {**_wrap(done[key]), "duplicate_call_ignored": True}
                else:
                    try: result = run_tool(fc.name, args)
                    except Exception as e:
                        log.exception("tool %s crashed", fc.name)
                        result = {"error": f"{type(e).__name__}: {e}"}
                    done[key] = result
                parts.append(types.Part(function_response=types.FunctionResponse(id=getattr(fc, "id", None), name=fc.name, response=_wrap(_shrink(result)))))
            history.append(types.Content(role="user", parts=parts))
        else:                                       # step limit: stop the loop, ask for a summary with tools switched off
            history.append(types.Content(role="user", parts=[types.Part(text="[system] Step limit reached. Do not call any more tools. Summarise what you found, what is still missing, and what the user should do next.")]))
            resp = _generate(client, history, _config(types, system_text, tool_specs, no_tools=True))
            cand = _candidate(resp)
            if cand is not None and cand.content is not None:
                history.append(cand.content); text = _text(cand.content)
    except Exception:
        del history[start:]                         # never leave a half-finished turn (an unanswered tool call breaks the next message)
        raise
    _trim(history)
    return text or "I couldn't put an answer together. Please rephrase or try again."
