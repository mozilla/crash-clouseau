# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Pure Pydantic types for the eval harness (#13).

Kept in their own module (rather than in corpus.py/metrics.py per the plan sketch)
so metrics/runner can import them without pulling corpus.py's libmozdata/DB deps."""
from __future__ import annotations

from pydantic import BaseModel, Field


class CorpusCase(BaseModel):
    uuid: str
    signature: str = ""
    # Ground truth. ``regressed_by`` gives regressor BUG ids (authoritative, alias-free);
    # their landing changesets are resolved from Bugzilla comments. A run "hits" if its
    # dossier references ANY regressor node OR bug (node matching is best-effort — the
    # dossier cites mozilla-central/searchfox revs that need not string-equal the
    # autoland landing revs, so the bug match is the robust signal).
    regressor_node: str = ""                                # first resolved node (compat)
    regressor_nodes: list[str] = Field(default_factory=list)  # all resolved landing revs
    regressor_bug: int | None = None                        # first regressor bug (compat)
    regressor_bugs: list[int] = Field(default_factory=list)   # regressed_by (authoritative)
    regressor_author: str = ""
    # All authors of the true regressor's changesets — for the PERSON-LEVEL ("silver nugget")
    # metric: a lead that blames the wrong changeset but the same author as the true regressor
    # is still a good needinfo target (the pivoted goal = get the right person investigating).
    regressor_authors: list[str] = Field(default_factory=list)
    channel: str = "nightly"
    crash_json_path: str | None = None
    seed_nodes: list[str] = Field(default_factory=list)  # the stack-only candidate set
    # Scored seed candidates (node/bug/backedout/score) frozen at mine time, so the eval
    # rerun feeds the agent the same KIND of seed prod's build_seed does (closes the
    # "no seed candidates" fidelity gap).
    candidates: list[dict] = Field(default_factory=list)
    on_stack_label: bool | None = None  # filled by the labeler; None = unlabeled
    # Phase-2 calibration (study-fixture corpus). ``is_offstack`` is the leak-free RUN-time
    # split (no candidate touched a stack file), computed by the adapter to mirror
    # ``build_seed`` — distinct from ``on_stack_label`` (ground truth, metrics-only).
    # ``is_negative`` marks a culprit-absent window (regressor removed): any non-abstain on it
    # is a FALSE-INVESTIGATE, and its regressor sets are empty so ``_hit`` is always False.
    # ``pin_rev`` pins blame/source reads to the crash build rev (leak-free).
    is_offstack: bool | None = None
    is_negative: bool = False
    pin_rev: str = ""
    # Landings that were RESOLVED for the regressor bug(s) but rejected as labels, with the
    # reason (landed after the crash build / auto-format / wrong bug / not on hg). Kept so a
    # surprising label is explainable and so the labeller's own accuracy stays auditable — an
    # audit of corpus_ship found 27% of resolved landing nodes unusable.
    label_rejects: list[dict] = Field(default_factory=list)


class SweepConfig(BaseModel):
    """A tuning sweep: role->model overrides layered onto the base agent.llm block,
    plus an optional report-threshold policy. Empty = use the configured defaults."""

    name: str = "default"
    roles: dict[str, str] = Field(default_factory=dict)   # role -> model short name
    principal_model: str | None = None
    effort: str | None = None  # applied to principal AND every role when set (e.g. "max")
    # Report-THRESHOLD policy applied by the eval runner (``runner._apply_report_thresholds``):
    # ``{decision -> min 0-100 rung score}`` — a reported verdict scoring below its minimum is
    # downgraded to abstain, so a sweep can measure a report threshold (recall / false-investigate
    # / person-precision) without re-running the agent. Keys: ``"lead"`` / ``"strong-evidence"``,
    # with ``"report"`` as the fallback for any reported verdict. Empty = report everything.
    confidence_thresholds: dict[str, float] = Field(default_factory=dict)


class Metrics(BaseModel):
    offstack_recall: float = 0.0
    stackonly_recall: float = 0.0
    evidence_precision: float = 0.0
    lead_precision: float = 0.0       # of leads, share that reference the true regressor
    abstain_calibration: dict = Field(default_factory=dict)  # {strong,lead,abstain}x{find,unfind}
    n_cases: int = 0
    n_offstack: int = 0
    n_strong: int = 0
    n_lead: int = 0
    n_errored: int = 0  # runs that failed (max_turns/timeout/exception), NOT deliberate abstains
    # Precision-first (Phase-2): over the culprit-absent NEGATIVE arm, share reported as a
    # lead/strong (a false-investigate). The metric the report threshold must keep near ~0.
    false_investigate_rate: float = 0.0
    n_negative: int = 0
    n_false_investigate: int = 0
    # Person-level scoring (Phase-2 pivot: "get the right person investigating"). Over reported
    # (lead/strong) cases, the share reaching the true regressor's AUTHOR — by the exact
    # changeset/bug OR the same-author 'silver nugget'. 0 unless the eval was scored with an
    # author resolver (offline test runs leave these at their defaults).
    person_precision: float = 0.0
    n_person_hit: int = 0
    n_reported: int = 0
    # Cost/usage, aggregated across the re-run (per-case avg + total). Lets a sweep or a
    # prompt change weigh a quality delta against its cost delta (a drop to a cheaper
    # model, or a heavier prompt, both show up here).
    mean_cost_usd: float = 0.0
    total_cost_usd: float = 0.0
    mean_output_tokens: float = 0.0
    mean_input_tokens: float = 0.0
    corpus_hash: str = ""
    sweep_config: dict = Field(default_factory=dict)
