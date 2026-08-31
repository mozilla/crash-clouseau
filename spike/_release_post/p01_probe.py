"""Instrument probe before the 329-query run:
 1. does the PRODUCTION query shape work with a timestamp upper bound (for the 72-tick sim)?
 2. is April 2026 release data still in Socorro (retention)?
 3. does the cached per-signature fetch from sel_12_fetch.py still reproduce today?
"""
import json
from libmozdata import socorro
from crashclouseau import config, utils

CAP = config.get_threshold("protos", "Firefox", "release")
LIM = config.get_limit_facets()
print("cap thresholds.protos.Firefox.release =", CAP, " facets_limit =", LIM,
      " search_channel(release) =", utils.get_search_channel("release"))

def q(params):
    got = {}
    def h(j, d):
        d["r"] = j
    socorro.SuperSearch(params=params, handler=h, handlerdata=got).wait()
    return got["r"]

def prodq(sig, bid, lo, hi):
    """EXACT production shape (get_proto_big), one (signature, buildid), as-of `hi`."""
    return q({
        "product": "Firefox",
        "release_channel": utils.get_search_channel("release"),
        "date": [">=" + lo, "<" + hi],
        "build_id": bid,
        "signature": "=" + sig,
        "_aggs.proto_signature": "uuid",
        "_results_number": 0,
        "_facets": ["_cardinality.proto_signature", "_cardinality.install_time"],
        "_facets_size": CAP,
    })

pairs = json.load(open("spike/_release_post/pairs329.json"))
# oldest and newest pair
for p in (pairs[0], pairs[1], pairs[-1]):
    j = prodq(p["sig"], p["bid"], p["since"], p["upto"])
    print("\n%s %s bid=%s  date=[>=%s,<%s]" % (p["day"], p["reason"], p["bid"], p["since"], p["upto"]))
    print("   errors=%r total=%d card_proto=%s kept=%d  sig=%s"
          % (j["errors"], j["total"], j["facets"]["cardinality_proto_signature"]["value"],
             len(j["facets"]["proto_signature"]), p["sig"][:60]))
    print("   replay said count=%d" % p["count"])

# 1. timestamp upper bound
p = pairs[-1]
for hi in (p["day"] + "T06:00:00", p["day"] + "T12:00:00", p["upto"]):
    j = prodq(p["sig"], p["bid"], p["since"], hi)
    print("tick-bound <%s -> total=%d kept=%d errors=%r"
          % (hi, j["total"], len(j["facets"]["proto_signature"]), j["errors"]))

# 3. reproduce a cached per-signature fetch (sel_12_fetch shape, whole build)
import os
bid, upto = "20260415192539", "2026-04-21"
key = "spike/_release_recon/cache/%s_%s.json" % (bid, upto)
if os.path.exists(key):
    old = json.load(open(key))
    j = q({"product": "Firefox", "release_channel": "release",
           "date": [">=2026-03-18", "<" + upto], "build_id": bid,
           "_aggs.signature": ["build_id", "_cardinality.install_time"],
           "_results_number": 0, "_facets": "release_channel", "_facets_size": LIM})
    print("\nRETENTION/REPRO %s <%s: cached total=%d facets=%d | today total=%d facets=%d"
          % (bid, upto, old["total"], old["nfacets"], j["total"], len(j["facets"]["signature"])))
