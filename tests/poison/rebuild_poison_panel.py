"""Rebuild tests/poison/poison_fault_panel.json, IN FULL, from the raw panels in
/tmp/clouseau_s5/exposer-poison (fetch_nightly_long.py, fetch_filings.py, bmo_panel.py,
study_regby.py, the cached Poison.h/mozjemalloc.h -- see that directory's RESULTS.md for the
re-run order) plus spike/regressor_dataset/strategy_analysis.json for the study's own labels.

Every block of the fixture is produced here, so the committed file is regenerable rather than
hand-maintained; the only literals below are the PROSE (`source` / `note` / `_readme`), never a
count. Byte-identical to the committed copy:

  DATABASE_URL=sqlite:///:memory: REDIS_URL=redis://localhost:6379 \
      uv run python tests/poison/rebuild_poison_panel.py > /tmp/p.json && \
      cmp /tmp/p.json tests/poison/poison_fault_panel.json

`strategy_analysis.json` is gitignored (.gitignore:28) and the /tmp panels are session
scratch, so a reviewer without them can still CHECK the file (tests/test_exposer_poison.py
recomputes every docstring number from it) but cannot regenerate it -- run the fetchers in
RESULTS.md first.
"""
import collections
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
from crashclouseau.agent import orchestrator as orch  # noqa: E402

# The raw panels this reduces. Session scratch, not committed: override with
# CLOUSEAU_POISON_PANEL_SRC after re-running the fetchers listed in RESULTS.md.
B = os.environ.get("CLOUSEAU_POISON_PANEL_SRC", "/tmp/clouseau_s5/exposer-poison")
rows = json.load(open(B + "/nightly_90d.json"))


def dominant(addr):
    """The byte `_looks_poison` would test, or None when the address cannot fire at all."""
    f = orch._fault_address({"json_dump": {"crash_info": {"address": addr}}})
    if f is None or f <= orch._MAX_FIELD_FAULT:
        return None, f
    parts, x = [], f
    while x:
        parts.append(x & 0xFF)
        x >>= 8
    if len(parts) < 2:
        return None, f
    top = max(set(parts), key=parts.count)
    return (top if parts.count(top) >= max(2, len(parts) - 1) else None), f


per_addr = collections.defaultdict(
    lambda: {"reports": 0, "sigs": collections.Counter(), "builds": set()})
per_byte = collections.defaultdict(
    lambda: {"reports": 0, "sigs": set(), "builds": set(), "addresses": set()})
parseable = 0
for h in rows:
    top, f = dominant(h.get("address"))
    if f is not None:
        parseable += 1
    if top is None:
        continue
    key = "0x{:x}".format(f)
    a = per_addr[key]
    a["reports"] += 1
    a["sigs"][h.get("signature") or ""] += 1
    a["builds"].add(str(h.get("build_id"))[:8])
    b = per_byte[top]
    b["reports"] += 1
    b["sigs"].add(h.get("signature"))
    b["builds"].add(str(h.get("build_id"))[:8])
    b["addresses"].add(key)

addresses = [{"address": k, "reports": v["reports"], "signatures": len(v["sigs"]),
              "build_days": len(v["builds"]),
              "top_signature": v["sigs"].most_common(1)[0][0][:110]}
             for k, v in sorted(per_addr.items(), key=lambda kv: (-kv[1]["reports"], kv[0]))]
by_byte = {"0x%02X" % t: {"reports": v["reports"], "signatures": len(v["sigs"]),
                          "build_days": len(v["builds"]),
                          "addresses": len(v["addresses"])}
           for t, v in sorted(per_byte.items(), key=lambda kv: -kv[1]["reports"])}

panel = {b["id"]: b for b in json.load(open(B + "/panel.json"))}
filings = [{"bug": f["bug"], "uuid": panel[f["bug"]]["uuid"],
            "signature": (f["signature"] or "")[:110], "address": f["address"],
            "resolution": panel[f["bug"]]["resolution"] or panel[f["bug"]]["status"]}
           for f in json.load(open(B + "/filings_addresses.json"))]

# ---- the study's own labels, and the poison-literal scan over its evidence text ---------- #
LITERALS = re.compile(
    r"0x(e5e5|4b4b|cccc|cdcd|dddd|abab|fdfd|bebe|fbfb|5a5a|2b2b|a5a5|e4e4)", re.I)
