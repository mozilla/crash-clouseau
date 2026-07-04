# Plan #15 — From binary verdict to leads + area-experts

> **Status:** design proposal (2026-07-04), not yet implemented. Grounded in the
> current code (file:line cited throughout) and spot-verified: `VERDICT_TYPE` enum,
> `Node.hgauthor`/`HGAuthor`, and the `apply_min_confidence` config key were checked
> against source. Motivated by two live runs (bugs 2044578 GCData, 2014136 Popover"
> UAF) that both correctly pinned the regressor changeset but ABSTAINED and surfaced
> nothing — a false-negative the product must avoid.
>
> Purpose (maintainer's framing): the deliverable is **useful leads** + **a
> knowledgeable person to needinfo** (someone who recently worked in the crashing
> area, even if not responsible), not only strong evidence. Be smart without being
> too smart: down-rank obviously-unrelated patches, never hard-drop.

## Summary (why)

Two live runs on real fixed-crash regressions both **correctly pinned the regressor
changeset** but ABSTAINED: the skeptic couldn't close the final mechanism step, so
`Dossier._skeptic_veto` (`schema.py:279-304`) collapsed the whole verdict to `abstain`,
which produced no needinfo (`triage.py:271`), mapped to DB `"abstain"`
(`orchestrator.py:144-146`), and rendered an **empty panel** (`html.py:39-40`,
`show_abstain=false`). A correct, cited lead surfaced *nothing*. This design makes the
common "plausible-but-unproven" case a first-class **`lead`** verdict, attaches
**deterministically-computed area-experts** to every crash (so a knowledgeable human
is surfaced even on a true abstain), and adds a **down-rank noise filter** so leads
stay credible. Everything down-ranks rather than deletes, and all Bugzilla writes stay
human-gated — so a real regressor in a common file always keeps a path out, and no one
gets auto-pinged.

## 1. Unified model — how the three pieces compose

- **Verdict tier (LLM judgment):** `strong-evidence` → `lead` → `abstain`. A `lead` =
  a *cited* candidate/hunk/edge whose mechanism is **not** verified end-to-end.
  `abstain` shrinks to its true meaning: *nothing cited worth a human's time.*
- **Skeptic ladder (was a cliff):** a skeptic `fail` now **downgrades
  `strong-evidence → lead`** (keeping the cited candidate) instead of collapsing to
  `abstain` — **unless no cited anchor stands**, in which case it still abstains. This
  single change turns both false-negative runs into delivered leads.
- **Noise filter (seed + prompt down-rank):** cosmetic/doc/whitespace, ubiquitous
  primitives, and universal bottom-of-stack frames get a **seed-score multiplier /
  confidence prior**, never a hard drop. Keeps the ranked candidate list credible.
- **Area-experts (deterministic, verdict-independent):** computed outside the LLM from
  local `Node.hgauthor` + libmozdata blame/recency, attached to the dossier for *any*
  verdict. A `lead` needinfos an area-expert (or the candidate author) with a **soft,
  non-accusatory** draft; a bare `abstain` can still surface experts to render.

Composition (the false-negative case): patch-scout cites the regressor → data-flow
builds a mechanism → skeptic `fail` on the final step → ladder downgrades to `lead`
(confidence clamped to medium=50, below the 85 apply gate) → `lead` carries a soft
needinfo draft targeting the candidate author (resolved from `Node.hgauthor`, not the
free-text `Candidate.author`) plus 1-3 area-experts → panel renders the lead + experts
→ human optionally sends the soft needinfo. Noise filter had already ensured the cited
candidate wasn't `nsTArray.h`.

## 2. Phased implementation (cheap/high-value first)

### Phase 1 — Surface the leads (highest value, lowest risk, no write-path change)

Fixes both false-negatives. Ships surfacing without any new one-click write.

| Change | File | Detail |
|---|---|---|
| Add `Decision.lead = "lead"` | `schema.py:50-53` | third enum value |
| Skeptic **ladder** (downgrade, not cliff) | `schema.py:279-304` `_skeptic_veto` | `strong-evidence + failed + has_anchor → lead(medium)`, keep candidate/draft, relabel mechanism as unverified; `+ no anchor → abstain` (old behavior). `has_anchor = cited candidate.node OR any cited hunk OR any cited call_path edge`. Keep **in-place mutation, never raise** (preserves the salvage-survives invariant at `schema.py:284-290`). |
| `lead` branch in `_consistency_rule` | `schema.py:247-265` | clamp confidence to `medium` if the model over-claims; **do not** raise on a missing claim anchor (defer to the Dossier-level anchor check — raising trips salvage and drops the lead) |
| Prompt: add `lead`, **rewrite** the hard "MUST abstain" | `system.md:5, 48, 52-64` | Add `lead` to the decision enum + a `lead` rule with a **soft** draft template. **Critically edit lines 56-60** — currently "If the skeptic returns `fail` … you MUST abstain." → "…a skeptic `fail` downgrades to `lead` if a cited candidate still stands." Soften line 5 too. |
| `lead` DB mapping | `orchestrator.py:141-151` `_verdict_row` | add `elif decision == lead: vt="lead"; rationale = mechanism.statement or …`. Confidence math unchanged. |
| Add `"lead"` to the enum | `models.py:22-23` `VERDICT_TYPE` | **DB migration required** — see §4. |
| Render leads | `config/global.json:52-56`, `config.py:162-169`, `html.py:39-40`, `crashstack.html:66-77,101-113` | add `show_lead:true` + `lead_label:"LEAD"`; panel gate admits `lead`; distinct amber badge; relabel Mechanism/Consistency `<h3>` as **"Working hypothesis (unverified)"** when `verdict=='lead'`. |

Phase-1 does **not** touch `_needinfo_action` or the apply gate: leads *render* the soft
draft for a human to copy, but one-click apply stays culprit-only.

### Phase 2 — Area-experts (deterministic, verdict-independent)

| Change | File | Detail |
|---|---|---|
| New helper `area_experts(frames, *, channel, build_node, build_date, neighborhood_files=(), max_experts=3)` | **new** `crashclouseau/agent/tools/experts.py` | Tier 1: local `Node.hgauthor → HGAuthor` for on-stack scored candidates (`models.py:147,156,560`) — **zero network, migration-proof**. Tier 2: libmozdata file-history authors+reviewers (confirm the class name — `Annotate` exists; verify `HGFileInfo`). Tier 3: `hgmozilla.Annotate` line-precision blame. Best-effort, swallow network errors (mirror `git2hg` at `inspector.py:40-43`). |
| Compute at seed + enrich candidate author | `orchestrator.py:86-109` `build_seed` | attach `experts`; also fill each candidate's real author from `Node.hgauthor` (fixes the unverified free-text `Candidate.author` at `schema.py:170`). |
| Persist | `orchestrator.py:134-151` + dossier JSON | add `AreaExpert` + `Dossier.area_experts`. **Not** a `Cited` subclass — keep it off the `min_citations`/salvage/veto path. Rides the dossiers JSONB (`models.py:1497`) → **no extra DB column**. |
| Render experts (even on abstain) | `crashstack.html:75-77` | "Suggested contacts / area experts" section; panel gate opens when `verdict=='lead'` OR `area_experts` non-empty. |

Exclusions reuse Phase-3's lists (drop experts whose only qualifying files are
ubiquitous primitives / bottom-of-stack frames; skip backed-out-only and bot authors;
require a non-trivial touch). Cap at 3; prefer module owners/peers; de-dup per signature.

