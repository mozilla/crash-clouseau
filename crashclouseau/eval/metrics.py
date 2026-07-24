# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Gating metrics over a corpus + agent results (#13).

Pure functions over ``CorpusCase``s and ``{uuid -> CrashTriageResult}``:
- off-stack recall (did the agent reach the true regressor) vs the stack-only
  baseline (was it in the seed candidate set),
- evidence precision of strong-evidence verdicts (right changeset + grounded),
- abstain calibration ({abstain,strong} x {findable,unfindable}).
Reads the #03 Dossier shape off each ``CrashTriageResult.dossier``; no network
except the optional diff-line check, injected via ``diff_checker`` so tests stay
offline."""
from __future__ import annotations

import json
import math
import re

from crashclouseau import utils
from crashclouseau.agent.schema import CONFIDENCE_SCORE, Decision
from crashclouseau.eval.models import Metrics


def _short(node):
    return utils.short_rev(node) if node else ""


def _dossier(results, uuid):
    r = results.get(uuid)
    return r.dossier if r is not None else None


def _nodes_in_dossier(dossier):
    """Every hg node the dossier references (candidate + hunks + diff-line cites)."""
    nodes = set()
    if dossier is None:
        return nodes
    if dossier.candidate and dossier.candidate.node:
        nodes.add(_short(dossier.candidate.node))
    for hunk in dossier.hunks:
        if hunk.node:
            nodes.add(_short(hunk.node))
        for cite in hunk.citations:
            node = getattr(cite, "node", None)
            if node:
                nodes.add(_short(node))
    cite_sources = []
    if dossier.call_path:
        for edge in dossier.call_path.edges:
            cite_sources += edge.citations
    if dossier.data_flow:
        cite_sources += dossier.data_flow.citations
    for cite in cite_sources:
        node = getattr(cite, "node", None)
        if node:
            nodes.add(_short(node))
    return nodes


def _is_strong(dossier):
    if not (dossier and dossier.verdict):
        return False
    return dossier.verdict.decision == Decision.strong_evidence


def _is_lead(dossier):
    if not (dossier and dossier.verdict):
        return False
    return dossier.verdict.decision == Decision.lead


_BUG_RE = re.compile(r"\bbug\s+(\d+)", re.I)


def _case_nodes(case):
    """Ground-truth regressor nodes (short-rev) — the resolved landing set, or the
    single-node compat field."""
    nodes = case.regressor_nodes or ([case.regressor_node] if case.regressor_node else [])
    return {_short(n) for n in nodes if n}


def _case_bugs(case):
    """Ground-truth regressor bug ids (authoritative from ``regressed_by``)."""
    bugs = list(case.regressor_bugs) or ([case.regressor_bug] if case.regressor_bug else [])
    return {int(b) for b in bugs if b}


def _bugs_in_dossier(dossier):
    """Every bug id the dossier points at: the candidate's bug + any ``bug NNN`` cited in
    the free-text mechanism/consistency/needinfo. Bug matching is alias-free, so it is the
    robust signal when the dossier's node (a mozilla-central/searchfox rev) doesn't
    string-equal the regressor's autoland landing rev."""
    bugs = set()
    if dossier is None:
        return bugs
    if dossier.candidate and dossier.candidate.bug:
        bugs.add(int(dossier.candidate.bug))
    v = dossier.verdict
    texts = []
    if v:
        for claim in (v.mechanism, v.consistency):
            if claim and claim.statement:
                texts.append(claim.statement)
        if getattr(v, "needinfo_draft", None):
            texts.append(v.needinfo_draft)
    for text in texts:
        for m in _BUG_RE.finditer(text or ""):
            bugs.add(int(m.group(1)))
    return bugs


def _hit(case, dossier):
    """True if the dossier points at the true regressor by NODE or by BUG."""
    if _case_nodes(case) & _nodes_in_dossier(dossier):
        return True
    return bool(_case_bugs(case) & _bugs_in_dossier(dossier))


def person_hit(case, dossier, author_of):
    """PERSON-LEVEL hit ("silver nugget"): the accused changeset's author is one of the true
    regressor's authors — a good needinfo target even when the exact changeset is wrong (the
    pivoted goal). ``author_of`` is a node->author resolver (``eval.authors.author_of``).
    A superset of the node/bug ``_hit`` in spirit; scored separately so both levels report."""
    from crashclouseau.eval.authors import same_person
    if dossier is None or dossier.candidate is None or not dossier.candidate.node:
        return False
    return same_person(author_of(dossier.candidate.node),
                       getattr(case, "regressor_authors", None))


