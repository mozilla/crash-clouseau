"""Does the 72x/day SELECTOR cadence pick more (signature, buildid) pairs than the once-a-day
replay the 329-pair set came from? Re-run the real selector at 3-hourly ticks on two run-days,
using the same production functions sel_13_replay.py uses, with the per-build per-signature
facet refetched as-of each tick.  8 ticks x 3 builds x 2 days = 48 SuperSearch queries.
"""
import copy, json, os, datetime as dt
from libmozdata import socorro
from crashclouseau import config, utils
from crashclouseau import datacollector as dc

PROD, CHAN = "Firefox", "release"
TH = config.get_threshold("installs", PROD, CHAN)
FLOOR = config.get_spike("floor", PROD, CHAN)
RATIO = config.get_spike("ratio", PROD, CHAN)
MAT_AFTER, MAT_INST = dc.get_maturity_bar(PROD, CHAN)
NOUSER = dc.get_no_user_build_floor(PROD, CHAN)
PLAN = {p[0]: (p[1], p[2], p[3]) for p in json.load(open("spike/_release_recon/plan_long.json"))}
CACHE = "spike/_release_post/selcache"
os.makedirs(CACHE, exist_ok=True)

def fetch(bid, since, hi):
    key = os.path.join(CACHE, "%s_%s.json" % (bid, hi.replace(":", "")))
    if os.path.exists(key):
        return json.load(open(key))
    got = {}
    def h(j, d):
        if j["errors"]:
            raise Exception(j["errors"])
        d["r"] = j
    socorro.SuperSearch(params={
        "product": PROD, "release_channel": utils.get_search_channel(CHAN),
        "date": [">=" + since, "<" + hi], "build_id": bid,
        "_aggs.signature": ["build_id", "_cardinality.install_time"],
        "_results_number": 0, "_facets": "release_channel",
        "_facets_size": config.get_limit_facets()}, handler=h, handlerdata=got).wait()
    j = got["r"]
    rows = [[f["term"], f["facets"]["build_id"][0]["count"],
             f["facets"]["cardinality_install_time"]["value"]] for f in j["facets"]["signature"]]
    out = {"total": j["total"], "rows": rows}
    json.dump(out, open(key, "w"))
    return out

def picks_at(day, hi):
    bids, since, _ = PLAN[day]
    base = {}
    for b in bids:
        bd = utils.get_build_date(b)
        d0 = dt.datetime(bd.year, bd.month, bd.day)
        base.setdefault(d0, {"installs": {}, "bids": {}, "count": 0})
        base[d0]["bids"][bd] = 0
    data = {}
    for b in bids:
        bd = utils.get_build_date(b)
        d0 = dt.datetime(bd.year, bd.month, bd.day)
        for sgn, count, installs in fetch(b, since, hi)["rows"]:
            if sgn not in data:
                data[sgn] = copy.deepcopy(base)
            n = data[sgn]
            n[d0]["count"] += count
            n[d0]["bids"][bd] = count
            n[d0]["installs"][bd] = 1 if installs == 0 else installs
    dead = dc.find_no_user_days(data, NOUSER)
    D = dt.date.fromisoformat(day)
    out = set()
    for sgn, numbers in data.items():
        if dead:
            numbers = {d: v for d, v in numbers.items() if d not in dead}
            if not numbers:
                continue
        got, big, recs = utils.evaluate_days(numbers, 1, TH, FLOOR, RATIO, today=D,
                                            mature_after=MAT_AFTER, mature_installs=MAT_INST)
        for bid in got:
            out.add((sgn, bid.strftime("%Y%m%d%H%M%S")))
    return out

DAYS = os.getenv("DAYS", "2026-06-30,2026-08-20").split(",")
for day in DAYS:
    his = ["%sT%02d:00:00" % (day, h) for h in (3, 6, 9, 12, 15, 18, 21)] + [PLAN[day][2]]
    union, eod = set(), None
    per = []
    for hi in his:
        s = picks_at(day, hi)
        per.append((hi[-8:], len(s)))
        union |= s
        eod = s
    print("%s ticks %s" % (day, per))
    print("   end-of-day picks %d ; union over 8 ticks %d (+%d = x%.2f)"
          % (len(eod), len(union), len(union) - len(eod), len(union) / max(1, len(eod))), flush=True)