### Phase 3 — Noise filter (credibility)

Single config home `agent.filters` in `config/global.json`. All rules are **seed-score
multipliers or confidence priors**, never drops:

| Class | Detect | Home | Lever |
|---|---|---|---|
| cosmetic/comment/doc | extend `patch_extract` (`is_cosmetic` at `patch_extract.py:293` + `comment_only`/`doc_only`; `is_inert()` = all files inert) | `patch_extract.py`, `tools/patch.py`, prompts; optional `build_seed` multiplier | `cosmetic_penalty` |
| ubiquitous primitives | curated path/symbol denylist + optional searchfox `calls_to` fan-in (agent-side) | path → `Changeset.get_scores`/`build_seed`; symbol → prompt + patch-tool header | `ubiquitous_penalty` |
| universal anchor frames | `function` matches `anchor_frame_patterns` (main/RunTask/ThreadFunc/message-pump/…) | `build_seed` loop (`orchestrator.py:87-96`) | `anchor_frame_weight` |
| no call-graph proximity | no neighborhood/edge intersection after explorer **fixpoint** (distinct from a searchfox *hole*) | prompts only | down-rank "no path found" (never on line-proximity alone — off-stack is the core case) |

### Phase 4 — Lead-tier apply (only after lead precision is measured)

| Change | File | Detail |
|---|---|---|
| Lead needinfo bridge | `triage.py:257-283` `_needinfo_action` | accept `decision in (strong_evidence, lead)`; soft expert/author draft; keep `is_private:True`. |
| Lead apply gate | `bugzilla_apply.py:59-73,151`, `config.py:162-169`, `global.json` | add `lead_apply_min` to **both** `global.json` (`agent.confidence.lead_apply_min`) and the `get_agent_ui()` normalization, then admit `verdict=="lead" and conf>=lead_apply_min`. |
| Button copy | `crashstack.html:~197` | "Send needinfo to a knowledgeable person…" when `is_lead`. |