def _findable(case):
    """Proxy: the regressor is on-stack, or in the stack-only seed set."""
    if case.on_stack_label is True:
        return True
    return bool(_case_nodes(case) & {_short(n) for n in case.seed_nodes})


def offstack_recall(cases, results):
    off = [c for c in cases if c.on_stack_label is False]
    n = len(off)
    if n == 0:
        return {"offstack_recall": 0.0, "stackonly_recall": 0.0, "n_offstack": 0}
    reached = seed_hit = 0
    for case in off:
        if _hit(case, _dossier(results, case.uuid)):
            reached += 1
        if _case_nodes(case) & {_short(n) for n in case.seed_nodes}:
            seed_hit += 1
    return {
        "offstack_recall": reached / n,
        "stackonly_recall": seed_hit / n,
        "n_offstack": n,
    }


def evidence_correctness(cases, results, diff_checker=None):
    """Precision of strong-evidence verdicts: named the right changeset, carries
    citations, and (if ``diff_checker`` given) its cited diff lines verify."""
    by_uuid = {c.uuid: c for c in cases}
    strong = []
    for uuid in results:
        dossier = _dossier(results, uuid)
        if _is_strong(dossier) and uuid in by_uuid:
            strong.append((by_uuid[uuid], dossier))
    if not strong:
        return {"evidence_precision": 0.0, "n_strong": 0}
    correct = 0
    for case, dossier in strong:
        named_ok = _hit(case, dossier)  # references the true regressor by node or bug
        cited = bool(_nodes_in_dossier(dossier) or (dossier.call_path and dossier.call_path.edges))
        diff_ok = True if diff_checker is None else bool(diff_checker(case, dossier))
        if named_ok and cited and diff_ok:
            correct += 1
    return {"evidence_precision": correct / len(strong), "n_strong": len(strong)}


def lead_precision(cases, results):
    """Precision of ``lead`` verdicts: share of leads that reference the true regressor
    (by node OR bug). A strict lower-bound proxy for "is this lead worth a human's time" —
    a mechanism lead that names no regressor counts as a miss here, so read it alongside
    the calibration matrix (``lead_unfindable`` is the low-value-lead risk cell)."""
    by_uuid = {c.uuid: c for c in cases}
    leads = []
    for uuid in results:
        dossier = _dossier(results, uuid)
        if _is_lead(dossier) and uuid in by_uuid:
            leads.append((by_uuid[uuid], dossier))
    if not leads:
        return {"lead_precision": 0.0, "n_lead": 0}
    hit = sum(1 for case, dossier in leads if _hit(case, dossier))
    return {"lead_precision": hit / len(leads), "n_lead": len(leads)}


def cost_summary(results):
    """Per-case mean + total cost/tokens over the scored (non-None) results."""
    scored = [r for r in results.values() if r is not None]
    n = len(scored)
    if not n:
        return {"mean_cost_usd": 0.0, "total_cost_usd": 0.0,
                "mean_output_tokens": 0.0, "mean_input_tokens": 0.0, "n_scored": 0}
    total = sum(float(getattr(r, "total_cost_usd", 0.0) or 0.0) for r in scored)
    out = sum(int(getattr(r, "output_tokens", 0) or 0) for r in scored)
    inp = sum(int(getattr(r, "input_tokens", 0) or 0) for r in scored)
    return {
        "mean_cost_usd": total / n, "total_cost_usd": total,
        "mean_output_tokens": out / n, "mean_input_tokens": inp / n, "n_scored": n,
    }


def abstain_calibration(cases, results):
    """{strong, lead, abstain, errored} x {findable, unfindable} confusion matrix. The
    cells that matter for a prompt/threshold change: ``abstain_findable`` (false abstains
    — should fall) and ``lead_unfindable`` (leads offered where even the seed missed the
    regressor — the low-value-lead risk that should stay bounded). ``errored_*`` is a run
    that failed (max_turns/timeout/exception → a None result), bucketed apart so a crashed
    run never masquerades as a deliberate abstain (which would silently flatter the
    false-abstain cell)."""
    conf = {
        v + f: 0
        for v in ("strong_", "lead_", "abstain_", "errored_")
        for f in ("findable", "unfindable")
    }
    for case in cases:
        if results.get(case.uuid) is None:
            verdict = "errored_"
        else:
            dossier = _dossier(results, case.uuid)
            verdict = "strong_" if _is_strong(dossier) else (
                "lead_" if _is_lead(dossier) else "abstain_"
            )
        conf[verdict + ("findable" if _findable(case) else "unfindable")] += 1
    return conf