cls = json.load(open(REPO + "/spike/regressor_dataset/strategy_analysis.json"))["classifications"]
labels = {c["bug_id"]: bool(c.get("exposer_not_cause")) for c in cls}
lit = {c["bug_id"]: bool(LITERALS.search(json.dumps(c))) for c in cls}
exposers = [b for b, e in labels.items() if e]
non_exposers = [b for b, e in labels.items() if not e]

study = [{"bug": c["id"], "component": c["component"], "status": c["status"],
          "resolution": c["resolution"], "regressed_by": c["regressed_by"],
          "exposer_not_cause": labels[c["id"]]}
         for c in json.load(open(B + "/poisoncases.json"))["bugs"]]

regby = {}
for name, path in (("exposer", "/study_exposer.json"), ("non_exposer", "/study_nonexposer.json")):
    regby[name] = sum(1 for b in json.load(open(B + path)) if b.get("regressed_by"))

bmo = json.load(open(B + "/bmo_panel_summary.json"))["summary"]

# ---- the in-tree constants, with their line numbers READ OFF the cached headers ---------- #
HEADERS = (("js/src/util/Poison.h", B + "/Poison.h"),
           ("memory/build/mozjemalloc.h", B + "/mzj.h"))
WANTED = [("0x2b", "JS_SWEPT_NURSERY_PATTERN"), ("0x49", "JS_MOVED_TENURED_PATTERN"),
          ("0x4b", "JS_SWEPT_TENURED_PATTERN"), ("0xab", "JS_FREED_BUFFER_PATTERN"),
          ("0xdb", "JS_POISONED_JSSCRIPT_DATA_PATTERN"), ("0xff", "JS_OOB_PARSE_NODE_PATTERN"),
          ("0xcd", "JS_LIFO_UNDEFINED_PATTERN"), ("0xcc", "JS_SCOPE_DATA_TRAILING_NAMES_PATTERN"),
          ("0xed", "JS_SWEPT_CODE_PATTERN"), ("0xe5", "kAllocPoison"), ("0xe4", "kAllocJunk")]
ANNOTATED = {"JS_SWEPT_CODE_PATTERN": "JS_SWEPT_CODE_PATTERN (x86/x86_64)"}
constants = []
for byte, name in WANTED:
    for repo_path, cached in HEADERS:
        hit = None
        for i, line in enumerate(open(cached, encoding="utf-8", errors="replace"), 1):
            if re.search(r"\b%s\b" % re.escape(name), line) and byte in line.lower():
                hit = i
                break
        if hit:
            constants.append({"byte": byte, "name": ANNOTATED.get(name, name),
                              "file": repo_path, "line": hit})
            break
    else:
        raise SystemExit("no in-tree line for %s (%s)" % (name, byte))

sys.stderr.write("reports=%d parseable=%d addresses=%d filings=%d constants=%d\n"
                 % (len(rows), parseable, len(addresses), len(filings), len(constants)))
