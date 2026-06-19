# Crash-Clouseau — Evidence-Agent Plan

*Turning Clouseau from a line-proximity scorer into a tool that produces **strong,
verifiable evidence** that a specific developer should look at a specific nightly
crash because their patch is the likely culprit.*

---

## 1. Thesis

On Firefox **nightly** the population is small, so a single new-signature crash can
represent something at a much larger scale once it ships — but nobody hand-triages
single nightly crashes. Clouseau's job is to do what the maintainer used to do by
hand: **read the patch and the crash, understand the C/C++/Rust, and produce a
defensible "this is very likely caused by XXX"** — strong enough to justify a
needinfo.

Two things the current tool cannot do, and this plan does:

1. **Find the off-stack culprit.** The guilty patch frequently modifies a function
   that is **in the call graph but not on the crash stack** (a callee that produced
   bad state and returned, a callback/vtable target, a helper, a caller that now
   passes bad data). Today `Changeset.find` only considers patches touching files
   that appear *on the stack*, so it is structurally blind to this very common case.
2. **Reason about mechanism.** Today's score is pure line proximity — no "why." We
   want a causal argument: *this hunk changed this function, which is reached from
   frame N via this path, and under condition C frees/nulls/overruns the object that
   frame 0 then touches — consistent with the crash's faulting address / assertion.*

**The heuristic scorer demotes from "the answer" to "a cheap candidate seed."** The
new core is **call-graph-aware evidence assembly + grounded LLM reasoning + a
verification/abstain contract.**

### The org-chart metaphor (the design, not just an analogy)

A **team of senior engineers** (cheap / possibly free LLMs) does the legwork —
driving `searchfox-cli` to explore the call graph, reading function bodies, gathering
candidate patches, decoding the crash — and hands a clean **evidence dossier** to a
**principal engineer** (Claude) who does the deep causal judgment and writes the
verdict. This works because the two halves have opposite cost profiles:

| Work | Cost profile | Who |
|---|---|---|
| Exploration (many searchfox queries, reading bodies, pruning) | token-expensive, **reasoning-cheap** | cheap/free LLM seniors |
| Final causal verdict on a distilled dossier | **reasoning-expensive**, token-cheap | Claude (principal) |

Nightly volume is low enough to run this per interesting crash, so the lever is
**context assembly + verifiability, not cost-gating.**

---

## 2. The deliverable: the strong-evidence contract

The output for a crash is one of:

- **STRONG EVIDENCE** — a structured causal chain, every link citing a *verifiable
  artifact*, plus a confidence and a draft needinfo. Concretely:
  ```
  crash mechanism  : UAF; faulting address 0xe5e5e5e5 (jemalloc freed-poison)
  culprit patch    : <changeset node>  (bug <id>, author <hgauthor>)
  changed function : Foo::Bar           [searchfox permalink @ rev]
  call path        : frame3 Baz::Run -> Foo::Bar -> (returns freed mObj)   [calls-between evidence]
  mechanism        : hunk @@ lines 412-419 moves Release(mObj) before the early-return,
                     so mObj is freed while frame0 Qux::Use still derefs it   [diff line cite]
  consistency      : matches UAF poison + the crashing deref at frame0:line   [stack frame cite]
  confidence       : high
  ```
- **ABSTAIN** — "insufficient evidence; do not needinfo." This is a *first-class
  output*, not a failure. Because searchfox's call graph has holes (see §7), a
  calibrated abstain protects the one thing that makes the tool usable at all:
  **trust**. A wrongly-needinfo'd dev stops believing the tool.

**Grounding rule (non-negotiable):** every claim links to something checkable — a
searchfox permalink/symbol-id, an exact diff line, an exact stack frame. Seniors
**quote the index; they never assert.** The LLM is allowed to *navigate and cite*
ground truth, not to *invent* it.

---

## 3. The team (roles) and the dossier hand-off

Each senior is a cheap-LLM agent whose only tools are `searchfox-cli` and the Socorro
crash JSON (+ diff/source fetch). They run mostly in parallel.

1. **Crash Interpreter** — normalize the raw crash into a brief: failure *class*
   (UAF from poison address / null-deref / assertion `MSG` / OOB / shutdownhang),
   which thread truly matters, stack with inlines expanded, and the implied failure
   mechanism. Everything downstream narrows from this.