# --------------------------------------------------------------------------- #
# Phase-2 calibration: per-case rows + precision-first threshold/reliability.
# --------------------------------------------------------------------------- #
def _reported(dossier):
    """A non-abstain, non-errored verdict = a lead/strong the pipeline points a human at.
    (A reported culprit-absent negative, or a reported case that missed the true regressor,
    is a false-investigate — the precision-first metric that gates the report threshold.)"""
    if not (dossier and dossier.verdict):
        return False
    return dossier.verdict.decision in (Decision.strong_evidence, Decision.lead)


def _confidence_value(dossier):
    if not (dossier and dossier.verdict and dossier.verdict.confidence is not None):
        return None
    return dossier.verdict.confidence


def _score(dossier):
    """The 0-100 rung the calibration keys on (``CONFIDENCE_SCORE``*100), or None (abstain)."""
    conf = _confidence_value(dossier)
    if conf is None:
        return None
    return int(round(CONFIDENCE_SCORE.get(conf, 0.0) * 100))


def per_case_rows(cases, results, author_of=None):
    """One flat, serializable row per case: the labeled ``(score, hit, is_negative, ...)``
    pair calibration needs. Persisted to ``results.jsonl`` so refitting/re-thresholding never
    re-runs the (expensive) agent. When ``author_of`` (a node->author resolver, e.g.
    ``eval.authors.author_of``) is given, each row also carries the PERSON-LEVEL signal
    (``person_hit`` + the resolved ``cited_author``) so a person-level refit stays offline;
    it is omitted offline (tests) so no network lookup fires."""
    rows = []
    for case in cases:
        r = results.get(case.uuid)
        dossier = r.dossier if r is not None else None
        conf = _confidence_value(dossier)
        cited_node = (_short(dossier.candidate.node)
                      if dossier and dossier.candidate and dossier.candidate.node
                      else None)
        row = {
            "uuid": case.uuid,
            "decision": ("errored" if r is None
                         else (dossier.verdict.decision.value
                               if dossier and dossier.verdict else "abstain")),
            "confidence": conf.value if conf is not None else None,
            "score": _score(dossier),
            "hit": _hit(case, dossier),
            "reported": _reported(dossier),
            # The primary changeset this run blamed (short-rev), for multi-sample
            # SAME-TARGET voting: real regressors reproduce across runs, spurious ones
            # scatter. None when the run cites no candidate (abstain / mechanism-only lead).
            "cited_node": cited_node,
            "is_negative": bool(getattr(case, "is_negative", False)),
            "is_offstack": (bool(case.is_offstack)
                            if getattr(case, "is_offstack", None) is not None else None),
            "on_stack_label": case.on_stack_label,
            "cost_usd": float(getattr(r, "total_cost_usd", 0.0) or 0.0) if r else 0.0,
            "input_tokens": int(getattr(r, "input_tokens", 0) or 0) if r else 0,
            "output_tokens": int(getattr(r, "output_tokens", 0) or 0) if r else 0,
            "num_turns": int(getattr(r, "num_turns", 0) or 0) if r else 0,
            "corroborations": (dict(dossier.corroborations)
                               if dossier and getattr(dossier, "corroborations", None)
                               else {}),
        }
        if author_of is not None:
            # Person-level ("silver nugget"): does the blamed changeset's author match one of
            # the true regressor's authors, even when the exact changeset is wrong?
            ph = person_hit(case, dossier, author_of)
            row["person_hit"] = ph
            row["cited_author"] = (
                author_of(dossier.candidate.node)
                if dossier and dossier.candidate and dossier.candidate.node
                else None
            )
            # The PERSON-LEVEL worth-investigating outcome the calibration fits: the exact
            # changeset/bug hit OR the same-author silver nugget. This is what DECOUPLES
            # worth-investigating from proof-strength — a right-person/wrong-changeset lead
            # is worth a human's time, so it must not be bucketed with a culprit-absent miss.
            row["worth"] = bool(row["hit"] or ph)
        rows.append(row)
    return rows


