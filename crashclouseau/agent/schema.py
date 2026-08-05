# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Dossier schema & strong-evidence contract (plan #03).

Pure-data Pydantic models + the every-claim-has-a-citation validation rule, plus
the parse/validate helpers the #02 substrate calls on the principal's best-effort
trailing ```json handoff (mirrors bugbug's ``parse_plan``). There is no strict
JSON-schema exported to any API: structured output is validated *after* parse, and
``parse_and_validate`` never lets an uncited/hallucinated claim survive: on a
validation failure it SALVAGES — keeps the sub-objects (and verdict) that validate,
drops the ones that don't — so a single malformed optional field can't discard a
properly-cited verdict; a verdict that fails its own grounding rules is still forced
to abstain. Only stdlib + pydantic + ``config``/``logger`` are imported here (no
db/SDK/network).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import (
    BaseModel,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from crashclouseau import config
from crashclouseau.logger import logger


# --------------------------------------------------------------------------- #
# Enums (str-valued so they serialize cleanly to/from JSON)
# --------------------------------------------------------------------------- #
class FailureClass(str, Enum):
    uaf = "uaf"
    null_deref = "null_deref"
    assertion = "assertion"
    oob = "oob"
    shutdownhang = "shutdownhang"
    other = "other"


class Decision(str, Enum):
    strong_evidence = "strong-evidence"
    lead = "lead"
    abstain = "abstain"


class Confidence(str, Enum):
    low = "low"
    medium = "medium"
    # ``probable`` sits between ``medium`` and ``high``. Under the worth-investigating pivot
    # a lead MAY self-assert up to ``probable`` (a strong worth-investigating estimate for a
    # coherent, well-linked clue), AND a deterministic corroboration (computed outside the
    # LLM — e.g. a fault-address == struct-field-offset match, in
    # ``orchestrator._apply_corroboration_gate``) also raises a bare lead to ``probable``.
    # Only ``high`` stays reserved: reachable solely by a VERIFIED strong-evidence chain (a
    # lead's ``high`` is clamped one notch to ``probable`` by ``_consistency_rule``).
    probable = "probable"
    high = "high"


class SkepticStatus(str, Enum):
    passed = "pass"
    failed = "fail"
    unverifiable = "unverifiable"


class CitationKind(str, Enum):
    searchfox = "searchfox"
    diff_line = "diff_line"
    stack_frame = "stack_frame"
    struct_layout = "struct_layout"


# Categorical confidence -> numeric, for the abstain_below_confidence floor.
# NOTE: config ``abstain_below_confidence`` is deliberately set to ``high``'s score
# (0.85) so the floor + strict ``<`` in Verdict._consistency_rule enforce system.md's
# "strong-evidence REQUIRES confidence:high". These two move together: lowering this
# score or the config floor re-admits medium; raising the floor above 0.85 makes
# strong-evidence impossible.
CONFIDENCE_SCORE: dict[Confidence, float] = {
    Confidence.low: 0.25,
    Confidence.medium: 0.5,
    Confidence.probable: 0.70,
    Confidence.high: 0.85,
}


# --------------------------------------------------------------------------- #
# Citations (a discriminated union on ``kind``)
# --------------------------------------------------------------------------- #
class SearchfoxCitation(BaseModel):
    kind: Literal["searchfox"] = "searchfox"
    permalink: str
    symbol_id: str
    repo: str
    rev: str = ""


class DiffLineCitation(BaseModel):
    kind: Literal["diff_line"] = "diff_line"
    node: str
    filename: str
    line: int
    side: Literal["added", "deleted", "context"]
    content: str


class StackFrameCitation(BaseModel):
    kind: Literal["stack_frame"] = "stack_frame"
    uuid: str
    stackpos: int
    filename: str
    function: str
    line: int
    # NOT required: a stack frame legitimately has no changeset when the frame is not
    # attributable (no source file, or a file no changeset in the window touched) —
    # ``CrashFrame.node`` above is already ``""`` for exactly that case, and the sibling
    # ``AreaExpert.node`` defaults too. Required, it was a whole-dossier grenade: on
    # dossier 5748 the model omitted it on two frames cited inside
    # ``verdict.consistency.citations[]``, ``_salvage`` dropped the entire verdict, and a
    # correct $1.75 analysis became "dossier validation failed (verdict unusable)". Safe
    # to default because NO GATE reads a citation's ``node`` (every gate reads
    # ``candidate.node``); the one consumer is the offline ``eval.metrics._nodes_in_dossier``
    # node set, which already does ``getattr(cite, "node", None)`` and skips a falsy value —
    # and it could not have seen a nodeless citation before this default existed either, so
    # no metric moves. This also un-grenades ``DataFlowHypothesis.crash_site`` below, where a
    # nodeless frame used to drop the whole ``data_flow`` sub-object via ``_salvage``.
    node: str = ""


class StructLayoutCitation(BaseModel):
    """A deterministic C++ memory-layout fact from ``mcp__searchfox__field_layout``:
    field ``field`` of type ``type_name`` sits at byte ``offset``. Its purpose is to
    make a null/small-address fault VERIFIABLE — the corroboration gate
    (``orchestrator``) confirms ``offset`` equals the crash's fault address outside
    the LLM, so this can raise a lead's confidence without trusting model prose."""

    kind: Literal["struct_layout"] = "struct_layout"
    type_name: str
    field: str = ""
    offset: int
    repo: str = "mozilla-central"


Citation = Annotated[
    Union[SearchfoxCitation, DiffLineCitation, StackFrameCitation, StructLayoutCitation],
    Field(discriminator="kind"),
]


class Cited(BaseModel):
    """Base for any claim-bearing model: it must carry at least
    ``config.get_min_citations_per_claim()`` citations or validation fails."""

    citations: list[Citation] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_citations(self):
        minimum = config.get_min_citations_per_claim()
        if len(self.citations) < minimum:
            raise ValueError(
                f"{type(self).__name__} needs >= {minimum} citation(s), "
                f"got {len(self.citations)}"
            )
        return self


# --------------------------------------------------------------------------- #
# Content models
# --------------------------------------------------------------------------- #
class CrashFrame(BaseModel):
    stackpos: int
    function: str = ""
    filename: str = ""
    line: int = 0
    node: str = ""
    inlines: list[str] = Field(default_factory=list)
    trust: str | None = None

    @field_validator("function", "filename", "node", mode="before")
    @classmethod
    def _none_to_empty(cls, v):
        """Symbolication can emit a null ``function``/``filename`` for an opaque frame
        (e.g. macOS ``os_unfair_lock``, JIT/stub frames). These are ``str`` fields, so a
        literal ``None`` would fail validation and — since ``CrashBrief.frames`` is
        validated as a whole in ``_salvage`` — drop the ENTIRE crash brief (every frame)
        and force a false abstain. Coerce ``None`` -> ``""`` so a single opaque frame is
        kept (empty) instead of discarding the crash context."""
        return "" if v is None else v


class CrashBrief(BaseModel):
    uuid: str
    signature: str = ""
    failure_class: FailureClass = FailureClass.other
    faulting_address: str | None = None
    moz_crash_reason: str | None = None
    reason: str | None = None
    crashing_thread: int = 0
    frames: list[CrashFrame] = Field(default_factory=list)
    phc_kind: str | None = None
    phc_alloc_stack: bool | None = None
    phc_free_stack: bool | None = None
    async_shutdown_timeout: str | None = None


class Candidate(BaseModel):
    node: str
    bug: int | None = None
    author: str = ""
    channel: str = ""
    pushdate: datetime | None = None
    # The git counterpart of ``node`` (Firefox is in both forges since the hg->git migration),
    # resolved once in the worker and stored so the bug comment can link the changeset on
    # GitHub without a page render paying for hg's 8-13s json-rev lookup. "" = not resolved
    # (old dossiers, or hg had no counterpart) -> the comment simply omits the gh link.
    git_commit: str = ""
    # MODEL-SUPPLIED and ambiguous: the handoff shape no longer asks for it, but old dossiers
    # carry it and the model may still volunteer one. Two different predicates have been written
    # here — "this changeset IS a backout commit" (what the seed means, from
    # ``pushlog.is_backed_out(desc)``) and "this changeset WAS backed out" (what the agent's
    # ``changeset`` tool prints as "BACKED OUT BY"). Never gate on it; use ``backedout_by``.
    backedout: bool = False
    # The sha that backed this candidate out, resolved by the ORCHESTRATOR against hg
    # (``sigage.backedout_by_for_node``) and stripped from the model's handoff so it cannot be
    # injected. "" = not backed out, or never resolved (offline / lookup failed) — the gate
    # only ever fires on a non-empty value, so unknown timing never suppresses a verdict.
    backedout_by: str = ""
    # The MIRROR predicate, and the one that actually bit us: does this changeset's own hg
    # description say it IS ITSELF a backout/revert (``pushlog.is_backed_out``)? Also
    # ORCHESTRATOR-authored and stripped from the handoff — ``backedout`` above is the model's
    # unreliable guess at the same thing, and the two must not be confusable.
    is_backout: bool = False
    # When ``is_backout``: the changeset it backs out, IF that changeset landed in the SAME
    # push (``sigage.same_push_backout_target``). Non-empty means the tree's content never
    # differed, so the candidate provably changed nothing -> the verdict is suppressed.
    # "" = no same-push target, or we could not find out (never suppress on unknown).
    backout_of_same_push: str = ""
    seed_score: int | None = None
    changed_functions: list[str] = Field(default_factory=list)


class CallEdge(Cited):
    caller_symbol: str
    callee_symbol: str
    # The searchfox query the edge came from. Kept a free-form str, not a strict
    # Literal["calls-from","calls-to","calls-between","define"]: the model routinely
    # annotates it (e.g. "calls-from (virtual)") and a narrow Literal then discards
    # the WHOLE dossier via parse_and_validate -> a false abstain on an otherwise good
    # run. Descriptive only (citations are the grounding gate), so a str is safe.
    via: str = ""


class CallPath(BaseModel):
    edges: list[CallEdge] = Field(default_factory=list)
    from_stackpos: int | None = None
    to_symbol: str = ""


class DiffHunk(Cited):
    node: str
    filename: str
    header: str = ""
    # Supplementary changed-line detail. Kept an untyped list, NOT
    # list[DiffLineCitation]: the model often emits bare strings (e.g. the raw diff
    # text) here, and a strict item type made parse_and_validate discard the WHOLE
    # dossier -> a false abstain (same failure mode as `via`/`operation`). Grounding
    # is enforced by the Cited `citations` requirement; well-formed diff_line dicts
    # here are still picked up by the code view, bare strings are ignored.
    lines: list = Field(default_factory=list)


class DataFlowHypothesis(Cited):
    summary: str
    object_name: str = ""
    # Free-form mechanism label (e.g. free / mutate / null_deref / uaf / oob /
    # double_free / uninitialized / other). Kept a plain str, not a strict enum:
    # it is descriptive, not grounding-critical (citations are the anti-hallucination
    # gate), and a narrow Literal caused false-abstains on real model output.
    operation: str = "other"
    crash_site: StackFrameCitation | None = None


class SkepticResult(BaseModel):
    # Only ``status`` is strict — ``claim_ref``/``citations`` are intentionally lax
    # (str default, untyped list; same rationale as DiffHunk.lines / CallEdge.via).
    # The binding skeptic veto (Dossier._skeptic_veto) keys on ``status``, so a
    # malformed supporting citation or a missing claim_ref must NOT make a ``fail``
    # result fail validation: parse_and_validate's per-item salvage would drop it
    # and silently bypass the veto, letting a refuted strong-evidence verdict reach
    # the apply UI.
    status: SkepticStatus
    claim_ref: str = ""
    note: str = ""
    citations: list = Field(default_factory=list)


class Claim(Cited):
    """A cited free-text claim (the ``mechanism``/``consistency`` of a verdict)."""

    statement: str = ""


class AreaExpert(BaseModel):
    """A developer who recently worked in the crashing area — someone to ASK, not
    necessarily the cause. Computed deterministically OUTSIDE the LLM (from local
    ``Node.hgauthor``), so it is NOT a ``Cited`` claim and rides the dossier for any
    verdict (including abstain). ``reason`` explains why they were surfaced."""

    name: str = ""
    email: str = ""
    nick: str = ""
    node: str = ""            # the changeset that placed them in the area
    bug: int | None = None
    reason: str = ""


class SecondOpinion(BaseModel):
    """A blind second reviewer's INDEPENDENT conclusion (#SO).

    Produced by ``agent.second_opinion.run_second_opinion`` — a fresh Opus-4.8 agent
    given the crash (and, in verify mode, only the candidate changeset) with NONE of the
    first pipeline's reasoning, so its read is an unbiased plausibility signal. It is set
    programmatically on the dossier by ``orchestrator._fold_second_opinion`` (NOT parsed
    from a claim block, so it is not a ``Cited`` model and carries no citations); it rides
    the dossier JSONB payload and is additive + backward compatible (older dossiers omit it
    and validate to ``None``). ``mode`` is ``"verify"`` when a candidate was given (then
    ``corroborates`` is the boost/refute signal) and ``"mechanism"`` otherwise (an
    independent mechanism for a candidate-less lead; ``corroborates`` stays ``None``)."""

    mode: str = ""                    # "verify" (had a candidate) | "mechanism" (no candidate)
    corroborates: bool | None = None  # verify: does the candidate plausibly cause the crash?
    confidence: str = "low"           # the agent's confidence in ITS OWN conclusion
    mechanism: str = ""               # its independent mechanism / reasoning (concise)
    refutation: str = ""              # verify: the concrete reason the candidate can't be it
    # This pass's own API cost, set programmatically (NOT from the agent's JSON) so the
    # validation run can measure the SO's price separately from the primary triage cost.
    cost_usd: float | None = None


class Verdict(BaseModel):
    decision: Decision
    confidence: Confidence = Confidence.low
    # Phase-2 calibration: the empirical, calibrated probability this reported verdict is
    # WORTH INVESTIGATING (i.e. a lead a human should pick up), read off the fitted
    # rung -> P table (``eval.calibrate``) by ``orchestrator.apply_deterministic_gates`` AFTER
    # all gates settle the FINAL confidence rung. ``None`` when no calibration table is
    # configured (the pre-calibration default) or on an abstain — so the raw ``confidence``
    # rung stays the only signal until a table is fit + wired. Additive + backward compatible:
    # older persisted dossiers omit it and validate to ``None``; it rides the dossier JSONB
    # payload, so surfacing it needs no DB migration.
    p_worth_investigating: float | None = None
    needinfo_draft: str | None = None
    abstain_reason: str | None = None
    mechanism: Claim | None = None
    consistency: Claim | None = None

    @model_validator(mode="after")
    def _consistency_rule(self):
        if self.decision == Decision.strong_evidence:
            floor = config.get_abstain_below_confidence()
            if CONFIDENCE_SCORE[self.confidence] < floor:
                raise ValueError(
                    "strong-evidence requires confidence at/above the "
                    f"configured floor ({floor})"
                )
            if self.mechanism is None or not self.mechanism.citations:
                raise ValueError("strong-evidence requires a cited mechanism claim")
            if self.consistency is None or not self.consistency.citations:
                raise ValueError("strong-evidence requires a cited consistency claim")
        elif self.decision == Decision.lead:
            # A lead's confidence is a WORTH-INVESTIGATING estimate (how likely this is a
            # real clue worth a human's time), NOT a claim of proof — so the model MAY
            # self-assert up to `probable` (0.70) for a coherent, cited, well-linked lead
            # (e.g. a mechanism hypothesis + a domain/enablement link). Only `high` (0.85)
            # stays reserved for a fully VERIFIED chain, i.e. `strong-evidence`; NO lead
            # reaches `high` (``_apply_corroboration_gate`` raises a corroborated lead only to
            # `probable`, not `high`), so a lead's `high` is clamped one notch to `probable`.
            # The noise guard is NOT this clamp: it is the abstain decision + the
            # (noise-focused) skeptic — a skeptic `fail` demotes a lead to abstain, and an
            # anchorless lead is demoted too (``_skeptic_veto``) — which is what protects
            # against sending people after noise.
            if self.confidence == Confidence.high:
                self.confidence = Confidence.probable
        elif self.decision == Decision.abstain:
            if not self.abstain_reason:
                raise ValueError("abstain requires an abstain_reason")
            if self.needinfo_draft:
                raise ValueError("abstain must not carry a needinfo_draft")
        return self


class Dossier(BaseModel):
    schema_version: int = Field(default_factory=config.get_agent_schema_version)
    crash: CrashBrief | None = None
    candidate: Candidate | None = None
    call_path: CallPath | None = None
    hunks: list[DiffHunk] = Field(default_factory=list)
    data_flow: DataFlowHypothesis | None = None
    skeptic: list[SkepticResult] = Field(default_factory=list)
    area_experts: list[AreaExpert] = Field(default_factory=list)
    # Deterministic corroboration flags computed OUTSIDE the LLM (in ``orchestrator``)
    # from the crash + dossier — e.g. ``{"fault_address_offset_match": true,
    # "fault_offset": 8, "fault_field": "mLength", "fault_type": "...nsTStringRepr"}``.
    # Drives the corroboration gate (lead -> probable) and the UI chips. Not a Cited
    # claim; rides any verdict. Empty on older dossiers (backward compatible).
    corroborations: dict = Field(default_factory=dict)
    verdict: Verdict | None = None
    # The RAW, pre-gate verdict, snapshotted by ``orchestrator.apply_deterministic_gates``
    # BEFORE any deterministic gate runs. Without it the gates' NET effect is unauditable
    # after the fact: a second-opinion clamp to ``medium`` looks identical to a no-op on a
    # lead that was already ``medium``, so "how often does the SO actually MOVE a verdict?"
    # cannot be answered from persisted data. Computed outside the LLM (stripped at the parse
    # boundary like ``corroborations``/``second_opinion``); additive + backward compatible.
    raw_verdict: Verdict | None = None
    # Blind second-opinion re-analysis (#SO), attached programmatically by
    # ``orchestrator._fold_second_opinion`` for a REPORTED lead when the pass is enabled.
    # Additive + backward compatible: older persisted dossiers omit it and validate to
    # ``None``; it rides the dossier JSONB payload, so surfacing it needs no DB migration.
    second_opinion: SecondOpinion | None = None
    # WHY ``second_opinion`` is (or isn't) set — the SO is best-effort, so without this a
    # prod-only break is INVISIBLE: a failed run and an ineligible verdict both leave
    # ``second_opinion`` null and cannot be told apart. One of ``ok`` / ``failed`` /
    # ``skipped_disabled`` / ``skipped_no_verdict`` / ``skipped_abstain`` /
    # ``skipped_backedout`` / ``skipped_backout_netzero`` / ``skipped_below_threshold``, or
    # ``None`` when the gate never ran
    # (the offline eval runner, and every dossier written before this field existed).
    second_opinion_status: str | None = None
    created: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def _has_lead_anchor(self) -> bool:
        """True when something *cited* still points a human at an area — a candidate
        changeset, a cited diff hunk, or a cited call-path edge. Enough to hand over a
        lead even when the mechanism isn't verified end to end."""
        if self.candidate is not None and self.candidate.node:
            return True
        if any(h.citations for h in self.hunks):
            return True
        if self.call_path and any(e.citations for e in self.call_path.edges):
            return True
        return False

    def _soft_lead_draft(self) -> str | None:
        """A soft, non-accusatory needinfo draft for a DOWNGRADED lead that does NOT
        assert the (skeptic-refuted) mechanism — it names the candidate and asks for
        help. Returns None when there is no candidate to name (the panel then shows the
        lead without a pre-written draft)."""
        c = self.candidate
        if c is None or not c.node:
            return None
        bug = " (bug {})".format(c.bug) if c.bug else ""
        return (
            "A skeptic review could not confirm the mechanism, but changeset {}{} looks "
            "like a plausible lead for this crash. Could you help figure out whether it "
            "is related — or point us to who could? This is not an accusation.".format(
                c.node, bug
            )
        )

    @model_validator(mode="after")
    def _skeptic_veto(self):
        """Skeptic ladder + lead-anchor gate (on Dossier, so it can see the skeptic
        results alongside candidate/hunks/call_path). Mutates in place and NEVER raises
        — a raising validator would fall through ``parse_and_validate``'s salvage
        rebuild to a bare abstain and discard salvaged evidence.

        (1) A skeptic ``fail`` on STRONG-EVIDENCE refutes the *mechanism*, but a cited
        candidate/hunk/edge may still be a useful LEAD — so a ``fail`` DOWNGRADES
        strong-evidence to ``lead`` when such an anchor stands (dropping the now-stale
        assertive draft for a SOFT one that doesn't restate the refuted mechanism), and
        only collapses to ``abstain`` when nothing cited remains. ``unverifiable`` is
        advisory (a searchfox hole), not a ``fail``.

        (1b) A skeptic ``fail`` on a MODEL-emitted LEAD demotes it to ABSTAIN. Under the
        worth-investigating pivot the skeptic is the NOISE guardrail: ``fail`` means the
        claim is contradicted or the candidate is demonstrably unrelated (noise), NOT merely
        unproven (that is ``unverifiable``, which KEEPS the lead). We do not push a lead the
        skeptic flagged as noise — this is the teeth the reoriented skeptic needs on leads.

        (2) ANY surviving lead must carry a cited anchor; an anchorless lead is demoted to
        abstain (nothing to hand a human)."""
        v = self.verdict
        if v is None:
            return self
        failed = [s.claim_ref for s in self.skeptic
                  if s.status == SkepticStatus.failed]
        # (1) Skeptic ladder on a strong-evidence verdict.
        if v.decision == Decision.strong_evidence and failed:
            detail = "skeptic refuted the mechanism (failed: {})".format(
                ", ".join(failed) or "?"
            )
            if self._has_lead_anchor():
                self.verdict = Verdict(
                    decision=Decision.lead,
                    confidence=Confidence.medium,
                    needinfo_draft=self._soft_lead_draft(),
                    mechanism=v.mechanism,
                    consistency=v.consistency,
                )
            else:
                self.verdict = Verdict(
                    decision=Decision.abstain,
                    confidence=Confidence.low,
                    abstain_reason=detail + "; no cited candidate/hunk/edge remains",
                )
        # (1b) A skeptic fail on a model-emitted lead = noise -> abstain (guardrail teeth).
        elif v.decision == Decision.lead and failed:
            self.verdict = Verdict(
                decision=Decision.abstain,
                confidence=Confidence.low,
                abstain_reason="skeptic flagged this lead as noise / unrelated "
                               "(failed: {})".format(", ".join(failed) or "?"),
            )
        # (2) A lead (from the ladder OR emitted directly) needs a cited anchor.
        if self.verdict.decision == Decision.lead and not self._has_lead_anchor():
            self.verdict = Verdict(
                decision=Decision.abstain,
                confidence=Confidence.low,
                abstain_reason="lead has no cited candidate/hunk/edge anchor; "
                               "nothing to act on",
            )
        return self


# Per-role fragment models validated from a role's parsed trailing-```json block.
_ROLE_FRAGMENTS: dict[str, type[BaseModel]] = {
    "crash-interpreter": CrashBrief,
    "call-graph-explorer": CallPath,
    "data-flow-tracer": DataFlowHypothesis,
}
# Roles whose trailing block is a LIST, validated per item: patch-scout emits one hunk
# object per candidate; skeptic emits one verification object per claim it re-checked
# (matching Dossier.skeptic, which is list[SkepticResult]).
_DIFF_HUNK_LIST = TypeAdapter(list[DiffHunk])
_SKEPTIC_LIST = TypeAdapter(list[SkepticResult])


