import json, statistics as st, collections
CAP = 20
pairs = json.load(open("spike/_release_post/pairs329.json"))
res = json.load(open("spike/_release_post/prod329.json"))
rows = []
mismatch = 0
for p in pairs:
    k = p["sig"] + "\t" + p["bid"] + "\t" + p["day"]
    r = res[k]
    if r["reports"] != p["count"]:
        mismatch += 1
    rows.append(dict(p, **r))
print("VALIDITY: reports != replay count on %d/%d pairs" % (mismatch, len(rows)))

card = [r["card"] for r in rows]
kept = [min(r["card"], CAP) for r in rows]
keptf = [r["kept"] for r in rows]
print("facet-len == min(card,20) on %d/%d" % (sum(1 for a, b in zip(keptf, kept) if a == b), len(rows)))

def summ(name, xs):
    xs = sorted(xs)
    n = len(xs)
    print("%-28s n=%d sum=%d mean=%.2f median=%.0f p75=%.0f p90=%.0f max=%d"
          % (name, n, sum(xs), st.mean(xs), st.median(xs), xs[int(.75*n)], xs[int(.90*n)], xs[-1]))
summ("distinct protos (card)", card)
summ("KEPT = min(card,20)", kept)
print("capped (card>20): %d/%d = %.1f%%" % (sum(1 for c in card if c > CAP), len(card),
                                            100*sum(1 for c in card if c > CAP)/len(card)))
print("card==1: %d, <=2: %d, <=5: %d" % (sum(1 for c in card if c == 1),
      sum(1 for c in card if c <= 2), sum(1 for c in card if c <= 5)))

RD = len(json.load(open("spike/_release_recon/plan_long.json")))
print("\nrun-days=%d  pairs=%d (%.2f/run-day)" % (RD, len(rows), len(rows)/RD))
for c in (1, 3, 5, 20, 50):
    s = sum(min(x, c) for x in card)
    print("  cap %2d -> %5d single-tick UUIDs over %d run-days = %5.2f/day (%.2f/pair)"
          % (c, s, RD, s/RD, s/len(rows)))

# burst vs steady state: which run-days are bursts?
byday = collections.defaultdict(list)
for r in rows:
    byday[r["day"]].append(min(r["card"], CAP))
tot = {d: sum(v) for d, v in byday.items()}
top = sorted(tot.items(), key=lambda x: -x[1])[:10]
print("\ntop run-days by single-tick UUIDs:", [(d, n, len(byday[d])) for d, n in top])
burst_days = {d for d, n in top[:4]}
bpairs = [r for r in rows if r["day"] in burst_days]
qpairs = [r for r in rows if r["day"] not in burst_days]
print("4 burst days: %d pairs, %d uuids | other %d days: %d pairs, %d uuids = %.2f uuids/day"
      % (len(bpairs), sum(min(r["card"], CAP) for r in bpairs), RD-4, len(qpairs),
         sum(min(r["card"], CAP) for r in qpairs),
         sum(min(r["card"], CAP) for r in qpairs)/(RD-4)))
print("kept/pair: burst %.2f  quiet %.2f"
      % (sum(min(r["card"], CAP) for r in bpairs)/len(bpairs),
         sum(min(r["card"], CAP) for r in qpairs)/len(qpairs)))
# the 30-pair subset resolve_03 used, for comparison
sub = json.load(open("spike/_release_recon/v12_pairs.json"))
subk = {(s["sig"], s["bid"]) for s in sub}
ov = [r for r in rows if (r["sig"], r["bid"]) in subk]
print("\noverlap with resolve_03's 30 pairs: %d ; their kept mean %.2f median %.0f"
      % (len(ov), st.mean([min(r["card"], CAP) for r in ov]) if ov else 0,
         st.median([min(r["card"], CAP) for r in ov]) if ov else 0))
json.dump(rows, open("spike/_release_post/rows329.json", "w"))
