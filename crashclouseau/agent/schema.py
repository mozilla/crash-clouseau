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
to abstain. Only stdlib + pydantic + ``config``/``logger`` are imported at module scope
here (no db/SDK/network); ``_skeptic_veto`` additionally does a LOCAL import of
``crashclouseau.compiled_out``, which is pure stdlib ``re`` + ``logger`` at import time and
keeps its own hgedge/searchfox imports lazy, so the no-network property is unchanged.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
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
    # Real Firefox crash families the original vocabulary simply could not say. Left out,
    # the model's honest ``"oom"`` was an enum error, and because ``CrashBrief`` validates
    # whole in ``_salvage`` that dropped EVERY frame of the brief: 21 prod dossiers in a
    # month (``oom`` x17). Silent, too — nothing but the UI reads the brief.
    oom = "oom"
    stackoverflow = "stackoverflow"
    other = "other"

    @classmethod
    def _missing_(cls, value):
        """Case-fold, then degrade an unknown class to ``other`` instead of binning the
        crash brief. A VOCABULARY field on model-supplied JSON needs a total function, not
        another finite list — ``_KIND_ALIASES``/``_SIDE_ALIASES`` have each been extended
        several times and still miss values. ``other`` is the non-behaviour-asserting
        member, so an unrecognized class can never assert a mechanism it hasn't earned.

        Persistence note: once an ``oom`` dossier is written, an older build's enum cannot
        read the row back (``dossier_from_db_json`` raises). Do not roll the members back
        selectively; ``_missing_`` alone does not have that property."""
        s = str(value or "").strip().lower()
        for member in cls:
            if member.value == s:
                return member
        return cls.other


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
    ref = "ref"


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
# A citation's ``node`` is only ever used as one thing: the revision component of an
# hg URL. So it has to BE a revision. The model does not always oblige — it treats the
# field as a place to say where it looked, and bug 2061961 was filed with
#     [servo/components/style/data.rs](https://hg.mozilla.org/mozilla-central/file/tip (channel nightly)/servo/components/style/data.rs)
# because the handoff said ``"node": "tip (channel nightly)"``. That is a dead link in the
# one section of the comment whose entire purpose is letting a reader check the analysis
# without leaving the bug, and it is worse than no link: on crashstack.html a truthy node
# also SUPPRESSES the permalink fallback, so the page loses the reference altogether.
#
# Accepts a 7-40 char hex node (hg short or full, and a git sha), plus the symbolic ``tip``,
# which hg resolves and which is what the model means when it says it read current source.
# Anything else is prose: try the first whitespace-delimited token, since a revision never
# contains a space and the rest is commentary ("tip (channel nightly)", "c998e317e0cc (bug
# 2042063)"), and otherwise drop it — an empty node lets the permalink/label fallbacks run.
#
# Deliberately NOT applied to ``Candidate.node``: gates, backout resolution and the
# autofiler all read that one, so silently blanking it would change a verdict rather than
# a link. A citation's node has no such consumer (see StackFrameCitation.node's note).
_HG_REV_RE = re.compile(r"^(?:[0-9a-fA-F]{7,40}|tip)$")


def _clean_rev(v):
    """A usable hg revision from whatever the model put in a citation's ``node``, or ""."""
    if v is None:
        return ""
    s = str(v).strip()
    if not s or _HG_REV_RE.match(s):
        return s
    head = s.split()[0].strip("(),;:")
    return head if _HG_REV_RE.match(head) else ""


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

    @field_validator("node", mode="before")
    @classmethod
    def _clean_node(cls, v):
        return _clean_rev(v)


class StackFrameCitation(BaseModel):
    """A frame of the crashing stack, cited as evidence.

    Every field is optional and ``None``-tolerant, for the reason ``CrashFrame`` already
    documents below: symbolication emits nulls for an opaque frame (macOS
    ``os_unfair_lock``, JIT/stub/driver frames), and this model cites THOSE VERY FRAMES.
    A default alone is not enough — pydantic rejects an explicit ``null`` regardless of the
    default — so the before-validators are the load-bearing half. Measured over 1950 prod
    handoffs: the defaults recover 1 lost verdict, the coercion recovers 14.

    The cost of making everything optional is that ``{"kind": "stack_frame"}`` would
    otherwise be a content-free citation that satisfies the ``Cited`` min-citations
    anti-hallucination rule — and, with ``stackpos`` defaulting to 0, would render as
    "frame #0", i.e. the CRASHING frame, out of thin air. ``_must_point_somewhere`` is what
    keeps the guarantee: a citation has to name something."""

    kind: Literal["stack_frame"] = "stack_frame"
    uuid: str = ""
    stackpos: int = 0
    filename: str = ""
    function: str = ""
    line: int = 0
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

    @field_validator("uuid", "filename", "function", mode="before")
    @classmethod
    def _none_to_empty(cls, v):
        return "" if v is None else v

    @field_validator("node", mode="before")
    @classmethod
    def _clean_node(cls, v):
        """Also covers the None -> "" coercion the sibling validator does."""
        return _clean_rev(v)

    @field_validator("stackpos", "line", mode="before")
    @classmethod
    def _none_to_zero(cls, v):
        return 0 if v is None else v

    @model_validator(mode="after")
    def _must_point_somewhere(self):
        """A citation must identify a frame. ``stackpos``/``line`` do NOT count: both
        default to 0, so accepting them alone would let a bare ``{"kind":"stack_frame"}``
        pass as a citation of frame #0. The real case this must keep working is the opaque
        frame — ``function`` known, everything else null — and it does."""
        if not (self.uuid or self.filename or self.function or self.node):
            raise ValueError("stack_frame citation identifies no frame")
        return self


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


