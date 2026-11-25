"""Run on your own computer: python test_msport.py CODE [--create]
Reads a real MSport code. With --create it books the same legs again as a NEW non-staking MSport code and checks the legs came through."""
import sys, json, msport
code = sys.argv[1]
slip = msport.read_msport_code(code)
print("READ OK:", len(slip["legs"]), "bookable,", len(slip["suspended"]), "suspended,", slip["not_listed_by_msport"], "not listed by MSport")
for l in slip["legs"][:5]: print("  ", l["home"], "v", l["away"], "|", l["market"], l["pick"], l["odds"], "|", l["ref"])
if "--create" in sys.argv:
    r = msport.create_msport_code(slip["legs"])
    print("CREATE:", json.dumps(r, indent=1))
