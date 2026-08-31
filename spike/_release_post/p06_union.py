import json, os, statistics as st, datetime as dt
CAP = 20
TICKS = os.getenv("OUT", "spike/_release_post/ticks.json")
res = json.load(open(TICKS))
SAMPLE = json.load(open(os.getenv("SAMPLE", "spike/_release_post/sample.json")))
out = []
for p in SAMPLE:
    day = dt.date.fromisoformat(p["day"])
    hs = ["%sT%02d:%02d:00" % (day.isoformat(), m // 60, m % 60) for m in range(0, 24*60, 20)]
    sets = []
    for hi in hs + [p["upto"]]:
        k = "%s\t%s\t%s\t%s" % (p["sig"], p["bid"], p["day"], hi)
        v = res.get(k)
        if v is None or isinstance(v, dict):
            sets.append(None)
        else:
            sets.append(set(v))
    eod = sets[-1]
    ok = [s for s in sets if s is not None]
    u_all = set().union(*ok) if ok else set()
    last36 = [s for s in sets[36:] if s is not None]
    u_h = set().union(*last36) if last36 else set()
    # nonempty ticks = ticks where the pair had at least one report in range
    ne = sum(1 for s in sets[:-1] if s)
    out.append({"sig": p["sig"], "bid": p["bid"], "day": p["day"], "reports": p["reports"],
                "card": p["card"], "eod": len(eod), "u_all": len(u_all), "u_half": len(u_h),
                "ticks_nonempty": ne})
    print("%-10s r=%-6d card=%-5d eod=%-3d U72=%-4d (x%.2f)  Uhalf=%-4d  %s"
          % (p["day"], p["reports"], p["card"], len(eod), len(u_all),
             len(u_all)/max(1, len(eod)), len(u_h), p["sig"][:48]))
capped = [o for o in out if o["card"] > CAP]
unc = [o for o in out if o["card"] <= CAP]
def rep(name, xs):
    if not xs:
        print(name, "n=0"); return
    f = [x["u_all"]/max(1, x["eod"]) for x in xs]
    print("%-10s n=%-3d eod sum %4d -> U72 sum %4d  factor mean %.2f median %.2f max %.2f"
          % (name, len(xs), sum(x["eod"] for x in xs), sum(x["u_all"] for x in xs),
             st.mean(f), st.median(f), max(f)))
rep("CAPPED", capped)
rep("UNCAPPED", unc)
rep("ALL", out)
json.dump(out, open(TICKS.replace(".json", "_union.json"), "w"))