def person_metrics(cases, results, author_of):
    """PERSON-LEVEL precision (the pivoted goal = get the right PERSON investigating). Over every
    REPORTED (lead/strong) case, the share that reaches the true regressor's AUTHOR — by the exact
    changeset/bug (``_hit``) OR by naming a DIFFERENT changeset with the same author (the 'silver
    nugget', ``person_hit``). A reported culprit-absent negative reaches no true author, so it
    stays a person-level false-investigate. ``author_of`` is a node->author resolver
    (``eval.authors.author_of``); pass a dict-``.get``/stub in tests to stay offline."""
    by_uuid = {c.uuid: c for c in cases}
    reported = [
        (by_uuid[uuid], _dossier(results, uuid))
        for uuid in results
        if uuid in by_uuid and _reported(_dossier(results, uuid))
    ]
    if not reported:
        return {"person_precision": 0.0, "n_reported": 0, "n_person_hit": 0}
    hits = sum(
        1 for case, dossier in reported
        if _hit(case, dossier) or person_hit(case, dossier, author_of)
    )
    return {
        "person_precision": hits / len(reported),
        "n_reported": len(reported),
        "n_person_hit": hits,
    }


def false_investigate(cases, results):
    """Over the culprit-absent NEGATIVE arm: share reported as a lead/strong. This is the
    precision-first crux — the study's ~0-FP that the report threshold must preserve."""
    negs = [c for c in cases if getattr(c, "is_negative", False)]
    n = len(negs)
    fi = sum(1 for c in negs if _reported(_dossier(results, c.uuid)))
    return {
        "false_investigate_rate": (fi / n) if n else 0.0,
        "n_negative": n,
        "n_false_investigate": fi,
    }


def _row_worth(r):
    """The PERSON-LEVEL worth-investigating outcome of a persisted row — what the calibration
    fits — preferring ``worth`` (exact hit OR same-author 'silver nugget'), and falling back to
    the changeset-exact ``hit`` for rows written before person scoring (or scored offline). This
    is why ``Verdict.p_worth_investigating`` measures worth-investigating (reaching the right
    person) rather than proof-strength (the exact changeset), per the Phase-2 pivot."""
    return bool(r["worth"] if "worth" in r else r.get("hit"))


def reliability_bins(rows):
    """Per-rung empirical calibration over REPORTED rows: for each score rung, n, worth-hits
    (person-level via ``_row_worth``), empirical P(worth), and how many were culprit-absent
    negatives (all misses). A reported row that reached NEITHER the true regressor nor its author
    (or is a negative) counts as ~not worth investigating. ``n_hit``/``p_hit`` keep their names
    for the calibrate consumer, but count person-level worth (not changeset-exact hits)."""
    buckets = {}
    for r in rows:
        if not r.get("reported") or r.get("score") is None:
            continue
        b = buckets.setdefault(
            r["score"], {"score": r["score"], "n": 0, "n_hit": 0, "n_negative": 0}
        )
        b["n"] += 1
        b["n_hit"] += 1 if _row_worth(r) else 0
        b["n_negative"] += 1 if r.get("is_negative") else 0
    out = []
    for score in sorted(buckets):
        b = buckets[score]
        b["p_hit"] = b["n_hit"] / b["n"] if b["n"] else 0.0
        out.append(b)
    return out


def ece(bins):
    """Expected calibration error: Sum |rung_prob - p_hit| * n/N over the reliability bins,
    where rung_prob = score/100 is what the current uncalibrated score CLAIMS."""
    total = sum(b["n"] for b in bins)
    if not total:
        return 0.0
    return sum(abs(b["score"] / 100.0 - b["p_hit"]) * b["n"] for b in bins) / total