# --------------------------------------------------------------------------- #
# Parse / validate helpers (the SDK-path anti-hallucination boundary)
# --------------------------------------------------------------------------- #
# Same shape as bugbug's frontend_triage._JSON_BLOCK / parse_plan: last block wins.
_JSON_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_last_json_block(text: str | None):
    """Return the LAST ```json {...} ``` object in *text*, or None on no
    match / invalid JSON (never raises)."""
    if not text:
        return None
    matches = _JSON_BLOCK.findall(text)
    if not matches:
        return None
    try:
        data = json.loads(matches[-1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


# Common LLM spelling variants for the citation discriminator (``kind``) and the diff
# ``side``, keyed lowercased -> canonical enum token. The model routinely writes the
# prose spelling ("stack-frame" with a hyphen; a "removed" diff line) instead of the
# schema token, and a SINGLE such citation inside the verdict's own mechanism/
# consistency claims otherwise makes ``_salvage`` drop the whole (correct, fully-cited)
# verdict and force a FALSE abstain — observed live downgrading a genuine lead
# (`stack-frame`/`removed`), and the `stack-frame` variant has recurred across runs.
# Normalizing these unambiguous variants (incl. case) at the parse boundary keeps the
# anti-hallucination guarantee — we only fix spelling of an EXISTING citation, never
# invent one; an unknown value passes through unchanged and still fails validation.
_KIND_ALIASES = {
    "searchfox": "searchfox", "search-fox": "searchfox", "search_fox": "searchfox",
    "diff_line": "diff_line", "diff-line": "diff_line", "diffline": "diff_line",
    "diff line": "diff_line",
    "stack_frame": "stack_frame", "stack-frame": "stack_frame",
    "stackframe": "stack_frame", "stack frame": "stack_frame",
    "struct_layout": "struct_layout", "struct-layout": "struct_layout",
    "structlayout": "struct_layout", "struct layout": "struct_layout",
    "field_layout": "struct_layout", "field-layout": "struct_layout",
    "fieldlayout": "struct_layout", "field layout": "struct_layout",
}
_SIDE_ALIASES = {
    "added": "added", "add": "added", "addition": "added",
    "deleted": "deleted", "removed": "deleted", "remove": "deleted",
    "deletion": "deleted", "del": "deleted",
    "context": "context", "unchanged": "context", "ctx": "context",
    "unmodified": "context",
    # A hunk-header / structural diff line: the model labels a ``@@ ... @@`` header (or an
    # otherwise non-line citation) with ``side:"meta"``/"header"/"hunk", none of which is a
    # real added/deleted/context source line. Map to `context` (a valid, non-behavior-
    # asserting pointer, same rationale as blame/history) so ONE such citation in a
    # verdict's mechanism/consistency claim can't force-abstain an otherwise-correct lead
    # via `_salvage` (observed as a false abstain during canary validation).
    "meta": "context", "header": "context", "hunk": "context",
    "hunk_header": "context", "hunk-header": "context",
    # A line the model pulled from the blame/history tools (not a diff) is an EXISTING
    # source line, i.e. `context` in diff terms. The model routinely mislabels its
    # `side` as "history_blame"/"blame"/"history"; left unmapped, one such citation in
    # the verdict's mechanism force-abstains an otherwise-correct lead via salvage
    # (observed live on ab3238a5). Mapping to `context` keeps it a valid, non-behavior-
    # asserting pointer — it can't inflate the refactor-blame false positive.
    "history_blame": "context", "blame": "context", "history": "context",
    "history-blame": "context",
}


def _normalize_citations(obj):
    """Rewrite common citation spelling variants (``kind``/``side``) to the canonical
    enum tokens, recursively and in place. Keys strictly on the field names ``kind``
    and ``side``, which in this schema appear ONLY on citations, so no other field is
    touched (``phc_kind`` etc. are left alone). Case-insensitive; canonical values pass
    through unchanged; an unrecognized value is left as-is (and will legitimately fail
    validation). Returns ``obj`` for chaining."""
    if isinstance(obj, dict):
        k = obj.get("kind")
        if isinstance(k, str):
            obj["kind"] = _KIND_ALIASES.get(k.strip().lower(), k)
        s = obj.get("side")
        if isinstance(s, str):
            obj["side"] = _SIDE_ALIASES.get(s.strip().lower(), s)
        for v in obj.values():
            _normalize_citations(v)
    elif isinstance(obj, list):
        for v in obj:
            _normalize_citations(v)
    return obj


def validate_dossier(obj: dict) -> Dossier:
    """Strict gate: ``Dossier.model_validate`` — raises ``ValidationError`` on any
    uncited claim or malformed field."""
    return Dossier.model_validate(obj)


def _abstain(reason: str) -> Dossier:
    return Dossier(verdict=Verdict(decision=Decision.abstain, abstain_reason=reason))


# Top-level Dossier sub-objects salvaged independently (singular fields).
_SINGLE_FIELDS: dict[str, type[BaseModel]] = {
    "crash": CrashBrief,
    "candidate": Candidate,
    "data_flow": DataFlowHypothesis,
    "verdict": Verdict,
}
# ...and the Dossier list fields, salvaged per item.
_LIST_FIELDS: dict[str, type[BaseModel]] = {
    "hunks": DiffHunk,
    "skeptic": SkepticResult,
}


def _salvage(obj: dict):
    """Best-effort per-field validation: keep the sub-objects (and per-item list
    entries / call-path edges) that validate, drop the ones that don't. Nothing
    uncited SURVIVES — it is dropped, not kept — so the anti-hallucination guarantee
    holds while a single malformed optional field no longer discards the whole
    (properly-cited) verdict. Returns ``(kwargs, dropped)``."""
    kwargs: dict = {}
    dropped: list = []
    if isinstance(obj.get("schema_version"), int):
        kwargs["schema_version"] = obj["schema_version"]

    for name, model in _SINGLE_FIELDS.items():
        val = obj.get(name)
        if val is None:
            continue
        try:
            kwargs[name] = model.model_validate(val)
        except ValidationError:
            dropped.append(name)

    cp = obj.get("call_path")
    if isinstance(cp, dict):
        edges = []
        for i, edge in enumerate(cp.get("edges") or []):
            try:
                edges.append(CallEdge.model_validate(edge))
            except ValidationError:
                dropped.append("call_path.edges[{}]".format(i))
        try:
            kwargs["call_path"] = CallPath(
                edges=edges,
                from_stackpos=cp.get("from_stackpos"),
                to_symbol=cp.get("to_symbol") or "",
            )
        except ValidationError:
            dropped.append("call_path")

    for name, model in _LIST_FIELDS.items():
        items = obj.get(name)
        if not isinstance(items, list):
            continue
        kept = []
        for i, item in enumerate(items):
            try:
                kept.append(model.model_validate(item))
            except ValidationError:
                dropped.append("{}[{}]".format(name, i))
        if kept:
            kwargs[name] = kept

    return kwargs, dropped


def parse_and_validate(result: str | dict) -> Dossier:
    """Validate the best-effort handoff #02 parses from ``ResultMessage.result``.
    Never raises into the caller. On a missing/malformed ```json block or invalid
    JSON, returns an abstain Dossier. On a schema-validation failure, SALVAGES: keep
    the sub-objects/verdict that validate, drop the ones that don't — so one bad
    optional field can't discard a properly-cited verdict. A verdict that is absent
    or fails its own grounding rules (uncited strong-evidence, confidence below the
    floor, ...) is forced to abstain, but any salvaged evidence is still attached."""
    obj = result if isinstance(result, dict) else _extract_last_json_block(result)
    if obj is None:
        return _abstain("no parseable ```json block in the agent result")
    # Strip the fields that are computed OUTSIDE the LLM — ``corroborations`` (deterministic
    # gate flags set in ``orchestrator``), ``second_opinion`` + ``second_opinion_status`` (set
    # by the blind-SO fold) and ``raw_verdict`` (the pre-gate snapshot) — so the primary model
    # can NEVER populate them from its own handoff JSON. Left in, an injected value would spoof
    # a deterministic corroboration chip, suppress the SO boost via a fake
    # ``downgraded_from_strong``, masquerade as an independent second opinion, forge an "ok"
    # status over a failed pass, or fake a pre-gate verdict to make the gates look like a no-op.
    # The orchestrator overwrites all of them post-parse; this makes the strict path match the
    # salvage path (which never copies them) and preserves the blind-independent guarantee.
    if isinstance(obj, dict):
        obj.pop("corroborations", None)
        obj.pop("second_opinion", None)
        obj.pop("second_opinion_status", None)
        obj.pop("raw_verdict", None)
        # Same rule, one level down: ``candidate.backedout_by`` is resolved against hg by the
        # orchestrator and SUPPRESSES the verdict outright, so a model that emits one could
        # silence its own report (or, left empty, hide a real backout from a gate that only
        # resolves when the field is unset).
        if isinstance(obj.get("candidate"), dict):
            obj["candidate"].pop("backedout_by", None)
            # Same rule for the is-itself-a-backout pair: both suppress or cap the verdict, so
            # a model that could set them could silence its own report — or, by emitting a
            # false ``is_backout``, stop the orchestrator resolving the real one.
            obj["candidate"].pop("is_backout", None)
            obj["candidate"].pop("backout_of_same_push", None)
    # Fix unambiguous citation spelling variants BEFORE validating so a "stack-frame"/
    # "removed" citation can't force a false abstain via salvage. Mutates ``obj`` in
    # place, so the ``_salvage`` fallback below sees the normalized citations too.
    _normalize_citations(obj)
    try:
        return validate_dossier(obj)
    except ValidationError as exc:
        kwargs, dropped = _salvage(obj)
        if dropped:
            logger.warning("dossier salvage: dropped %s", ", ".join(dropped))
        if "verdict" not in kwargs:
            # verdict absent or failed its own grounding rules -> cannot be trusted;
            # abstain, but keep whatever evidence was salvageable for the panel.
            kwargs["verdict"] = Verdict(
                decision=Decision.abstain,
                abstain_reason="dossier validation failed (verdict unusable): {}".format(exc),
            )
        try:
            return Dossier(**kwargs)
        except ValidationError:
            return _abstain("dossier validation failed: {}".format(exc))


def validate_role_fragment(role: str, obj):
    """Validate one role's parsed sub-block against its fragment model
    (call-graph-explorer->CallPath, patch-scout->list[DiffHunk],
    data-flow-tracer->DataFlowHypothesis, skeptic->list[SkepticResult],
    crash-interpreter->CrashBrief). Raises ``ValidationError`` on an uncited
    claim; the caller (#02) decides whether to abstain."""
    _normalize_citations(obj)  # same citation-spelling fix as the dossier path
    if role == "patch-scout":
        return _DIFF_HUNK_LIST.validate_python(obj)
    if role == "skeptic":
        return _SKEPTIC_LIST.validate_python(obj)
    model = _ROLE_FRAGMENTS.get(role)
    if model is None:
        raise ValueError(f"unknown role fragment: {role!r}")
    return model.model_validate(obj)


def dossier_to_db_json(d: Dossier) -> dict:
    """JSON-serializable dict for the persistence sub-plan's JSONB column
    (mirrors ``CrashTriageResult.model_dump()``)."""
    return d.model_dump(mode="json")


def dossier_from_db_json(d: dict) -> Dossier:
    version = d.get("schema_version")
    current = config.get_agent_schema_version()
    if version is not None and version > current:
        raise ValueError(
            f"dossier schema_version {version} is newer than supported {current}"
        )
    return Dossier.model_validate(d)
