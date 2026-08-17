# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Prior-signature corroboration (P4).

A PRIOR already-FIXED crash bug that shares this crash's Socorro signature and already
carries a ``regressed_by`` is an OFF-STACK-PROOF pointer: its regressor bug is a strong
prior for THIS crash's regressor, independent of the crash stack. The spike measured the
reach (``spike/PRIOR_SIGNATURE_REPORT.md``): ~10% of OFF-STACK crashes get a corroborated
pointer to the EXACT regressor (vs ~1% on-stack), so this is used as a deterministic
lead-strengthener + candidate-ranking prior on the P1 off-stack path — NOT a standalone
verdict (per the study's P4 FP guidance: never inherit on a generic signature alone).

This module only ENUMERATES the prior-named regressor bugs; the FP guard lives in the
caller (``orchestrator.build_seed`` / ``_corroborations``): the confidence bump fires only
when a named bug ALSO landed in this crash's regression window (a model-independent 2nd
axis — the study's component/moz-reason/see_also axes aren't all available at live triage)
AND there is a SINGLE such in-window prior (a hot signature naming several is ranked for
recall but never bumps confidence).

Best-effort + never raises: a Socorro/Bugzilla hiccup degrades to "no prior", never
blocking a triage. libmozdata's UA is stamped by ``crashclouseau.net`` at import, so these
calls identify as ``crash-clouseau`` rather than getting 406-throttled.
"""
from __future__ import annotations

from libmozdata.bugzilla import Bugzilla
from libmozdata.socorro import Bugs as SocorroBugs

from crashclouseau import models
from crashclouseau.logger import logger

# Bound the Bugzilla fetch: a hot generic signature can map to many bugs, and we only need
# enough priors to surface a regressor pointer.
_MAX_SIBLINGS = 60
_MAX_SIGS = 4


def _our_own_claims():
    """``{(prior_bug, regressor_bug), ...}`` this pipeline itself wrote as ``regressed_by``.

    Since the filer sets that field (``bugzilla_apply._link_regressed_by``), a prior sibling
    naming a regressor is no longer necessarily a HUMAN naming one — and this module's whole
    premise is that it is ("an ALREADY-KNOWN regressor"). Without this filter one wrong
    attribution becomes its own corroboration: bug 2043000 got three filings off the same
    signature cluster, so the second would have read the first's claim as an independent prior
    and the caller would have BUMPED confidence on it.

    Only exact ``(bug, regressor)`` pairs are dropped, not every bug we filed: once a reviewer
    REPLACES our value the field is a human's again, and that is the most valuable prior there is.
    """
    claims = set()
    for row in models.Dossier.filed_bug_rows():
        filed = row.get("filed_bug") or {}
        bug = filed.get("bug")
        if not bug:
            continue
        for rb in filed.get("regressed_by") or []:
            claims.add((int(bug), int(rb)))
    return claims


def prior_regressor_hints(signatures, exclude_bug=None):
    """Prior FIXED sibling bugs (sharing an exact Socorro signature) that already name a
    regressor. Returns a de-duped list of ``{"regressor_bug", "prior_bug", "prior_summary"}``
    — the regressor bugs those siblings were regressed by, a strong off-stack prior for this
    crash's own regressor. Best-effort: ``[]`` on empty input or any lookup failure.

    At LIVE triage the leakage guard is implicit: a usable prior must be ``FIXED`` with
    ``regressed_by`` set (i.e. an ALREADY-KNOWN regressor), which by definition predates the
    fresh crash being triaged. ``exclude_bug`` drops the crash's own bug if known, and
    ``_our_own_claims`` drops the pairs this pipeline wrote itself — ``FIXED`` alone no longer
    implies a human vouched for the attribution, only that somebody fixed the crash."""
    try:
        sigs = [s for s in (signatures or []) if s][:_MAX_SIGS]
        if not sigs:
            return []
        s2b = SocorroBugs.get_bugs(sigs)  # {signature: [bug_id, ...]}
        # NEWEST first, THEN cap. Socorro's signature->bugs map is unordered, so an ascending
        # sort kept the 60 OLDEST siblings — i.e. on exactly the hot, long-lived signatures where
        # the cap actually bites, it threw away the priors most likely to still be relevant to
        # today's code. Bug ids are monotonic in filing time, so descending is "most recent".
        ids = sorted(
            {b for v in s2b.values() for b in (v or []) if b != exclude_bug}, reverse=True
        )
        ids = ids[:_MAX_SIBLINGS]
        if not ids:
            return []
        got: dict = {}

        def handler(bug, data):
            data[bug["id"]] = bug

        Bugzilla(
            bugids=[str(i) for i in ids],
            include_fields=["id", "summary", "resolution", "regressed_by"],
            bughandler=handler,
            bugdata=got,
        ).get_data().wait()

        # Ours, and therefore not evidence. Read only once there is something to check, and
        # failing CLOSED (no hints) rather than open: this module already degrades to "no prior"
        # on any hiccup, and re-admitting our own claim as corroboration is the failure that
        # cannot be seen from the outside.
        try:
            ours = _our_own_claims() if got else set()
        except Exception as exc:                            # pragma: no cover - defensive
            logger.warning("priorsig: cannot tell our own claims apart (%s) — no hints", exc)
            return []

        hints: dict = {}
        for sib_id, bug in got.items():
            if bug.get("resolution") != "FIXED":
                continue
            for rb in (bug.get("regressed_by") or []):
                if rb == exclude_bug or rb in hints:
                    continue
                if (sib_id, rb) in ours:
                    logger.info("priorsig: skipping bug %s -> %s, that regressed_by is OURS",
                                sib_id, rb)
                    continue
                hints[rb] = {
                    "regressor_bug": rb,
                    "prior_bug": sib_id,
                    "prior_summary": (bug.get("summary") or "")[:120],
                }
        if hints:
            logger.info(
                "priorsig: %d prior-signature regressor hint(s) from %d sibling(s): %s",
                len(hints), len(got), sorted(hints),
            )
        return list(hints.values())
    except Exception as exc:  # pragma: no cover - network best-effort
        logger.warning("priorsig: lookup failed: %s", exc)
        return []
