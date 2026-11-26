"""Test-only stand-ins. The real Google SDK is NOT used by these tests (it is replaced here), so they check BetOracle's own logic:
tool declarations, the conversation loop, validation, booking rules. They cannot prove the real Gemini API accepts the requests:
run test_live_gemini.py with your key for that."""
import sys, types as pt

def install_missing():
    def mod(n, **kw):
        m = pt.ModuleType(n); m.__dict__.update(kw); sys.modules[n] = m
    class FA:
        def post(s, *a, **k): return lambda f: f
        get = post
        def mount(s, *a, **k): pass
    for name, kw in (("httpx", {}), ("dotenv", {"load_dotenv": lambda *a, **k: None}), ("fastapi", {"FastAPI": FA}),
                     ("fastapi.staticfiles", {"StaticFiles": lambda **k: None}), ("pydantic", {"BaseModel": object})):
        try: __import__(name)
        except ImportError: mod(name, **kw)
    if "fastapi" in sys.modules and not hasattr(sys.modules["fastapi"], "FastAPI"): pass

class _Obj:
    def __init__(self, **kw): self.__dict__.update(kw)

def install_genai():
    g, genai, t = pt.ModuleType("google"), pt.ModuleType("google.genai"), pt.ModuleType("google.genai.types")
    class Part(_Obj):
        def __init__(self, text=None, function_call=None, function_response=None, thought=None, thought_signature=None):
            super().__init__(text=text, function_call=function_call, function_response=function_response, thought=thought, thought_signature=thought_signature)
    class Content(_Obj):
        def __init__(self, role=None, parts=None): super().__init__(role=role, parts=list(parts or []))
    t.Part, t.Content = Part, Content
    for n in ("FunctionCall", "FunctionResponse", "Tool", "GenerateContentConfig", "AutomaticFunctionCallingConfig", "ToolConfig",
              "FunctionCallingConfig", "ThinkingConfig", "HttpOptions"): setattr(t, n, type(n, (_Obj,), {}))
    class Client:
        script, calls = [], []
        def __init__(self, **kw): self.models = self
        def generate_content(self, model, contents, config):
            Client.calls.append({"contents": list(contents), "config": config, "model": model})
            r = Client.script.pop(0)(list(contents), config)
            if isinstance(r, Exception): raise r
            return r
    genai.Client, genai.types, g.genai = Client, t, genai
    sys.modules.update({"google": g, "google.genai": genai, "google.genai.types": t})
    return Client, t

class Resp(_Obj): pass
class Cand(_Obj): pass
