"""Adapter for the Manage Bet Codes API (managebetcodes.app), a third-party commercial service that reads, converts and books codes
for dozens of bookmakers. Written from its public documentation: nothing here has been run against the live service.
Needs MBC_API_KEY. Costs credits: view 1, convert 2 per target (failed calls are not charged, per its docs)."""
import os, re, uuid, httpx
from cache import cached

BASE = "https://api.managebetcodes.app/v1"
# Keys printed in the provider's own documentation table. Everything else is looked up in its live catalogue.
DOCUMENTED = {"sportybet": "sportybet", "bet9ja": "bet9ja", "msport": "msport", "betking": "betking", "betway": "betway",
              "1xbet": "onexbet", "22bet": "22bet", "paripesa": "paripesa", "melbet": "melbet", "afropari": "afropari_ng"}

class ProviderError(Exception): pass

def enabled(): return bool(os.getenv("MBC_API_KEY"))
def _norm(s): return re.sub(r"[^a-z0-9]", "", str(s).lower())

def catalogue():
    def call():
        r = httpx.get(BASE + "/bookmakers", timeout=20)
        r.raise_for_status(); j = r.json()
        return j if isinstance(j, list) else next((v for v in j.values() if isinstance(v, list)), [])
    return cached("mbc:catalogue", 86400, call)

def provider_key(canon_key, label=None):
    """The provider's key for one of our bookmakers, or None if the provider does not offer it."""
    if canon_key in DOCUMENTED: return DOCUMENTED[canon_key]
    try:
        want = {_norm(canon_key), _norm(label or canon_key)}
        for it in catalogue():
            if isinstance(it, dict) and want & {_norm(v) for v in [it.get("key"), it.get("name"), *(it.get("aliases") or [])] if v}:
                return it.get("key")
    except Exception:
        pass
    return None

def _call(method, path, **kw):
    key = os.getenv("MBC_API_KEY")
    if not key: raise ProviderError("The conversion provider key (MBC_API_KEY) is not set.")
    h = {"X-API-Key": key, "accept": "application/json"}
    if method == "POST": h.update({"Idempotency-Key": uuid.uuid4().hex, "content-type": "application/json"})
    r = httpx.request(method, BASE + path, headers=h, timeout=90, **kw)
    try: j = r.json()
    except Exception: j = {}
    if r.status_code >= 400:
        e = j.get("error") if isinstance(j.get("error"), dict) else {}
        code, msg = e.get("code") or f"http_{r.status_code}", e.get("message") or j.get("message") or ""
        if code == "slip_not_found" or r.status_code == 404: raise LookupError("The provider found no slip for that code.")
        if r.status_code == 401: raise ProviderError("The conversion provider rejected the API key.")
        if r.status_code in (402, 403): raise ProviderError(f"The conversion provider refused the call ({code}). Check the plan or credits. {msg}")
        if r.status_code == 429: raise ProviderError("The conversion provider is rate limiting. Wait a minute and retry.")
        raise ProviderError(f"Provider error {code}: {msg}")
    return j

def _split(match):
    parts = re.split(r"\s+(?:vs\.?|v)\s+", match or "", maxsplit=1, flags=re.I)
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else ((match or "").strip(), "")

def view(code, pkey, label="the bookmaker"):
    """Read a code. Legs carry no ids (the provider returns display text), so they cannot be re-booked directly."""
    j = _call("GET", "/view", params={"code": code, "bookmaker": pkey})
    legs = []
    for s in j.get("selections") or []:
        home, away = _split(s.get("match"))
        try: odds = float(s.get("odds"))
        except (TypeError, ValueError): odds = 0.0
        bookable = (s.get("section") in (None, "", "upcoming")) and (s.get("status") in (None, "", "PENDING"))
        legs.append({"home": home, "away": away, "market": s.get("market") or "", "pick": s.get("selection") or "", "odds": odds, "ref": None,
                     "sport": s.get("sport") or "loaded", "reason": f"Loaded from a {label} booking code.", "bookable": bookable,
                     "start": s.get("start_time")})
    if not legs: raise LookupError("The provider returned a slip with no selections.")
    return {"legs": legs, "total_odds": j.get("total_odds")}

def convert(code, from_key, to_key):
    j = _call("POST", "/convert", json={"code": code, "from": from_key, "to": to_key})
    target = j.get("target") or {}
    if not j.get("success") or not target.get("code"):
        raise ProviderError(f"The provider created no code. Failures: {j.get('failures') or j.get('error') or 'none listed'}")
    return {"code": target["code"], "counts": j.get("counts") or {}, "partial": bool(j.get("partial")), "verified": j.get("verified"),
            "odds": j.get("odds"), "failures": j.get("failures") or []}