def threshold_sweep(rows):
    """Sweep the report threshold tau over the rung scores (high->low). At each tau, over the
    rows reported with score>=tau: precision = TP / all-reported (a reported negative OR a
    reported person-miss is a FP), recall = TP / all positive cases, and false_investigate_rate
    over the negative arm. TP is PERSON-LEVEL (``_row_worth``), matching the calibration.
    Precision-first tau = the highest tau whose false_investigate stays at the study's ~0 (see
    ``pick_threshold`` in calibrate)."""
    total_pos = sum(1 for r in rows if not r.get("is_negative"))
    n_neg = sum(1 for r in rows if r.get("is_negative"))
    reported = [r for r in rows if r.get("reported") and r.get("score") is not None]
    out = []
    for tau in sorted({r["score"] for r in reported}, reverse=True):
        at = [r for r in reported if r["score"] >= tau]
        pos_at = [r for r in at if not r["is_negative"]]
        neg_at = [r for r in at if r["is_negative"]]
        tp = sum(1 for r in pos_at if _row_worth(r))
        out.append({
            "tau": tau,
            "n_reported": len(at),
            "n_reported_pos": len(pos_at),
            "n_false_investigate": len(neg_at),
            "false_investigate_rate": (len(neg_at) / n_neg) if n_neg else 0.0,
            "precision": (tp / len(at)) if at else 0.0,
            "recall": (tp / total_pos) if total_pos else 0.0,
        })
    return out


def wilson_ci(k, n, z=1.96):
    """Wilson score interval for a binomial proportion — the honest CI on a small negative
    arm (e.g. 0/30 false-investigate -> upper bound ~0.11, so claim '<=~10% FP', not 0%)."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def compute_metrics(cases, results, sweep_config=None, corpus_hash="", diff_checker=None,
                    author_of=None):
    off = offstack_recall(cases, results)
    ev = evidence_correctness(cases, results, diff_checker=diff_checker)
    ld = lead_precision(cases, results)
    cost = cost_summary(results)
    fi = false_investigate(cases, results)
    # PERSON-LEVEL scoring (the pivoted goal). Only when an author resolver is supplied — an
    # offline call (tests) leaves the person fields at their 0 defaults, no network lookup.
    pm = (person_metrics(cases, results, author_of) if author_of is not None
          else {"person_precision": 0.0, "n_reported": 0, "n_person_hit": 0})
    return Metrics(
        offstack_recall=off["offstack_recall"],
        stackonly_recall=off["stackonly_recall"],
        evidence_precision=ev["evidence_precision"],
        lead_precision=ld["lead_precision"],
        abstain_calibration=abstain_calibration(cases, results),
        n_cases=len(cases),
        n_offstack=off["n_offstack"],
        n_strong=ev["n_strong"],
        n_lead=ld["n_lead"],
        n_errored=sum(1 for c in cases if results.get(c.uuid) is None),
        false_investigate_rate=fi["false_investigate_rate"],
        n_negative=fi["n_negative"],
        n_false_investigate=fi["n_false_investigate"],
        person_precision=pm["person_precision"],
        n_person_hit=pm["n_person_hit"],
        n_reported=pm["n_reported"],
        mean_cost_usd=cost["mean_cost_usd"],
        total_cost_usd=cost["total_cost_usd"],
        mean_output_tokens=cost["mean_output_tokens"],
        mean_input_tokens=cost["mean_input_tokens"],
        corpus_hash=corpus_hash,
        sweep_config=sweep_config or {},
    )


def compare_to_baseline(metrics, baseline_path):
    """Compare against a committed baseline metrics.json. ``deltas`` are the quality
    metrics (higher is better) and gate pass/regress. ``info`` is non-gating context for
    a prompt/threshold change: the false-abstain count (lower is better) and the cost
    delta (so a quality win can be weighed against what it cost)."""
    try:
        with open(baseline_path) as handle:
            base = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {"status": "no-baseline", "deltas": {}, "info": {}}
    deltas = {
        "offstack_recall": metrics.offstack_recall - base.get("offstack_recall", 0.0),
        "evidence_precision": metrics.evidence_precision - base.get("evidence_precision", 0.0),
        "lead_precision": metrics.lead_precision - base.get("lead_precision", 0.0),
    }
    regressed = any(v < -1e-9 for v in deltas.values())
    base_cal = base.get("abstain_calibration") or {}
    info = {
        "abstain_findable": (metrics.abstain_calibration.get("abstain_findable", 0) - base_cal.get("abstain_findable", 0)),
        "n_errored": metrics.n_errored - base.get("n_errored", 0),
        "mean_cost_usd": metrics.mean_cost_usd - base.get("mean_cost_usd", 0.0),
    }
    return {"status": "regress" if regressed else "pass", "deltas": deltas, "info": info}
