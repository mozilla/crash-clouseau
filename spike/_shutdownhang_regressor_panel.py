# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Regenerate tests/archetypes/shutdownhang_bug_panel.json and the numbers in the
`shutdown-hang` archetype's closer (crashclouseau/archetypes.py).

    python3 spike/_shutdownhang_regressor_panel.py            # print the table only
    python3 spike/_shutdownhang_regressor_panel.py --write    # rewrite the committed panel

WHAT THE PANEL IS. Every bug whose `cf_crash_signature` contains the substring `shutdownhang |`
-- 613 with no date bound on 2026-08-21, 217 created since 2020-01-01, and it is the 2020 bound
that matters because `regressed_by` did not exist before then and an older bug can only
under-report. Then product in Core/Toolkit/Firefox, because a signature is shared across every
application built on m-c and 65 of those 217 are Thunderbird (54), MailNews Core (7), Data
Platform (2), NSS (1) and Toolkit Graveyard (1); then everything filed by the ACCOUNT this
pipeline files under excluded (6 of the remaining 152), because scoring our own output as
evidence about humans is the whole trap -- note only the 3 from 2026 are the pipeline's, the
other 3 are the same human's own bugs from 2020-2022 and 2 of those carry a `regressed_by`, so
this exclusion is the conservative direction. That leaves 146, of which 144 enter the panel. The
two that do not are `[@ ...]` PARSE failures and NOT Socorro gaps: bug 1685337 writes its
signature with no brackets at all and bug 1736568 as `[ @shutdownhang | ...`. Socorro dates both
of those signatures, and including them moves nothing: rb 19/146 = 13.0% and rb-among-FIXED
15/43 = 34.9%.

