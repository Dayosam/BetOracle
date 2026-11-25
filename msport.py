"""MSport adapter. MSport and SportyBet both use Sportradar ids (sr:match:..., marketId, outcomeId, specifier),
so legs move between them by copying ids. Nothing is matched by team name and nothing is invented.
Endpoints (captured with Proxyman):
  GET  https://www.msport.com/api/ng/orders/share/creator?shareCode={code}      read a booking code
  POST https://www.msport.com/api/ng/orders/real-sports/order/share            create a booking code"""
import re, httpx

BASE = "https://www.msport.com/api/ng"
HEAD = {"accept": "application/json", "user-agent": "Mozilla/5.0 (compatible; BetOracle/1.0)"}

def _http(method, path, **kw):
    r = httpx.request(method, BASE + path, headers={**HEAD, **({"content-type": "application/json"} if method == "POST" else {})}, timeout=25, **kw)
    if r.status_code in (401, 403):
        raise RuntimeError(f"MSport refused the request (HTTP {r.status_code}). It probably needs extra headers from the real app request. "
                           "Send the header NAMES of the captured request (not cookies or tokens).")
    if r.status_code == 429: raise RuntimeError("MSport is rate limiting this server. Wait a few minutes.")
    r.raise_for_status()
    j = r.json()
    if j.get("bizCode") != 10000:
        raise RuntimeError(f"MSport answered bizCode {j.get('bizCode')}: {j.get('message') or j.get('innerMsg')}")
    return j.get("data") or {}

def parse_slip(data):
    """Parse the 'bettableBetSlip' structure (taken from a real MSport response for code B2V6THQ)."""
    legs, suspended = [], []
    for s in data.get("bettableBetSlip") or []:
        ev, mk, oc = s["event"], s["market"], s["outcome"]
        spec = mk.get("specifiers") or ""
        leg = {"home": ev["homeTeam"], "away": ev["awayTeam"], "kickoff_ms": ev["startTime"],
               "market": mk["description"] + (f" ({spec})" if spec else ""), "pick": oc["description"], "odds": float(oc["odds"]),
               "ref": f'{ev["eventId"]}|{mk["id"]}|{spec}|{oc["id"]}', "sport": "loaded", "reason": "Loaded from an MSport booking code."}
        (legs if mk.get("status") == 0 and oc.get("isActive") == 1 else suspended).append(leg)
    original = data.get("originalSelectionCount") or len(legs) + len(suspended)
    return {"code": data.get("shareCode"), "legs": legs, "suspended": suspended,
            "not_listed_by_msport": max(0, original - len(legs) - len(suspended))}

def read_msport_code(share_code):
    code = (share_code or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9]{4,12}", code): raise ValueError("That does not look like an MSport booking code.")
    slip = parse_slip(_http("GET", "/orders/share/creator", params={"shareCode": code}))
    if not slip["legs"] and not slip["suspended"]:
        raise LookupError("MSport returned no selections for that code. It may be invalid or expired.")
    return slip

def create_msport_code(selections):
    """Create a real MSport booking code (stakes nothing). Each selection needs the ref 'eventId|marketId|specifier|outcomeId'."""
    items = []
    for s in selections:
        ref = s.get("ref") or ""
        if ref.count("|") != 3: raise ValueError(f"Selection {s.get('home')} v {s.get('away')} has no valid ref.")
        eid, mid, spec, oid = ref.split("|")
        if not eid.startswith("sr:match:"): raise ValueError(f"{eid} is not a Sportradar match id, so MSport cannot take it.")
        items.append({"eventId": eid, "marketId": mid, "outcomeId": oid, "specifier": spec})
    data = _http("POST", "/orders/real-sports/order/share", json={"selections": items})
    code = data.get("shareCode")
    if not code: raise RuntimeError("MSport did not return a booking code, so none was created.")
    out = {"code": code, "code_without_user": data.get("shareCodeWithoutUser"), "sent": len(items), "verified": False, "missing": []}
    try:  # the create response does not list the legs, so read the code back and compare
        back = read_msport_code(code)
        got = {l["ref"] for l in back["legs"] + back["suspended"]}
        out["missing"] = [f'{s["home"]} v {s["away"]}: {s["market"]} {s["pick"]}' for s in selections if s["ref"] not in got]
        out["verified"] = True
    except Exception as e:
        out["verify_error"] = f"{type(e).__name__}: {e}"
    return out

read_code = read_msport_code
