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
    # ``probable`` sits between ``medium`` and ``high`` and is reserved for a lead that
    # a DETERMINISTIC corroboration (computed outside the LLM — e.g. a fault-address ==
    # struct-field-offset match) has raised above the bare-lead ceiling. The model may
    # NOT self-assert it (``_consistency_rule`` clamps a model-emitted probable/high on
    # a lead back to medium); only ``orchestrator._apply_corroboration_gate`` sets it.
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
    node: str


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
    backedout: bool = False
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


class Verdict(BaseModel):
    decision: Decision
    confidence: Confidence = Confidence.low
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
            # A lead is plausible-but-unverified: the MODEL must never self-assert a
            # confidence above `medium` (its own "high"/"probable" is exactly the
            # over-claim we don't trust), so clamp both down to `medium`. `probable`
            # (0.70) is reachable ONLY via ``orchestrator._apply_corroboration_gate``,
            # which sets it AFTER validation from a deterministic corroboration (a
            # fault-address == struct-field-offset match) — a signal the model cannot
            # fabricate. This is a fixed one-way clamp, independent of the tunable
            # strong-evidence floor. The cited-anchor requirement (a candidate/hunk/
            # edge) is enforced at the Dossier level (``_skeptic_veto``), which can see
            # those fields — deliberately NOT raised here: raising would trip
            # parse_and_validate's salvage and drop an otherwise-useful lead.
            if self.confidence in (Confidence.high, Confidence.probable):
                self.confidence = Confidence.medium
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

        (1) A skeptic ``fail`` refutes a strong-evidence *mechanism*, but a cited
        candidate/hunk/edge may still be a useful LEAD — so a ``fail`` DOWNGRADES
        strong-evidence to ``lead`` when such an anchor stands (dropping the now-stale
        assertive draft for a SOFT one that doesn't restate the refuted mechanism), and
        only collapses to ``abstain`` when nothing cited remains. ``unverifiable`` is
        advisory (a searchfox hole), not a ``fail``.

        (2) ANY lead — including one the model emits directly — must carry a cited
        anchor; an anchorless lead is demoted to abstain (nothing to hand a human)."""
        v = self.verdict
        if v is None:
            return self
        # (1) Skeptic ladder on a strong-evidence verdict.
        if v.decision == Decision.strong_evidence:
            failed = [s.claim_ref for s in self.skeptic
                      if s.status == SkepticStatus.failed]
            if failed:
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
