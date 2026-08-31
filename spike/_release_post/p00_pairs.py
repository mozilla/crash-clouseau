"""Build the 329-pair set with, per pair, its FIRST-PICK run-day (deployed cadence) and the
production `search_date` lower bound for that day = oldest build-DAY of the 3-build window.

Source: spike/_release_recon/replay_release_long.json (133 run-days, 2026-04-20..08-30,
produced by sel_13_replay.py with PLAN=plan_long.json) + plan_long.json for since/upto.
"""
import json, collections

REP = json.load(open("spike/_release_recon/replay_release_long.json"))
PLAN = json.load(open("spike/_release_recon/plan_long.json"))
SINCE = {p[0]: p[2] for p in PLAN}
UPTO = {p[0]: p[3] for p in PLAN}
WIN = {p[0]: p[1] for p in PLAN}

pairs = []
seen = set()
picks_by_pair = collections.defaultdict(list)   # every run-day the pair is picked
for day in sorted(REP["per_day"]):
    v = REP["per_day"][day]
    for sig, bid, cnt in v.get("new_list", []):
        key = (sig, bid)
        assert key not in seen, key
        seen.add(key)
        pairs.append({"sig": sig, "bid": bid, "day": day, "count": cnt,
                      "since": SINCE[day], "upto": UPTO[day],
                      "window": WIN[day],
                      "reason": REP["reason"][sig + "\t" + bid][0]})

# how many days does each pair stay picked? (needs re-deriving picks per day: per_day has
# only the count, not the list -- so use `new_list` for first pick and note the gap.)
print("run-days:", len(REP["per_day"]), "distinct pairs:", len(REP["pairs"]),
      "first-pick rows:", len(pairs))
assert len(pairs) == len(REP["pairs"]), (len(pairs), len(REP["pairs"]))
print("per-day NEW: total %d over %d days = %.2f/day"
      % (sum(v["new"] for v in REP["per_day"].values()), len(REP["per_day"]),
         sum(v["new"] for v in REP["per_day"].values()) / len(REP["per_day"])))
print("per-day PICKS (re-queried every tick): total %d, mean %.2f, max %d"
      % (sum(v["picks"] for v in REP["per_day"].values()),
         sum(v["picks"] for v in REP["per_day"].values()) / len(REP["per_day"]),
         max(v["picks"] for v in REP["per_day"].values())))
by_day = collections.Counter(p["day"] for p in pairs)
print("top NEW-days:", by_day.most_common(8))
print("reason:", collections.Counter(p["reason"] for p in pairs))
# span of the as-of date windows
spans = [(p["since"], p["upto"]) for p in pairs]
import datetime as dt
lens = [(dt.date.fromisoformat(u) - dt.date.fromisoformat(s)).days for s, u in spans]
lens.sort()
print("date-range length (days): min %d median %d max %d" % (lens[0], lens[len(lens)//2], lens[-1]))
json.dump(pairs, open("spike/_release_post/pairs329.json", "w"))
