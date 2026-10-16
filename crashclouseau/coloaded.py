# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Find modules enriched in a signature family using Bonferroni-corrected Fisher tests.

Controls match product, channel, version and, when known, process type and platform.
Deduplicate by install_time; exclude case signatures and sampled install_times from controls.
install_time is a timestamp, not a unique installation ID.
"""
import math
from datetime import datetime, timedelta, timezone

from libmozdata import socorro
from libmozdata.connection import Query

from crashclouseau import utils
from crashclouseau.logger import logger

SAMPLE_INSTALLS = 40
BASELINE_INSTALLS = 80
BASELINE_PER_SIGNATURE = 2
ALPHA = 0.01
MAX_ROWS = 8
DAYS = 28
_HITS = 500
# Limit concurrent processed-crash requests.
_BATCH = 10


def _search_hits(params):
    got = {}

    def handler(json_, data):
        data["result"] = json_

    socorro.SuperSearch(params=params, handler=handler, handlerdata=got).wait()
    return (got.get("result") or {}).get("hits") or []


def _pick(params, n, per_signature=None, skip_signatures=(), skip_installs=()):
    """Pick newest (uuid, install_time) pairs with distinct install_time values."""
    hits = _search_hits(dict(params, _columns=["uuid", "signature", "install_time"],
                             _results_number=_HITS, _sort="-date"))
    out, installs, per = [], set(skip_installs), {}
    for hit in hits:
        install, sig = hit.get("install_time"), hit.get("signature")
        if not hit.get("uuid") or install is None or install in installs:
            continue
        if sig in skip_signatures or (per_signature and per.get(sig, 0) >= per_signature):
            continue
        installs.add(install)
        per[sig] = per.get(sig, 0) + 1
        out.append((hit["uuid"], install))
        if len(out) >= n:
            break
    return out


def summarize(processed):
    """Return ``{"modules": {name: signer}, "stack": {name}}`` for one processed crash."""
    dump = (processed or {}).get("json_dump") or {}
    modules = {}
    for m in dump.get("modules") or []:
        if isinstance(m, dict) and m.get("filename"):
            modules[m["filename"].lower()] = m.get("cert_subject")
    stack = set()
    threads = dump.get("threads") or []
    crashing = (processed or {}).get("crashing_thread")
    if isinstance(crashing, int) and 0 <= crashing < len(threads):
        stack = {str(f.get("module")).lower() for f in threads[crashing].get("frames") or []
                 if isinstance(f, dict) and f.get("module")}
    return {"modules": modules, "stack": stack}


def _summaries(uuids):
    """Fetch module summaries, omitting crashes with no modules."""
    out = {u: {} for u in uuids}

    def handler(json_, data):
        data.update(summarize(json_))

    for start in range(0, len(uuids), _BATCH):
        socorro.ProcessedCrash(queries=[
            Query(socorro.ProcessedCrash.URL, {"crash_id": u, "datatype": "processed"},
                  handler, out[u]) for u in uuids[start:start + _BATCH]]).wait()
    return [s for s in out.values() if s.get("modules")]


def _fisher_greater(a, b, c, d):
    """Return the one-sided Fisher exact P(X >= a) for the table [[a, b], [c, d]]."""
    n1, n2, k = a + b, c + d, a + c
    total = math.comb(n1 + n2, k)
    return sum(math.comb(n1, x) * math.comb(n2, k - x)
               for x in range(a, min(n1, k) + 1)) / total


def best_possible_p(n1, n2):
    """Minimum one-sided p-value: present in all cases and no controls."""
    return 1.0 / math.comb(n1 + n2, n1) if n1 > 0 else 1.0


def enrichment(sample, baseline, alpha=ALPHA, max_rows=MAX_ROWS):
    """Find enriched modules; group identical loading and stack membership."""
    n1, n2 = len(sample), len(baseline)
    tested = sorted({m for s in sample for m in s["modules"]})
    if not n1 or not n2 or not tested:
        return {"sample": n1, "baseline": n2, "tested": len(tested), "rows": []}
    groups = {}
    for module in tested:
        in_sample = frozenset(i for i, s in enumerate(sample) if module in s["modules"])
        in_base = frozenset(i for i, s in enumerate(baseline) if module in s["modules"])
        k1, k2 = len(in_sample), len(in_base)
        if k1 * n2 <= k2 * n1:
            continue
        p = _fisher_greater(k1, n1 - k1, k2, n2 - k2) * len(tested)
        if p > alpha:
            continue
        on_stack = frozenset(i for i, s in enumerate(sample) if module in s["stack"])
        group = groups.setdefault((in_sample, in_base, on_stack), {
            "modules": [], "signers": [], "in_sample": k1, "in_baseline": k2, "p": p,
            "on_stack": len(on_stack)})
        group["modules"].append(module)
        signer = next((s["modules"][module] for s in sample if s["modules"].get(module)), None)
        if signer and signer not in group["signers"]:
            group["signers"].append(signer)
    rows = sorted(groups.values(), key=lambda g: (g["p"], g["modules"][0]))
    return {"sample": n1, "baseline": n2, "tested": len(tested), "rows": rows[:max_rows]}


def for_signature(signatures, product, channel, version, process_type, platform):
    """Sample cases and controls; return None on missing inputs, insufficient cases or failure."""
    signatures = [s for s in signatures or [] if s]
    if not signatures or not version:
        return None
    since = (datetime.now(timezone.utc) - timedelta(days=DAYS)).strftime("%Y-%m-%d")
    scope = {"product": product or "Firefox", "version": version, "date": ">=" + since,
             "release_channel": utils.get_search_channel(channel)}
    if process_type:
        scope["process_type"] = process_type
    if platform:
        scope["platform"] = platform
    try:
        picked = _pick(dict(scope, signature=["=" + s for s in signatures]), SAMPLE_INSTALLS)
        sample = _summaries([u for u, _ in picked])
        tested = len({m for s in sample for m in s["modules"]})
        if not tested or best_possible_p(len(sample), BASELINE_INSTALLS) * tested > ALPHA:
            return None
        # SuperSearch ORs negations; filter remaining case signatures locally.
        others = _pick(dict(scope, signature="!=" + signatures[0]), BASELINE_INSTALLS,
                       per_signature=BASELINE_PER_SIGNATURE, skip_signatures=set(signatures),
                       skip_installs={i for _, i in picked})
        baseline = _summaries([u for u, _ in others])
    except Exception as exc:  # pragma: no cover - network failures
        logger.warning("coloaded: module sample failed for %r: %s", signatures[0], exc)
        return None
    out = enrichment(sample, baseline)
    out["scope"] = {"product": scope["product"], "version": version,
                    "process_type": process_type, "platform": platform}
    return out


def lines(result):
    """Render enriched modules as prompt lines; return [] for no rows."""
    rows = (result or {}).get("rows") or []
    if not rows:
        return []
    scope = result.get("scope") or {}
    where = " ".join(str(x) for x in (scope.get("product"), scope.get("version"),
                                      scope.get("process_type")) if x)
    out = ["", "MODULES LOADED MORE OFTEN than in other {} crashes{}: one report per "
               "installation, {} installations of this signature against {} of other "
               "signatures; one-sided Fisher exact test, Bonferroni-corrected over the {} "
               "modules seen. A loaded module is not on the crashing stack unless marked; it "
               "shows what these users ran or had installed and does not by itself implicate "
               "the module.".format(where, " on {}".format(scope["platform"])
                                    if scope.get("platform") else "",
                                    result["sample"], result["baseline"], result["tested"])]
    for row in rows:
        out.append("  - {}{}: {}/{} vs {}/{}{}".format(
            ", ".join(row["modules"]),
            " [{}]".format("; ".join(row["signers"])) if row["signers"] else "",
            row["in_sample"], result["sample"], row["in_baseline"], result["baseline"],
            "; on the crashing stack in {}".format(row["on_stack"]) if row["on_stack"] else ""))
    return out