class RefCitation(BaseModel):
    """A pointer at something the agent READ that none of the four kinds above can express.

    This closes a STRUCTURAL gap, not a spelling one. The union predates the
    ``mcp__source__raw_file`` and ``mcp__history__{file_history,blame,changeset}`` tools:
    the agent reads a changeset, cites it honestly, and there is no legal ``kind`` to
    write. ``_KIND_ALIASES`` cannot help — it only repairs spellings of kinds that exist.
    Measured over 1950 prod handoffs this was the single largest loss: 22 of the 41
    verdicts destroyed by validation, still firing as of 2026-08-04.

    It is deliberately the WEAKEST citation kind. ``orchestrator._has_verified_callpath``
    (the off-stack SF-3 gate) keys on ``isinstance(c, SearchfoxCitation)``, so a ``ref``
    cannot manufacture strong evidence; it only stops honest evidence being thrown away."""

    kind: Literal["ref"] = "ref"
    # ``rev``/``path`` are accepted as aliases because they are what the tools' own output
    # calls these things, and the model copies that vocabulary: 21 prod citations name the
    # changeset ``rev`` and the file ``path``. They still validated (on ``content``), but
    # both pointers were dropped, so the page showed bare prose where it could have linked
    # the file. Aliasing costs nothing and recovers the link.
    node: str = Field(default="", validation_alias=AliasChoices("node", "rev"))
    filename: str = Field(default="", validation_alias=AliasChoices("filename", "path"))
    line: int = 0
    symbol_id: str = ""
    permalink: str = ""
    content: str = ""
    # Needed because the two aliases above otherwise REPLACE the field names, which would
    # break every dossier already persisted with ``node``/``filename`` keys.
    model_config = ConfigDict(populate_by_name=True)

    @field_validator("filename", "symbol_id", "permalink", "content", mode="before")
    @classmethod
    def _none_to_empty(cls, v):
        return "" if v is None else v

    @field_validator("node", mode="before")
    @classmethod
    def _clean_node(cls, v):
        """Also covers the None -> "" coercion the sibling validator does. This is the
        kind bug 2061961's dead link came through: ``ref`` is the catch-all, so it is
        where the model is most likely to write prose instead of a revision."""
        return _clean_rev(v)

    @field_validator("line", mode="before")
    @classmethod
    def _none_to_zero(cls, v):
        return 0 if v is None else v

    @model_validator(mode="after")
    def _must_point_somewhere(self):
        """Same guarantee as ``StackFrameCitation``: every field defaulting would make
        ``{"kind":"changeset"}`` a citation of nothing that still satisfies the
        min-citations rule. Verified to recover the same 22 verdicts as the unguarded
        version while still refusing a content-free citation."""
        if not any((self.node, self.filename, self.symbol_id, self.permalink,
                    self.content)):
            raise ValueError("ref citation points at nothing")
        return self


