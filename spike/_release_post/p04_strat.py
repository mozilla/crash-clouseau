import json, statistics as st, collections
CAP = 20
rows = json.load(open("spike/_release_post/rows329.json"))
capped = [r for r in rows if r["card"] > CAP]
unc = [r for r in rows if r["card"] <= CAP]
print("capped %d  uncapped %d" % (len(capped), len(unc)))
print("uncapped: kept sum %d (mean %.2f)" % (sum(r["card"] for r in unc), st.mean([r["card"] for r in unc])))
print("capped: reports median %d p90 %d max %d"
      % (st.median([r["reports"] for r in capped]),
         sorted(r["reports"] for r in capped)[int(.9*len(capped))],
         max(r["reports"] for r in capped)))
tw = [r["protos"][19][1] for r in capped if len(r["protos"]) >= 20]
tw.sort()
print("count of the 20th (last kept) proto, capped pairs: median %d  p25 %d p75 %d min %d max %d"
      % (st.median(tw), tw[len(tw)//4], tw[3*len(tw)//4], tw[0], tw[-1]))
print("  20th-proto count == 1 on %d/%d capped pairs (%.0f%%) -> the cut is inside a tie group"
      % (sum(1 for x in tw if x == 1), len(tw), 100*sum(1 for x in tw if x == 1)/len(tw)))
print("  20th-proto count <= 2 on %d/%d" % (sum(1 for x in tw if x <= 2), len(tw)))
# signatures on multiple buildids -> cross-build proto overlap (free dossiers, paid uuid rows)
bysig = collections.defaultdict(list)
for r in rows:
    bysig[r["sig"]].append(r)
multi = {s: v for s, v in bysig.items() if len(v) > 1}
print("\ndistinct signatures %d; on >1 buildid: %d (covering %d pairs)"
      % (len(bysig), len(multi), sum(len(v) for v in multi.values())))
ovl_num = ovl_den = 0
for s, v in multi.items():
    v = sorted(v, key=lambda r: r["bid"])
    seen = set()
    for r in v:
        terms = {t for t, _ in r["protos"]}
        ovl_den += len(terms)
        ovl_num += len(terms & seen)
        seen |= terms
print("cross-build duplicate protos among multi-build signatures: %d of %d kept (%.1f%%)"
      % (ovl_num, ovl_den, 100*ovl_num/max(1, ovl_den)))
allkept = sum(len(r["protos"]) for r in rows)
print("as a share of ALL 329 pairs' kept protos: %d/%d = %.1f%%" % (ovl_num, allkept, 100*ovl_num/allkept))