## 3. Sharpest risks and mitigations

1. **Over-filtering a real regressor.** Phase 3 is *multiplier/prior only, never drop* —
   a regressor in `nsTArray` still surfaces if data-flow ties the exact changed line to
   the crash and the skeptic passes; the strong-evidence gate is independent of any
   penalty. Nothing deleted → the down-ranked author still a valid area-expert.
2. **Needinfo-spam / pinging uninvolved devs.** (a) the **anchor gate** — a `lead` needs
   something *cited* or it's abstain; (b) **soft, help-request wording** enforced in
   `system.md` + the expert draft ("this is NOT an accusation… could you help or
   redirect us?"); (c) **human-gated apply** — Phase 1 never posts; Phase 4 is
   confidence-floored + still a panel click. Cap experts at 3, prefer peers, skip bots.
3. **A refuted chain presented as proof.** Distinct `lead` badge; Mechanism/Consistency
   relabeled "Working hypothesis (unverified)."
4. **Lead volume swamps the panel.** Confidence-capped at `medium`; never wears the
   strong-evidence badge nor clears the 85 gate — monitor rendered volume after launch.

## 4. DB / schema-version implications

- **The only schema migration is the `VERDICT_TYPE` enum.** Prod is Postgres
  (`Verdict.set` uses `pg.insert(...).on_conflict_do_update`, `models.py:1540-1543`), so
  adding `"lead"` needs an Alembic `op.execute('ALTER TYPE "VERDICT_TYPE" ADD VALUE
  ''lead''')` **outside a transaction**, shipped with the `models.py:22-23` change, or
  `Verdict.set(verdict="lead")` raises in prod. In dev (`sqlite://`) the enum is VARCHAR
  → a recreate picks it up free.
- **`area_experts` needs no migration** — lives on the `Dossier`, stored in the existing
  `dossiers` JSONB via `Verdict.dossierid` (`models.py:1497`). Bump `agent.schema_version`
  (`global.json:43`) so `dossier_from_db_json` (`schema.py:466-473`) stays version-aware.
- `Verdict.set`/`get_evidence`/`get_by_uuid` are value-agnostic — no code change.

## 5. Section-level corrections resolved during synthesis

1. UI apply key is **`apply_min_confidence`** (`config.py:165`, read at
   `bugzilla_apply.py:56`), sourced from `agent.confidence.apply_min` — a `lead_apply_min`
   must be added to both `global.json` and `get_agent_ui()`, else `ui.get("lead_apply_min")`
   reads nothing. (Verified.)
2. `system.md:56-60` must be **edited, not appended** — it currently hard-states "MUST
   abstain" on a skeptic `fail`; leaving it contradicts the ladder.
3. The area-expert needinfo cannot be a plain comment: `bugzilla.add_comment` posts a
   comment only. A true needinfo needs `bugzilla.update_bug` with `changes.flags`
   (requestee = expert email) via `_put_bug` (`bugzilla_apply.py:91-103`) *plus* the soft
   comment. Both types already in `enabled_types`.
4. Pure-abstain experts have no target bug → always *render* experts+draft; enable a
   one-click action only when a concrete target bug exists.
5. `hgmozilla` still points at `hg.mozilla.org` post-migration → the experts helper is
   ordered **local `Node.hgauthor` first** (migration-proof), then best-effort network.
6. `result.actionable` correctly stays culprit-only (`result.py:37-39`) — not on the
   apply path; leads are eligible via `_needinfo_action`/apply gate.

## Ranked recommendation

Ship **Phase 1** now (fixes both false-negatives, zero new write risk) → **Phase 2**
(experts — the "knowledgeable person" deliverable, mostly free from local data) →
**Phase 3** (credibility) → **Phase 4** (lead one-click apply, once precision is measured).
