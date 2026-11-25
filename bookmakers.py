"""Nigerian bookmakers BetOracle knows, how each is reached, and exactly what is implemented and tested.
Nothing is marked tested unless it has run against the live service."""
import re

BOOKMAKERS = {
    "sportybet": {"label": "SportyBet"}, "msport": {"label": "MSport"}, "bet9ja": {"label": "Bet9ja"}, "betking": {"label": "BetKing"},
    "nairabet": {"label": "NairaBet"}, "betway": {"label": "Betway"}, "livescorebet": {"label": "LiveScore Bet"},
    "surebet247": {"label": "Surebet247"}, "betnaija": {"label": "BetNaija"}, "accessbet": {"label": "AccessBet"},
    "1xbet": {"label": "1xBet", "family": "1x"}, "paripesa": {"label": "Paripesa", "family": "1x"}, "melbet": {"label": "Melbet", "family": "1x"},
    "22bet": {"label": "22Bet", "family": "1x"}, "afropari": {"label": "Afropari", "family": "1x"}, "betpawa": {"label": "Betpawa"},
}
# direct = BetOracle talks to the bookmaker itself; provider = through the Manage Bet Codes API; none = no route found
ROUTE = {k: "provider" for k in BOOKMAKERS}
ROUTE.update(sportybet="direct", msport="direct", nairabet="none", surebet247="none", betnaija="none")
NO_ROUTE = {k for k, v in ROUTE.items() if v == "none"}
SEARCH_IMPL, MARKETS_IMPL = {"sportybet"}, {"sportybet"}   # fixture search / market retrieval built (direct only)

def caps(key):
    r = ROUTE[key]
    yn = lambda b: "yes" if b else "no"
    return {"route": r, "read_implemented": yn(r != "none"), "read_tested": "no", "search_implemented": yn(key in SEARCH_IMPL),
            "markets_implemented": yn(key in MARKETS_IMPL), "create_implemented": yn(r != "none"), "create_tested": "no", "full_conversion_tested": "no"}

ALIASES = {"sporty": "sportybet", "b9ja": "bet9ja", "bet9": "bet9ja", "livescore": "livescorebet", "surebet": "surebet247", "naija": "betnaija",
           "1x": "1xbet", "onexbet": "1xbet", "pawa": "betpawa", "mel": "melbet"}

def canon(text):
    """Turn what the user typed ('Sporty', 'bet 9ja', 'convert to BetNaija') into a bookmaker key, or None."""
    n = "".join(c for c in (text or "").lower() if c.isalnum())
    if n in BOOKMAKERS: return n
    if n in ALIASES: return ALIASES[n]
    for key in sorted(list(BOOKMAKERS) + list(ALIASES), key=len, reverse=True):
        if key in n: return ALIASES.get(key, key)
    return None

def label(key): return BOOKMAKERS[key]["label"]

def candidates(code):
    """Bookmakers whose code format fits this code (formats are the provider's published ones). Several can fit; the caller tries each."""
    c, out = (code or "").strip(), []
    alnum, has_both = c.isalnum(), bool(re.search(r"[A-Za-z]", c)) and bool(re.search(r"\d", c))
    if re.fullmatch(r"B\d[A-Za-z0-9]{5}", c, re.I): out.append("msport")
    if re.fullmatch(r"\d[A-Za-z0-9]{6}", c): out.append("bet9ja")
    if re.fullmatch(r"BW[A-Za-z0-9]{6,}", c, re.I): out.append("betway")
    if re.fullmatch(r"[A-Za-z]{3}-?\d{3}-?\d|[A-Za-z]{3}\d{4}", c): out.append("accessbet")
    if len(c) == 6 and alnum: out += ["sportybet", "betking"]
    if len(c) == 7 and alnum: out.append("betpawa")
    if len(c) == 5 and alnum and has_both: out += ["1xbet", "paripesa", "melbet", "22bet", "afropari", "livescorebet"]
    return list(dict.fromkeys(out))