Citation = Annotated[
    Union[SearchfoxCitation, DiffLineCitation, StackFrameCitation, StructLayoutCitation,
          RefCitation],
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
    # Defaulted for the same reason as the ``_none_to_empty`` fields below: an INLINED frame
    # has no position of its own, and the model omitted ``stackpos`` on 52 prod frames.
    stackpos: int = 0
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

    @field_validator("stackpos", "line", mode="before")
    @classmethod
    def _int_none_to_zero(cls, v):
        """The same hazard, one field short of where it was first fixed. ``line`` is
        EXACTLY the field symbolication nulls — ``_stack_text`` shows the model frames
        rendered ``#7 None :None`` — and the coercion above only covered the ``str``
        fields, so a null ``line`` still dropped the whole brief. 177 null lines across 89
        prod dossiers, the single biggest cause of the 127 lost crash briefs. Placeholder
        strings are folded in too; anything else still fails, so real drift is not hidden."""
        return 0 if v in (None, "", "None", "unknown", "?") else v


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
    # The candidate author's email, resolved by the ORCHESTRATOR from hg's ``user`` field and
    # STRIPPED from the model's handoff — this is who the automatic filer sends a needinfo to,
    # so a model that could set it could point a ping at anyone. "" = unresolved, and the
    # filed bug then simply carries no needinfo.
    author_email: str = ""
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
            # (noise-focused) skeptic — a skeptic `fail` demotes a lead to abstain (unless
            # its ground is a configure-switch claim: ``_skeptic_veto`` (1c)), and an
            # anchorless lead is demoted too — which is what protects against sending
            # people after noise.
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

        (1c) A ``fail`` whose STATED GROUND is a configure-switch compile-flag claim does
        NOT bind on a lead — it is treated exactly like ``unverifiable`` — unless the
        deterministic gate agrees (``corroborations['compiled_out_suppressed']``). The
        invariant this buys: such a ``fail`` costs a verdict its STRONG-EVIDENCE status
        (rule 1 is untouched) but never its existence. Rationale, the replay panel and the
        GTK-on-Windows counter-example it must not eat: ``compiled_out.is_build_flag_ground``
        — in short, that ``fail`` is the one place where the cheapest model tier walks the
        longest multi-hop chain (symbol -> ``#ifdef`` -> ``set_define`` -> ``option``) for
        the harshest consequence we have, and an abstain reaches no scoreboard, so a wrong
        one is invisible. Note the ordering: ``_apply_compiled_out_gate`` runs much LATER
        (in ``apply_deterministic_gates``), so live the corroboration is never set by the
        time this runs and it always unbinds — the gate then does the suppressing itself,
        deterministically. The clause earns its keep on a RE-VALIDATED stored payload,
        where the flag IS present and must not be undone.

        (2) ANY surviving lead must carry a cited anchor; an anchorless lead is demoted to
        abstain (nothing to hand a human)."""
        # Local: this module's contract is stdlib + pydantic + config/logger, and
        # ``compiled_out`` lazily imports hgedge/searchfox. Cheap after the first call.
        from crashclouseau import compiled_out

        v = self.verdict
        if v is None:
            return self
        # Split the fails by what they REST ON, not by what they conclude (1c). Per RESULT,
        # because the skeptic emits one per claim: a genuine contradiction sitting in its
        # own entry must keep its teeth even when another entry cites a configure switch.
        gate_agrees = bool((self.corroborations or {}).get("compiled_out_suppressed"))
        failed, binding, unbound = [], [], []
        for s in self.skeptic:
            if s.status != SkepticStatus.failed:
                continue
            failed.append(s.claim_ref)
            if not gate_agrees and compiled_out.is_build_flag_ground(s.note, s.citations):
                unbound.append(s.claim_ref)
            else:
                binding.append(s.claim_ref)
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
        elif v.decision == Decision.lead and binding:
            self.verdict = Verdict(
                decision=Decision.abstain,
                confidence=Confidence.low,
                abstain_reason="skeptic flagged this lead as noise / unrelated "
                               "(failed: {})".format(", ".join(binding) or "?"),
            )
        # (1c) The only fails left rest on a configure-switch claim, which the deterministic
        # compiled-out gate decides. Keep the lead and RECORD it, so a rule whose whole
        # failure mode is a false abstain is countable instead of invisible.
        elif v.decision == Decision.lead and unbound:
            self.corroborations = {
                **(self.corroborations or {}),
                "skeptic_build_flag_unbound": unbound,
            }
            logger.info("schema: skeptic fail(s) %s rest on a configure-switch claim, not on this "
                        "crash's own facts; lead kept (the compiled-out gate decides that)",
                        ", ".join(unbound) or "?")
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
    # The source/history tools have no citation kind of their own -> ``ref`` (see
    # ``RefCitation``). Unlike every entry above, these are not misspellings: each is a tag
    # the model INVENTED because the vocabulary had no word for what it had just read.
    # This is exactly the set observed across 1950 prod handoffs — it recovers all 22
    # affected verdicts with none newly lost, so it is complete for observed data, not a
    # guess. It is still a PARTIAL map, and the next invented tag will miss it; mapping any
    # unrecognized kind to ``ref`` would make it total (the "points at something" guard
    # makes that safe), but that is unmeasured, so it is not what ships here.
    "ref": "ref", "changeset": "ref", "source": "ref", "source_raw_file": "ref",
    "source_raw": "ref", "source_read": "ref", "source_pinned": "ref",
    "pinned_source": "ref", "source_line": "ref", "history": "ref",
    "history_changeset": "ref", "history_file_history": "ref",
    # ...but NOT ``stack``: 5 of the 6 prod ``kind:"stack"`` citations carry the exact
    # ``StackFrameCitation`` field set (uuid/stackpos/filename/function/line/node), so
    # routing them to the catch-all would silently discard function/stackpos/uuid and render
    # an hg link where the page should say "frame #5 … AfterSetAttr". Measured: it recovers
    # the same verdicts either way, so send it to the kind that keeps the information.
    "stack": "stack_frame",
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


# --------------------------------------------------------------------------- #
# Validation-failure reporting
# --------------------------------------------------------------------------- #
# ``abstain_reason`` is rendered VERBATIM to a human on crashstack.html. Formatting a raw
# pydantic ``ValidationError`` into it put a wall of ``input_value={...}`` reprs and
# ``errors.pydantic.dev`` links on the page — 118 persisted dossiers still carry one. The
# field paths are the only part a reader (or a future audit) can use, so keep those and send
# the full exception to the log, where it belongs.
_MAX_REPORTED_PATHS = 4
# A pydantic error path line in an ALREADY-PERSISTED reason: unindented, not the header.
_ERR_PATH = re.compile(r"^([A-Za-z_][\w.\[\]]*(?:\.[\w.\[\]]+)*)\s*$", re.MULTILINE)


def _validation_paths(exc: ValidationError) -> list[str]:
    """The dotted field paths pydantic rejected, deduped, in report order."""
    seen: set = set()
    out: list = []
    for err in exc.errors():
        path = ".".join(str(p) for p in err.get("loc", ()))
        if path and path not in seen:
            seen.add(path)
            out.append(path)
    return out


def _format_paths(paths: list[str], total: int | None = None) -> str:
    n = total if total is not None else len(paths)
    shown = paths[:_MAX_REPORTED_PATHS]
    more = len(paths) - len(shown)
    detail = ", ".join(shown) + (" (+{} more)".format(more) if more > 0 else "")
    return "{} malformed field{}{}".format(n, "" if n == 1 else "s",
                                           ": " + detail if detail else "")


def humanize_validation_reason(reason: str | None) -> str | None:
    """Turn a validation-failure ``abstain_reason`` into something a human can read.

    Applied at READ time (``bugzilla_apply.build_evidence``) rather than only at write time,
    because the walls of pydantic prose are already persisted on 118 dossiers and re-parsing
    the stored text is the only way to fix those pages without a data migration. Any other
    reason is returned unchanged."""
    if not reason or not reason.startswith("dossier validation failed"):
        return reason
    body = reason.split(":", 1)[1] if ":" in reason else ""
    paths = [m.group(1) for m in _ERR_PATH.finditer(body)
             if m.group(1) not in ("Dossier",) and "." in m.group(1)]
    return (
        "Clouseau finished an analysis but could not read its own output back "
        "({}), so nothing is reported rather than guessed. This is a Clouseau-side "
        "failure, not a finding about the crash.".format(
            _format_paths(paths) if paths else "malformed output"
        )
    )


def validate_dossier(obj: dict) -> Dossier:
    """Strict gate: ``Dossier.model_validate`` — raises ``ValidationError`` on any
    uncited claim or malformed field."""
    return Dossier.model_validate(obj)


def _abstain(reason: str) -> Dossier:
    return Dossier(verdict=Verdict(decision=Decision.abstain, abstain_reason=reason))


# The abstain_reason ``parse_and_validate`` uses when there was no readable handoff at
# all. Named (not inlined) because ``triage.build_result`` matches on it to tell that
# apart from a real verdict: the system prompt requires the fenced block on EVERY path
# -- ``abstain`` is a value INSIDE the JSON, with a required ``abstain_reason`` -- so a
# missing/unparseable block is never the model declining, it is the run never reaching a
# conclusion. ``parse_and_validate`` keeps returning an abstain Dossier (its "never
# raises" contract is relied on by the eval runner and the schema tests); the caller
# decides that it is a failure.
NO_HANDOFF_REASON = "no parseable ```json block in the agent result"


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
        return _abstain(NO_HANDOFF_REASON)
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
            # Who the automatic filer needinfos. Resolved from hg by the orchestrator; a
            # model-supplied value would let the handoff aim an unattended Bugzilla ping at
            # an arbitrary address.
            obj["candidate"].pop("author_email", None)
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
            # The FULL exception goes to the log, not into ``abstain_reason``: that string is
            # rendered verbatim to a human, and a pydantic dump there is unreadable noise.
            logger.warning("dossier validation failed (verdict unusable): %s", exc)
            kwargs["verdict"] = Verdict(
                decision=Decision.abstain,
                abstain_reason="dossier validation failed (verdict unusable): {}".format(
                    _format_paths(_validation_paths(exc))
                ),
            )
        try:
            return Dossier(**kwargs)
        except ValidationError as inner:
            logger.warning("dossier validation failed, nothing salvageable: %s", inner)
            return _abstain("dossier validation failed: {}".format(
                _format_paths(_validation_paths(exc))
            ))


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