WHY IT EXISTS. The row shipped "Finally, expect NO regressor", generalised from one INVALID bug
the day after it closed. On the population that sentence speaks about a regressor is the answer
roughly a third of the time. Unauthenticated BMO + Socorro only, so a reviewer can rerun it.
"""
import argparse
import json
import os
import re
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PANEL = os.path.join(os.path.dirname(HERE), "tests", "archetypes",
                     "shutdownhang_bug_panel.json")
UA = {"User-Agent": "crash-clouseau"}
GECKO = ("Core", "Toolkit", "Firefox")
OURS = "cdenizet@mozilla.com"
FIELDS = ("id,summary,status,resolution,product,component,creator,creation_time,"
          "regressed_by,cf_crash_signature")


def _get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=90) as fh:
        return json.load(fh)


def bugs(since="2020-01-01", fields=FIELDS):
    """BMO: every bug carrying a `shutdownhang |` signature, created since `since`."""
    terms = [("f1", "cf_crash_signature"), ("o1", "substring"), ("v1", "shutdownhang |")]
    if since:
        terms += [("f2", "creation_ts"), ("o2", "greaterthaneq"), ("v2", since)]
    terms += [("include_fields", fields), ("limit", "0")]
    query = urllib.parse.urlencode(terms)
    return _get("https://bugzilla.mozilla.org/rest/bug?" + query)["bugs"]


def first_dates(signatures):
    """Socorro `SignatureFirstDate`, 20 signatures per call."""
    out = {}
    signatures = sorted(set(signatures))
    for i in range(0, len(signatures), 20):
        query = urllib.parse.urlencode([("signatures", s) for s in signatures[i:i + 20]])
        url = "https://crash-stats.mozilla.org/api/SignatureFirstDate/?" + query
        for hit in _get(url)["hits"]:
            out[hit["signature"]] = hit["first_date"]
        time.sleep(0.3)
    return out


def build():
    print("bugs with a `shutdownhang |` signature, no date bound:",
          len(bugs(since=None, fields="id")))
    raw = bugs()
    print("  created since 2020-01-01 (`regressed_by` does not exist before it):", len(raw))
    for bug in raw:
        found = [s for s in re.findall(r"\[@\s*(.*?)\s*\]", bug.get("cf_crash_signature") or "")
                 if s.startswith("shutdownhang")]
        bug["_sig"] = found[0] if found else None
    gecko = [b for b in raw if b["product"] in GECKO]
    base = [b for b in gecko if b["creator"] != OURS]
    print("  product in Core/Toolkit/Firefox:", len(gecko))
    print("  minus everything filed by %s:" % OURS, len(base))
    dates = first_dates([b["_sig"] for b in base if b["_sig"]])
    panel = []
    for bug in sorted(base, key=lambda b: b["id"]):
        first = dates.get(bug["_sig"] or "")
        if not first:
            # Two different failures, and conflating them is how a comment came to blame
            # Socorro for a drop that is OURS: `_sig` None means the `[@ ...]` parse found
            # nothing in `cf_crash_signature` (no brackets at all, or `[ @`) and Socorro was
            # never asked -- as of 2026-08-21 it dates both of the two signatures this drops.
            print("  dropped, %s:" % ("cf_crash_signature does not parse" if not bug["_sig"]
                                      else "no SignatureFirstDate"),
                  bug["id"], repr(bug["_sig"]))
            continue
        age = (_when(bug["creation_time"]) - _when(first)).days
        panel.append({
            "id": bug["id"], "product": bug["product"], "component": bug["component"],
            "creation_time": bug["creation_time"], "resolution": bug["resolution"],
            "signature": bug["_sig"], "signature_first_date": first,
            "signature_age_days_at_filing": age, "regressed_by": bug["regressed_by"],
            "summary": bug["summary"],
        })
    print("  with a resolvable signature age:", len(panel))
    return panel


def _when(stamp):
    import datetime
    return datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def wilson(n, total):
    if not total:
        return "  0/0"
    p, z = n / total, 1.96
    den = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / den
    half = z * ((p * (1 - p) / total + z * z / (4 * total * total)) ** 0.5) / den
    return "%3d/%-3d %5.1f%% [%4.1f-%4.1f]" % (n, total, 100 * p, 100 * (centre - half),
                                               100 * (centre + half))


def table(panel):
    def row(label, sub):
        fixed = [b for b in sub if b["resolution"] == "FIXED"]
        rb = [b for b in sub if b["regressed_by"]]
        print("%-24s n=%3d | FIXED %s | regressed_by %s | rb among FIXED %s" % (
            label, len(sub), wilson(len(fixed), len(sub)), wilson(len(rb), len(sub)),
            wilson(len([b for b in fixed if b["regressed_by"]]), len(fixed))))

    print()
    row("all", panel)
    for lo, hi, label in ((-1 << 30, 30, "sig age <30d"), (30, 365, "sig age 30-365d"),
                          (365, 1 << 30, "sig age >365d")):
        row("  " + label, [b for b in panel
                           if lo <= b["signature_age_days_at_filing"] < hi])
    negative = [b for b in panel if b["resolution"] in
                ("WORKSFORME", "INCOMPLETE", "INVALID", "INACTIVE")]
    print("\nWFM/INCOMPLETE/INVALID/INACTIVE", wilson(len(negative), len(panel)))
    for res in ("DUPLICATE", "WONTFIX"):
        print("%-31s%s" % (res, wilson(len([b for b in panel if b["resolution"] == res]),
                                       len(panel))))
    print("%-31s%s" % ("still open",
                       wilson(len([b for b in panel if not b["resolution"]]), len(panel))))
    print("\nFIXED + regressed_by on a signature already >1y old (kills the age-gated repair):")
    old = [b for b in panel
           if b["signature_age_days_at_filing"] >= 365
           if b["resolution"] == "FIXED" and b["regressed_by"]]
    for bug in sorted(old, key=lambda b: -b["signature_age_days_at_filing"]):
        print("  bug %d  sig %5dd  regressed_by=%s  %s" % (
            bug["id"], bug["signature_age_days_at_filing"], bug["regressed_by"],
            bug["summary"][:58]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true",
                        help="rewrite tests/archetypes/shutdownhang_bug_panel.json")
    args = parser.parse_args()
    panel = build()
    table(panel)
    if args.write:
        with open(PANEL, "w", encoding="utf-8") as fh:
            json.dump(panel, fh, indent=1)
            fh.write("\n")
        print("\nwrote", PANEL)


if __name__ == "__main__":
    main()
