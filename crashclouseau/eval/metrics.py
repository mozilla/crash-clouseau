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

from crashclouseau import utils
from crashclouseau.agent.schema import Decision
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


def _findable(case):
    """Proxy: the regressor is on-stack, or in the stack-only seed set."""
    if case.on_stack_label is True:
        return True
    return _short(case.regressor_node) in {_short(n) for n in case.seed_nodes}


def offstack_recall(cases, results):
    off = [c for c in cases if c.on_stack_label is False]
    n = len(off)
    if n == 0:
        return {"offstack_recall": 0.0, "stackonly_recall": 0.0, "n_offstack": 0}
    reached = seed_hit = 0
    for case in off:
        rn = _short(case.regressor_node)
        if rn and rn in _nodes_in_dossier(_dossier(results, case.uuid)):
            reached += 1
        if rn and rn in {_short(n) for n in case.seed_nodes}:
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
        named_ok = bool(dossier.candidate) and _short(dossier.candidate.node) == _short(case.regressor_node)
        cited = bool(_nodes_in_dossier(dossier) or (dossier.call_path and dossier.call_path.edges))
        diff_ok = True if diff_checker is None else bool(diff_checker(case, dossier))
        if named_ok and cited and diff_ok:
            correct += 1
    return {"evidence_precision": correct / len(strong), "n_strong": len(strong)}


def abstain_calibration(cases, results):
    conf = {
        "strong_findable": 0, "strong_unfindable": 0,
        "abstain_findable": 0, "abstain_unfindable": 0,
    }
    for case in cases:
        strong = _is_strong(_dossier(results, case.uuid))
        findable = _findable(case)
        key = ("strong" if strong else "abstain") + (
            "_findable" if findable else "_unfindable"
        )
        conf[key] += 1
    return conf


def compute_metrics(cases, results, sweep_config=None, corpus_hash="", diff_checker=None):
    off = offstack_recall(cases, results)
    ev = evidence_correctness(cases, results, diff_checker=diff_checker)
    return Metrics(
        offstack_recall=off["offstack_recall"],
        stackonly_recall=off["stackonly_recall"],
        evidence_precision=ev["evidence_precision"],
        abstain_calibration=abstain_calibration(cases, results),
        n_cases=len(cases),
        n_offstack=off["n_offstack"],
        n_strong=ev["n_strong"],
        corpus_hash=corpus_hash,
        sweep_config=sweep_config or {},
    )


def compare_to_baseline(metrics, baseline_path):
    """Compare against a committed baseline metrics.json; return a pass/regress
    summary keyed on the two headline metrics."""
    try:
        with open(baseline_path) as handle:
            base = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {"status": "no-baseline", "deltas": {}}
    deltas = {
        "offstack_recall": metrics.offstack_recall - base.get("offstack_recall", 0.0),
        "evidence_precision": metrics.evidence_precision - base.get("evidence_precision", 0.0),
    }
    regressed = any(v < -1e-9 for v in deltas.values())
    return {"status": "regress" if regressed else "pass", "deltas": deltas}
