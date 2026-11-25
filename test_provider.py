"""Run on your own computer after setting MBC_API_KEY:
   python test_provider.py                                  -> checks the provider catalogue resolves your bookmakers (no credits used)
   python test_provider.py CODE SOURCE TARGET               -> real conversion, e.g.  python test_provider.py 9ABC123 bet9ja sportybet
A real conversion spends up to 4 provider credits and creates a real (non-staking) booking code."""
import sys, json, bookmakers as bm, managebetcodes as mbc, convert_router as router
print("Provider key set:", mbc.enabled())
for k, v in bm.BOOKMAKERS.items():
    print(f"  {v['label']:<14} route={bm.ROUTE[k]:<8} provider key={mbc.provider_key(k, v['label'])}")
if len(sys.argv) == 4:
    code, src, tgt = sys.argv[1:]
    out = router.convert_for_chat(code, src, tgt)
    out.pop("ticket", None)
    print(json.dumps(out, indent=1, default=str))