2. **Call-graph Explorer** — for each frame's function, drive
   `calls-from` / `calls-to` / `calls-between` (+ `define` to pull bodies) to build
   the **neighborhood**: callees where bad state could be produced-and-returned,
   callers, and what's passed between frames. *This is the role that surfaces
   off-stack candidates.*
3. **Patch Scout** — intersect the recent-patch set with the neighborhood (not just
   on-stack files), fetch each candidate diff, identify the modified functions, and
   write one line of "what changed semantically."
4. **Data-flow Tracer** — for a `(patch, frame)` pair, read the bodies along the path
   and answer the targeted question: *does this change actually free/mutate/null the
   value that the crash site touches?* Searchfox gives **edges**; it does **not** give
   data flow — so this read-and-reason step is exactly what replaces the maintainer's
   brain.
5. **Skeptic** — adversarial: re-query searchfox to confirm each claimed call edge
   *exists*, each cited diff line *says that*, the function is reachable at that
   revision, and there's no backout. Kills weak chains before they reach the
   principal.

**Principal (Claude)** receives, per surviving candidate, a tight pre-verified
dossier — crash brief + relevant hunks + the traced call path *with the source of
each hop* + the data-flow hypothesis + skeptic notes — and renders the verdict:
strong evidence (+ needinfo draft) or abstain.

### The dossier IS the architecture

Get the hand-off contract right and **every model in every seat becomes swappable** —
free model today, a better one tomorrow, no rewrite. The dossier is a typed object
(Pydantic) with: the crash brief, the candidate (node/bug/author), the call path as a
list of edges each carrying a searchfox citation, the diff hunks with line cites, the
data-flow hypothesis, and the skeptic's pass/fail per claim. The contract is also the
anti-hallucination boundary: a dossier field that lacks its citation is invalid.

---

## 4. End-to-end pipeline (and where it plugs into today's code)

The agent is a **decoupled downstream consumer** of what the existing pipeline already
produces. The current Python tool keeps doing ingestion, windowing, scoring, and the
web UI unchanged; it already writes the seed candidates we need into Postgres
(`Score`, `CrashStack`, `Changeset`, `Node`).

```
[existing Python pipeline]                         [new evidence agent — separate RQ worker]
 Socorro/VCS ingest                                 1. read seeds for a UUID
   -> inspect_stacktrace (inspector.py)                (CrashStack.get_by_uuid + Score)
   -> Changeset.find (window candidates)            2. Crash Interpreter  -> crash brief
   -> get_scores / put_frames (models.py)           3. Call-graph Explorer (searchfox-cli) -> neighborhood
   -> UUID.max_score                                4. Patch Scout: neighborhood ∩ window patches
   -> writes seeds to Postgres  ───────────────►    5. Data-flow Tracer per (patch, frame)
                                                     6. Skeptic verification pass
                                                     7. Principal (Claude) -> verdict + dossier
                                                     8. persist dossier+verdict; draft needinfo
                                                        (report_bug.py, behind human confirm)
```

- **Seed in:** the agent starts from the candidates Clouseau already surfaced
  (`Changeset.find` + `get_scores`). Off-stack expansion happens in step 4, *adding*
  to the seed set, not replacing it.
- **Crash data:** reuse `libmozdata`/`inspector.get_crash_data` to fetch the processed
  crash; pull the currently-discarded fields (`reason`, `crash_info.address`,
  `moz_crash_reason`, per-frame `inlines`/`trust`, PHC stacks) for the Crash
  Interpreter.
- **Diffs/source:** `RawRevision` (raw unified diff) + `searchfox-cli define` for
  function bodies.