print(json.dumps({
    "_readme": "Panel for `orchestrator._POISON_BYTES` / `_looks_poison` / `_classify_exposer`."
               " Rebuilt by tests/poison/rebuild_poison_panel.py. Every number in those"
               " functions' docstrings is recomputable from this file by"
               " tests/test_exposer_poison.py.",
    "census": {
        "source": "Socorro SuperSearch, product=Firefox, release_channel=nightly, "
                  "2026-05-24..2026-08-20 (89 days), day-by-day paging, "
                  "_columns=uuid,signature,address,date,build_id",
        "rebuild": "1) for each of the 89 days D: GET https://crash-stats.mozilla.org/api/"
                   "SuperSearch/?product=Firefox&release_channel=nightly&date=%3E%3DD&date="
                   "%3CD+1&_results_number=1000&_results_offset=N&_facets=_none&_columns=uuid"
                   "&_columns=signature&_columns=address&_columns=date&_columns=build_id "
                   "(unauthenticated, User-Agent: crash-clouseau, <=6 in flight), paging until "
                   "exhausted; 2) keep every row whose address passes orchestrator."
                   "_fault_address, is > _MAX_FIELD_FAULT and has >= 2 bytes whose most common "
                   "byte occurs >= max(2, len(parts)-1) times; 3) group by address -> reports / "
                   "distinct signatures / distinct build days / most common signature. Steps 2-3 "
                   "are `_looks_poison`'s own shape test with the byte-set membership check "
                   "removed, so the result is byte-set independent.",
        "reports": len(rows),
        "distinct_signatures": len({r.get("signature") for r in rows}),
        "parseable_addresses": parseable,
        "dominant_reports": sum(a["reports"] for a in addresses),
        "by_dominant_byte": by_byte,
        "note": "`addresses` is EVERY distinct fault address in the census that passes "
                "`_looks_poison`'s own dominance rule (> 0x1000, >= 2 bytes, count(top) >= "
                "max(2, len(parts)-1)) -- i.e. every address the predicate could fire on for ANY "
                "byte set. Whatever is not here cannot fire, so the per-byte census is exact "
                "rather than sampled.",
        "addresses": addresses,
    },
    "in_tree_poison_constants": constants,
    "in_tree_constants_read": "firefox-main via searchfox, 2026-08-21",
    "filings": filings,
    "bmo_signature_panel": {
        "source": "104 signatures that produced a poison fault in the census vs 104 "
                  "volume-matched non-poison controls from the same census (median volume 4 vs "
                  "4); BMO cf_crash_signature substring search, bmo_panel.py",
        "poison": {"n": bmo["poison"]["n"], "any_bug": bmo["poison"]["any_bug"],
                   "fixed": bmo["poison"]["fixed"],
                   "accepted_regressed_by": bmo["poison"]["regressed_by"]},
        "control": {"n": bmo["control"]["n"], "any_bug": bmo["control"]["any_bug"],
                    "fixed": bmo["control"]["fixed"],
                    "accepted_regressed_by": bmo["control"]["regressed_by"]},
    },
    "study_poison_literal_bugs": study,
    "filings_source": "every BMO bug with creator=cdenizet@mozilla.com, creation_time>="
                      "2026-08-05 and 'Crash in [@' in the summary (52 of them); the crash uuid "
                      "comes out of comment 0 and the address out of the live Socorro "
                      "ProcessedCrash. This is the pipeline's only outcome panel.",
    "filings_censoring": "READ THIS BEFORE PRICING A POISON RULE ON THE `filings` BLOCK. The "
                         "query above is ANONYMOUS, and a restricted bug is ABSENT from that "
                         "query form rather than an error -- so this block systematically loses "
                         "exactly the filings a human turned into security bugs, which for a "
                         "poison rule is the entire positive class. Measured 2026-08-24: 0 of "
                         "these 52 addresses satisfy `_looks_poison`, while bug 2065051 (crash "
                         "41bb8c8a, fault address 0xe5e5e5e5e5e5e5e8, mozjemalloc kAllocPoison) "
                         "is a Clouseau filing from this same window that DOES -- and it is "
                         "missing here, because :mccr8 had it restricted. So '0 of 52 filings "
                         "would have been affected' is a statement about BMO visibility, not "
                         "about poison. Rebuild the block from the local dossiers "
                         "(`models.Dossier.filed_bug_rows()`), where a restriction cannot hide "
                         "a row, before quoting any cost figure from it.",
    "study_poison_literal_bugs_source": "the five bugs of spike/regressor_dataset/"
                                        "strategy_analysis.json whose evidence quotes a "
                                        "poison-byte literal, with their live BMO status/"
                                        "resolution/regressed_by. Four of the five are "
                                        "exposer_not_cause=False in the study's own labels.",
    "study_289": {
        "source": "spike/regressor_dataset/strategy_analysis.json (the 289-bug regressor study; "
                  "the file is gitignored, hence these aggregates) crossed with live BMO "
                  "`regressed_by` / resolution, 2026-08-21",
        "exposers": len(exposers),
        "non_exposers": len(non_exposers),
        "exposers_with_accepted_regressed_by": regby["exposer"],
        "non_exposers_with_accepted_regressed_by": regby["non_exposer"],
        "exposers_with_poison_literal": sum(1 for b in exposers if lit[b]),
        "non_exposers_with_poison_literal": sum(1 for b in non_exposers if lit[b]),
        "poison_literal_bugs": {
            "exposer": sorted(b for b in exposers if lit[b]),
            "non_exposer": sorted(b for b in non_exposers if lit[b]),
        },
        "note": "the poison-literal scan is a search for 0x(e5e5|4b4b|cccc|cdcd|dddd|abab|fdfd|"
                "bebe|fbfb|5a5a|2b2b|a5a5|e4e4) over each classification record; 1/86 vs 4/203 "
                "is Fisher p=1.00, i.e. a poison literal does not discriminate exposers",
    },
}, indent=1))
