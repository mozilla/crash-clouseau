"""Replay the REAL selector on RELEASE, one run per day, using the production functions:
utils.evaluate_days, datacollector.find_no_user_days/get_maturity_bar/get_no_user_build_floor,
config.get_threshold/get_spike. Mirrors get_new_signatures' assembly of `base`/`data`.
"""
import copy, json, os, sys
from collections import Counter, defaultdict
from datetime import datetime
from crashclouseau import config, utils
from crashclouseau import datacollector as dc

CACHE = "spike/_release_recon/cache"
PLAN = json.load(open(os.getenv("PLAN", "spike/_release_recon/plan.json")))
BUILDS = json.load(open("spike/_release_recon/release_builds.json"))
VER = {b[0]: b[1] for b in BUILDS}

PROD, CHAN = "Firefox", "release"
THRESHOLD = config.get_threshold("installs", PROD, CHAN)
FLOOR = config.get_spike("floor", PROD, CHAN)
RATIO = config.get_spike("ratio", PROD, CHAN)
MAT_AFTER, MAT_INST = dc.get_maturity_bar(PROD, CHAN)
NOUSER = dc.get_no_user_build_floor(PROD, CHAN)
DROP = os.getenv("NO_DROP") != "1"
THRESHOLD = int(os.getenv("THRESHOLD", THRESHOLD))
FLOOR = int(os.getenv("FLOOR", FLOOR))
print(f"threshold(installs)={THRESHOLD} floor={FLOOR} ratio={RATIO} shift=1 "
      f"maturity={MAT_AFTER, MAT_INST} no_user_floor={NOUSER} apply_drop={DROP}")

def load(bid, upto):
    return json.load(open(os.path.join(CACHE, f"{bid}_{upto}.json")))

def build_data(bids, upto):
    base = {}
    for b in bids:
        bd = utils.get_build_date(b)
        day = datetime(bd.year, bd.month, bd.day)
        base.setdefault(day, {"installs": {}, "bids": {}, "count": 0})
        base[day]["bids"][bd] = 0
    data = {}
    for b in bids:
        bd = utils.get_build_date(b)
        day = datetime(bd.year, bd.month, bd.day)
        for sgn, count, installs in load(b, upto)["rows"]:
            if sgn not in data:
                data[sgn] = copy.deepcopy(base)
            n = data[sgn]
            n[day]["count"] += count
            n[day]["bids"][bd] = count
            n[day]["installs"][bd] = 1 if installs == 0 else installs
    return data

all_pairs, first_pick, per_day = set(), {}, {}
outcomes = Counter()
fromzero = 0
picked_installs = []
pair_reason = {}
for Dstr, bids, since, upto in PLAN:
    if not bids:
        per_day[Dstr] = {"window": [], "picks": 0, "new": 0}
        continue
    D = datetime.strptime(Dstr, "%Y-%m-%d").date()
    data = build_data(bids, upto)
    dead = dc.find_no_user_days(data, NOUSER) if DROP else set()
    picks = []
    for sgn, numbers in data.items():
        if dead:
            numbers = {d: v for d, v in numbers.items() if d not in dead}
            if not numbers:
                continue
        got, big, records = utils.evaluate_days(
            numbers, 1, THRESHOLD, FLOOR, RATIO, today=D,
            mature_after=MAT_AFTER, mature_installs=MAT_INST)
        for r in records:
            outcomes[r["outcome"]] += 1
        for bid, cnt in got.items():
            picks.append((sgn, bid.strftime("%Y%m%d%H%M%S"), cnt))
            key = (sgn, bid.strftime("%Y%m%d%H%M%S"))
            all_pairs.add(key)
            if key not in first_pick:
                first_pick[key] = Dstr
                rec = [r for r in records if r["picked"] == bid][0]
                base = max(rec["baseline"]) if rec["baseline"] else 0
                pair_reason[key] = ("from_zero" if base == 0 else "floor_ratio", cnt,
                                    rec["installs"].get(bid, 0), base)
    new = [p for p in picks if first_pick[(p[0], p[1])] == Dstr]
    per_day[Dstr] = {"window": bids, "dead": sorted(d.strftime("%Y-%m-%d") for d in dead),
                     "picks": len(picks), "new": len(new),
                     "new_list": [[p[0], p[1], p[2]] for p in new], "pick_list": [[p[0], p[1]] for p in picks]}
    print(f"{Dstr} win=[{','.join(VER[b] for b in bids)}] dead={len(dead)} "
          f"picks={len(picks):5d} NEW={len(new):5d}", flush=True)

days = len(PLAN)
tot_new = sum(v["new"] for v in per_day.values())
print()
print(f"run-days {days}; distinct (signature,buildid) pairs {len(all_pairs)}; "
      f"new/day {tot_new/days:.2f}; distinct signatures {len({s for s,_ in all_pairs})}")
print("max picks in one run:", max(v["picks"] for v in per_day.values()),
      " max NEW in one run:", max(v["new"] for v in per_day.values()))
fz = sum(1 for v in pair_reason.values() if v[0] == "from_zero")
print(f"first-pick branch: from_zero {fz} ({100*fz/max(1,len(pair_reason)):.1f}%), "
      f"floor_ratio {len(pair_reason)-fz}")
print("selection-record outcomes:", dict(outcomes))
json.dump({"per_day": per_day, "pairs": sorted(all_pairs),
           "reason": {f"{k[0]}\t{k[1]}": v for k, v in pair_reason.items()}},
          open(os.getenv("OUT", "spike/_release_recon/replay_release.json"), "w"))