- **Out:** persist `dossier` + `verdict` + `confidence` in a new additive table;
  surface an "evidence" panel in the existing UI; for `confidence=high`, prefill the
  needinfo draft via `report_bug.py` (display the suggested needinfo author — the
  field can't be auto-set in the `enter_bug` URL).

---

## 5. Implementation notes (Python)

- **Decoupled worker.** The agent is its own RQ job (enqueued from `update.py` after a
  report is scored, or run as a separate queue) that reads seeds from the DB and writes
  back. This isolates LLM flakiness/latency from ingestion and lets the agent evolve
  independently. (The clean seam also keeps a future Rust port of *just this worker*
  cheap, if ever wanted — but we're staying in Python.)
- **Reuse, don't rebuild:** `libmozdata` (Socorro/Bugzilla/hg), the existing
  SQLAlchemy models, `report_bug.py`, and the windowing logic all carry over.
- **`searchfox-cli`** (github.com/padenot/searchfox-cli — built for Claude Code):
  shell out to it; it emits LLM-friendly markdown by design. Commands of interest:
  `calls-from` / `calls-to` / `calls-between` (with `depth`), symbol/definition lookup
  and full-function extraction, field layout. Wrap it in a thin Python adapter that
  also captures the permalink/symbol-id for citations.
- **LLM SDK:** `anthropic` for the principal; the seniors hit whatever cheap/free
  model endpoint we choose, behind a single `llm_call(role, prompt)` abstraction so
  the model per seat is config, not code.
- **Dossier schema:** Pydantic models, validated at the hand-off so an uncited claim
  can't reach the principal.
- **Model tiering is a knob:** start every senior on the cheapest acceptable model;
  the Data-flow Tracer is the most reasoning-heavy senior and may warrant a mid tier;
  only the principal is Claude. Tune after Phase 0 with real numbers.

---

## 6. Grounding & verification discipline

The #1 failure mode is a cheap model hallucinating during exploration. Defenses, in
order of importance:

1. **Quote-only seniors.** Outputs must be artifacts (symbol ids, permalinks, exact
   lines), not prose assertions. Searchfox/diff is the source of truth.
2. **Schema validation at the hand-off.** A dossier link without its citation is
   rejected before it costs principal tokens.
3. **The Skeptic re-checks** every edge and line against the index/diff independently.
4. **The principal is told the map may be incomplete** and must abstain rather than
   stretch.
5. **Eval closes the loop** (§8): we measure whether "strong evidence" verdicts were
   actually right on a labeled corpus, and recalibrate confidence/abstain thresholds.

---

## 7. Known blind spots (and how we handle them)

- **Searchfox call-graph holes.** Virtual dispatch, function pointers, IPC,
  templates, macros, and especially **cross-language edges** (JS→C++ via
  WebIDL/XPIDL, Rust↔C++ FFI) are invisible to `calls-to`/`calls-from`. The
  neighborhood map is therefore *partial* exactly where Firefox is hairiest.
  Mitigations: bridge heuristics (for a virtual method, search implementors; for IPC,
  follow the message name), and — crucially — the **abstain path**, so a missing edge
  yields "insufficient evidence," not a wrong guess.
- **Revision drift.** The crash is a specific build node; searchfox indexes ~tip (now
  the git repo — see the hg→git migration). For nightly the gap is usually days, but
  the Tracer must remember the candidate patch *is itself* part of what differs
  between the crashed build and the indexed source.
- **Cheap-model variance.** Handled by the grounding discipline in §6.

---

## 8. Evaluation — measure what changed

Ground truth: the human-confirmed corpus — bugs carrying the `clouseau` alias
(`report_bug.py` writes `blocked='clouseau,{bugid}'`) with their `regressed_by` +
`cf_crash_signature`, mined from BMO and frozen at harvest time (the DB only retains
~30 days). Three metrics that map to the actual goals:

1. **Off-stack recall** — for regressions whose true regressor function was *not on
   the stack*, did the call-graph expansion's neighborhood reach it? Compared against
   today's stack-only recall, this is the single number that justifies the whole
   approach.
2. **Evidence correctness** — for "strong evidence" verdicts, did the cited call path
   and diff line actually hold, and was the named changeset the real regressor?
   (Precision of confident verdicts — the trust metric.)
3. **Abstain calibration** — when we abstained, was the true regressor genuinely
   unfindable from the available signal? (We don't want to abstain on findable cases,
   or claim strong evidence on unfindable ones.)

---

## 9. Phased plan

**Phase 0 — Call-graph Explorer spike *(do this first; it's the go/no-go)*.**
On a handful of past nightly regressions whose true regressor was **off-stack**, run a
cheap LLM + `searchfox-cli` and measure whether the neighborhood map reaches the true
regressor function. No DB, no integration — a standalone script. *This tests the one
assumption everything else rides on.* Effort: small. Risk: this is where we learn if
searchfox's holes (§7) sink the premise.

**Phase 1 — Dossier contract + seed seam + patch extraction.** Define the Pydantic
dossier; expose the existing seeds to the agent; land the patch-extraction foundation
(`plans/14` — hunk text + `@@` enclosing function + identifiers + cosmetic flag, no new
deps); build the Crash Interpreter and Patch Scout (which consumes patch extraction) to
assemble a dossier (no principal yet). Effort: medium.

**Phase 2 — Principal + evidence contract + abstain.** Claude consumes a dossier and
emits a strong-evidence verdict or abstain; add the Skeptic verification pass. Effort:
medium.

**Phase 3 — Wire to the product.** Persist verdicts, add the UI evidence panel, and
prefill the needinfo draft (`report_bug.py`) behind a human confirm. Effort: small–medium.

**Phase 4 — Eval harness + tuning.** Build the §8 measurement on the `clouseau`/
`regressed_by` corpus; tune model tiers and confidence/abstain thresholds with real
numbers. Effort: medium. (Can start in parallel once Phase 0 proves the premise.)

---

## 10. Reconciliation — the earlier heuristic ideas become *inputs*

The prior line-proximity-improvement ideas aren't discarded; they feed the agent:

- **Patch extraction upgrade** (`plans/14`) → stop keeping only line numbers; parse each
  diff's **hunk text** and the **`@@` enclosing-function** (a per-hunk function name for
  free, no symbol index), plus a touched-identifier set and a cosmetic flag. The
  enclosing-function name is the symbol-index-free join key from a patch to the
  call-graph neighborhood — the cheap half of off-stack matching. Feeds the Patch Scout
  (`#07`) and Data-flow Tracer (`#08`) and improves the current scorer (cosmetic
  down-weight, deleted-guard) with zero new dependencies.
- **Hot-file / IDF dampener** → prioritizes which seeds the seniors explore first
  (don't burn budget on `nsTArray`-class noise).
- **Semantic diff classification** (free/deref/bounds/lock) → cheap pre-tags (built on
  the patch-extraction hunk text) that become evidence features and route the
  Data-flow Tracer.
- **Crash-signal decoding** (poison address, `moz_crash_reason`, PHC stacks, inlines,
  trust) → the Crash Interpreter's raw material.
- **Blame / SZZ + drift-corrected line mapping** → seeds and sanity-checks for the
  call-path hops.
- **The eval harness** → repurposed to measure **off-stack recall + evidence
  correctness + abstain calibration**, not just rank.
- **Learned ranker / feature store** → secondary; useful later for seed *ordering*,
  not for producing evidence.

---

## 11. Beyond the LLM — non-LLM models as complements

The LLM framing is a *choice*, not the shape of the problem. Strip it away and
culprit-finding is natively several non-linguistic problems at once: a **graph**
problem (call graph + the stack is a path through it + patch→function + author/co-change),
a **time-series** problem (a regression is a crash-rate *step* at one build — pure
signal, no code), a **ranking-with-labels** problem (years of `regressed_by` pairs), and
at its core a **causal** problem ("did this patch *cause* this crash?"). The LLM is the
right tool for exactly one facet — *read the diff and the code and explain the
mechanism* — and the wrong or wasteful tool for the others. These models own the other
facets and **compose with** the agent rather than replacing it.

**What I'd add, ranked by leverage:**

1. **Statistical changepoint / regression-range *(buildable now, code-free)*.** A
   regression is a changepoint in a counting process. We already store the inputs —
   `Stats` holds per-signature `number` + `installs` per build. A CUSUM / Bayesian /
   Poisson rate-change test on that series finds *which build the regression started in*;
   `buildhub` + pushlog turns that into the exact set of changesets between last-good and
   first-bad — automated bisection-by-statistics (what `mozregression` does by hand). It
   doesn't name the function, but it shrinks the candidate set from "a 3-day window" to
   "the pushes in this build delta," sharpening and cheapening everything downstream.
   Unsupervised, no call graph, no labels. Limit: nightly's small N → a strong prior, not
   a verdict.

2. **GNN over a heterogeneous crash graph *(the structural answer to off-stack)*.**
   Nodes: functions, files, changesets, signatures, authors. Edges: *calls* (Searchfox),
   *contains*, *modified-by*, *appears-in-stack*, *co-change*, *authored-by*. A crash
   lights up the stack-frame nodes; fault localization becomes **learned
   blame-propagation by message passing** — suspicion spreads from frames along call
   edges to *off-stack* functions and scores the changeset nodes. This is the manual
   call-graph walk the senior team does with `searchfox-cli`, but learned from confirmed
   regressors, instant, and per-crash free. Slots in as a much smarter **candidate seed**
   feeding the same dossier; its edge weights are themselves evidence. Needs the labeled
   corpus + subgraph sampling (Firefox's graph is huge); cold-start applies.

3. **Learning-to-rank (GBDT / LambdaMART) *(boring, effective, interpretable)*.**
   LightGBM over engineered features — proximity, recency, churn, file IDF/hotness, frame
   depth + `trust`, author area, backed-out, crash-class, co-change, the `#14`
   touched-identifier overlap. Learns the tribal knowledge ("JS-engine changes look
   guilty but aren't") from labels; runs in microseconds; feature importances are legible
   evidence. Pairs with the rest — take the GNN embedding and the changepoint in-range
   flag as features and it becomes the ensemble's ranking head.

**Honorable mentions (genuinely different paradigms):**
- **Causal inference / difference-in-differences** — the purest framing of the
  deliverable: across the build matrix, does this signature's rate jump *specifically*
  when changeset X is present, controlling for the other patches in that build? A
  statistical causal argument independent of any code reading — strong corroboration.
  Hard (many patches/build = confounders, small N), but the conceptually-right model.
- **Retrieval / metric learning (two-tower)** — embed the crash, embed each diff, rank by
  learned similarity trained on confirmed pairs; or BM25 over identifiers (no generation).
  Cheap candidate generation.
- **Static / symbolic program analysis (CodeQL / clang dataflow)** — not ML, but the
  *rigorous* version of what the Data-flow Tracer (`#08`) approximates: a dataflow/taint
  query that mechanically checks whether a patch's change can reach the crash site with a
  UAF/null effect. The highest-precision **deterministic Skeptic** we could bolt on.
- **Sequence clustering** for stack-trace dedup beyond proto-signatures → attribute once,
  dedup the cluster, use cross-crash consensus as a signal.
- **Bandits / active learning** — the triager's accept/reject is a reward; frame "which
  crash to surface next" to maximize confirmed-regressors-per-human-minute, and request
  labels on the most informative cases. Improves the system, not the per-crash call.

**How it composes — the LLM is the narrator, not the ranker.** Cheap structure-aware
models (GNN/GBDT) score every crash and become the candidate seed (replacing line
proximity); the changepoint/DiD and a static-dataflow check are *independent*
corroboration; the LLM principal still writes the human-readable causal story over a
candidate set the non-LLM signals already agree on. The real win is that **multi-modal
agreement is calibrated confidence** — when the changepoint, the GNN, and the LLM
mechanism all point at the same patch, that's a far stronger (and cheaper-to-trust)
needinfo than one model's say-so; when they disagree, that's the **abstain** signal. It
also lets us spend the expensive LLM only where the cheap models are uncertain.

**Binding constraint: labels.** The learned models (GNN, GBDT, retrieval, causal) all
want the confirmed-regressor corpus, which is small and noisy on nightly — so they gate
on the same frozen corpus + eval harness (§8) the LLM plan already needs. The
**changepoint and the static-analysis verifier are usable now** (unsupervised /
rule-based). Sequence: ship the changepoint/regression-range prior first (it pays off
regardless of whether the agent ships), wire the static-dataflow Skeptic as the agent's
deterministic backbone, and bring up the GNN/GBDT ensemble once the corpus exists.

---

## TL;DR

Build a decoupled Python RQ worker that consumes the seeds Clouseau already produces,
sends a **team of cheap-LLM "senior engineers" with `searchfox-cli`** to explore the
call graph (including **off-stack**) and assemble a **fully-cited evidence dossier**,
then hands it to **Claude as the principal** to render a **strong-evidence verdict or
a calibrated abstain**. The dossier contract + grounding discipline are the real
engineering; the model in each seat is a tunable knob. Prove the premise in a Phase-0
call-graph spike before building the rest. And remember the LLM owns only the
*semantic-mechanism* facet (§11): culprit-finding is also a graph / time-series / causal
problem, so a statistical regression-range prior, a GNN seed, and a deterministic
static-analysis Skeptic are complements worth building — multi-model agreement is the
cheapest calibrated-confidence signal we have.
