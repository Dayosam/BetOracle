"""Run this on your own computer: python test_sportybet.py   (add --book to also create one non-staking test code)
It checks each SportyBet endpoint and prints what it got, so we can fix anything that changed."""
import sys, json, sportybet as sb
def step(name, fn):
    try: r = fn(); print("OK  ", name, "->", str(r)[:300]); return r
    except Exception as e: print("FAIL", name, "->", type(e).__name__, e); return None
ev = step("football events", lambda: sb._events("football")[:2])
step("darts events", lambda: len(sb._events("darts")))
if ev:
    m = step("event markets", lambda: sb.get_markets("football", ev[0]["home"], ev[0]["away"]))
    step("corner markets", lambda: sb.get_markets("football", ev[0]["home"], ev[0]["away"], "corner")["markets"][:3])
    if m and "--book" in sys.argv:
        step("booking (non-staking)", lambda: sb.book([m["markets"][0]]))
