# Second-opinion validation — what the first three prod days actually showed

*2026-07-27. Canary `crash-clouseau-augmented` v49, `SECOND_OPINION_ENABLED=1` since
2026-07-24 18:32 UTC. All numbers below are from the canary DB or from re-runnable scripts in
`spike/`; nothing here is an estimate unless it says so.*

---

## TL;DR

1. The blind second opinion (#SO) **worked in prod from the first day** — 25 runs, gating clean.
   The previous session's "no successful SO observed" note was an artefact of Heroku's ~2.5h log
   retention, not of the code.
2. It **refuted 17 of 23 verify-mode leads (74%)**. That is **not** instrument bias. Measured
   against corpus ground truth the SO has **sensitivity ~0.93, specificity 1.00**.
3. Inverting the prod refute rate with those characteristics: **only ~28% of prod verify-mode
   leads name the true regressor.** Two independent cross-checks agree to three decimals.
4. The failure has **one dominant, mechanizable cause**: the pipeline names a changeset from the
   current build's pushlog window for a signature that has existed for a median **~6 months**.
   **10 of 10** high-confidence refutations rest on this, and all 10 verify with free arithmetic.
5. Chasing that down turned up an unrelated problem: **23% of `corpus_ship`'s regressor labels are
   wrong** (27% of individual landing nodes). It does **not** corrupt the calibration — verified,
   0 tainted hits — but it did corrupt this report's own §3 measurement, and the labeller that
   produced it is now fixed.
6. Cost: **67% of spend goes to signatures that were not new** when we triaged them. The
   second opinion is only ~4% of spend and is not the problem.

---

## 1. What ran (canary DB, 2026-07-24 18:32Z → 2026-07-27 08:00Z)

| | |
|---|---|
| dossiers | 204 |
| shipped verdicts | 168 abstain, 29 lead, 2 culprit |
| second opinions stored | **25** (23 verify mode, 2 mechanism mode) |
| abstains carrying an SO | **0** — gating is correct |
| SO cost | **$1.07** avg, $26.63 total = **~4% of spend** |

Agreement, verify mode (n=23):

| | corroborates | high conf | medium conf |
|---|---|---|---|
| **refuted** | 17 (74%) | 10 | 7 |
| **corroborated** | 6 (26%) | 5 | 1 |

Only a *high-confidence* refutation moves the band, so those 10 are the ones with teeth.

Coverage gaps found: **2** eligible rung-50 leads had no SO at all (silent failure — the pass is
best-effort, so a prod break left no trace), and **4** reported leads at rung 25 were never
eligible because `min_confidence` was 50 even though every `lead` is displayed.

## 2. The refutations are right — verified without an LLM

`spike/verify_so_timing_claims.py` (Socorro first-seen buildid vs hg pushdate, free):

| group | n | candidate landed AFTER the signature first appeared | median gap |
|---|---|---|---|
| **refuted, high confidence** | 10 | **10 / 10 (100%)** | **178 days** |
| refuted, any confidence | 17 | 13 / 17 (76%) | 111 days |
| **corroborated (control)** | 6 | 2 / 6 (33%) | — |

The control is what makes this conclusive: the SO is not refusing everything. It even refuted node
`a7f1793a7983` for one signature while corroborating the same node for another.

Two honest caveats, both real:

* **Signature reuse.** An old signature can acquire a new cause, and a rare pre-existing crash can
  be made frequent by a new change. "Predates" is strong evidence the candidate is not the
  *origin*, not proof the lead is worthless.
* **2 of 6 corroborated leads showed the same timing pattern**, so a hard "postdates → abstain"
  rule would have killed independently-confirmed leads. This must be a **strong downweight,
  overridable on mechanism grounds** — not a drop.

Separately confirmed: **all 23 prod candidates do predate their own crash build** (0 violations),
so the pushlog window is doing its job. The prod defect is purely about signature age.

## 3. How good is the instrument? (`spike/so_instrument_calibration.py`)

51 real SO calls against corpus ground truth, effort=max, $40.62, zero errors. Two arms:
the **known-true** regressor as candidate (every refute is false), and a **real-but-wrong**
changeset from a window that provably lacks the regressor (every refute is true).

| arm | n | corroborated | refuted | rate |
|---|---|---|---|---|
| known-true regressor | 25 | 18 | 7 | false-refute **0.28** |
| wrong changeset | 26 | **0** | 26 | true-refute **1.00** |

Raw discrimination gap **0.72**. It never once endorsed a wrong changeset.

Then reading the 7 false refutes showed **6 were on cases whose corpus LABEL was broken** (§4).
On the clean-label subset (n=15) there is **1** false refute:

> **sensitivity 14/15 = 0.93** (Wilson 95% CI **0.70–0.99**)
> **specificity 26/26 = 1.00** (Wilson 95% CI **0.87–1.00**)

**Inverting the prod refute rate.** With `R = p(1−sens) + (1−p)·spec`:

```
0.739 = p(0.07) + (1−p)(1.00)   →   p ≈ 0.28
```

**~28% of prod verify-mode leads name the true regressor.** Cross-check: the predicted
corroborate rate `0.28 × 0.93 = 0.261` matches the observed `6/23 = 0.261` exactly.

**Be honest about the width.** The prod refute rate itself is `17/23` (Wilson 95% CI 0.54–0.88),
which propagates to **p ≈ 0.13–0.50**. Using the uncorrected sensitivity of 0.72 instead gives
p ≈ 0.36 (0.17–0.65). So the point estimate is ~28% and the defensible claim is "well under half" —
not a precise figure. The *direction* is solid, and it is corroborated independently by §2 (10/10
timing checks) and §5 (24 of 32 leads on not-new signatures), which do not depend on this model at
all. Specificity 1.00 may also be optimistic: corpus negatives come from windows that provably
lack the regressor, whereas prod's wrong candidates are plausible adjacent code — lower real
specificity would push p *up*.

Two side observations: **refuting is cheaper and faster than corroborating** ($0.59/178s vs
$1.01/341s — it stops once it finds the disqualifier), and false refutes concentrate **off-stack**
(45%) versus on-stack (14%).

### 3b. effort `high` vs `max` — max is simply worse

The long-open A/B, run on the **same 51 cases** (selection is deterministic), both arms complete
with zero errors:

| | effort=max | effort=high |
|---|---|---|
| false refutes (of 25 positives) | 7 (0.28) | **4 (0.16)** |
| **clean-label sensitivity** | 14/15 = 0.93 | **15/15 = 1.00** |
| 95% CI | 0.70–0.99 | 0.80–1.00 |
| specificity | 26/26 | 26/26 |
| **total cost (51 calls)** | $40.62 | **$19.89** |
| **mean latency** | 258s | **101s** |

`high` is **2.0× cheaper and 2.6× faster** at equal-or-better accuracy — every one of its 4 false
refutes was on a broken-label case, so it made **zero** errors on clean labels. The sensitivity
edge is a single case and well inside the noise; the cost and latency wins are not.

One dimension favours `max` slightly: it was high-confidence on all 26 negatives, where `high` was
high-confidence on 24. Since only a high-confidence refute moves the band, `high` is marginally
more conservative — which is the safe direction.

**Acted on:** the `effort` default is now `high`. This retires the standing carve-out that "the SO
is the allowed single-shot exception to the no-`effort=max` rule" — no exception is needed, because
`max` lost on its own merits here, not just on cost.

## 4. Corpus label noise — 23% (`spike/audit_corpus_labels.py`)

Auditing all 64 labelled positives in `corpus_ship` with arithmetic + hg only:

| problem | n |
|---|---|
| **IMPOSSIBLE — labelled regressor landed AFTER the crashing build** (median 42.8d; one 2013d) | **10** |
| NO-OP COMMIT — "apply code formatting via Lando", entire diff is whitespace | 3 |
| BUG MISMATCH — changeset's own bug ≠ `regressor_bug` (one is a Fenix UI-test revert) | 2 |
| node not found on hg | 3 |
| **any problem** | **15 / 64 = 23.4%** |
| *(also: labelled regressor is backed out)* | *4* |

**Root cause:** `crashclouseau/eval/corpus.py::_regressor_nodes(bug_ids)` resolves a bug to its
landing node without checking that node against the crash's own build.

**CORRECTION — this does NOT corrupt the calibration.** I claimed above that it did; checking
rather than assuming shows otherwise (`spike/audit_all_regressor_nodes.py`). `metrics._hit` is
`case_nodes & dossier_nodes` OR `case_bugs & dossier_bugs`, where `case_nodes` is the PLURAL
`regressor_nodes` list and `case_bugs` comes straight from Bugzilla `regressed_by` — untouched by
node resolution. So a case can carry a broken `regressor_node` while `regressor_nodes` still holds
the correct changeset. Auditing every node rather than just the singular field:

| | |
|---|---|
| individual nodes audited | 385 |
| junk nodes | 103 (26.8%) |
| positive hits recorded | 54 |
| hits via a cited node in `regressor_nodes` | 50 |
| hits via bug match only | 4 |
| **hits where the matched node was junk** | **1** — and that case still has a clean node |
| **TAINTED hits (no clean node remains)** | **0** |

Every recorded `hit` is legitimate, so **a refit would be a no-op** and the shipped
`p_worth_investigating` table stands. What the noise did corrupt is `regressor_node` (singular),
which §3 fed as "the" candidate — already corrected there (sensitivity 0.72 → 0.93). The earlier
speculation that this explains the inconclusive prompt A/B is therefore withdrawn too.

**Still worth fixing, and fixed:** the labeller is genuinely buggy, so future corpora would repeat
this. `regressor_node` was `sorted(nodes)[0]` — ordered by hash, i.e. picked at random — and nodes
were pooled across bugs so `regressor_bug` could describe a different changeset than
`regressor_node`. Both fixed, plus validation that rejects a landing which postdates the crash
build, is an auto-format/backout/revert/merge/tagging push, or belongs to another bug; survivors
are ordered earliest-first and rejects are kept on the case (`label_rejects`) so a surprising
label is explainable.

Unlike §2's first-seen argument, "a regressor must land before the build that crashes" has **no
caveat** — no signature-reuse confound — so it is safe as a hard filter in the labeller.

## 5. Cost (`spike/signature_age_vs_spend.py`)

$655 over ~3 days = **~$217/day**, avg **$3.27**/crash, **116 of 204** runs over the $2 advisory
cap (which is log-only and never aborts).

Where it goes:

| cut | share of spend |
|---|---|
| runs ending in **abstain** | **81%** ($527) |
| abstains that never even pinned a candidate | 43% ($279) |
| **signatures already >7 days old when the build was made** | **67%** ($439) |
| signatures already >90 days old | 53% ($349) |
| the second opinion itself | **4%** ($27) |

Cost tracks **output tokens** (~$60/Mtok effective, flat across verdicts); prompt cache is already
99.2% efficient, so caching is not a lever. Pipeline models are haiku-4.5 + sonnet-5, not Opus.

The sharpest number: **24 of 32 reported leads came from signatures that were already >7 days
old**, versus 8 from genuinely-new ones. The leads are concentrated exactly where a window-based
lead is structurally impossible.

**A naive lever to avoid:** gating on `seed_score` would be a mistake — 19 of 31 leads came from
`seed_score=0` runs, so that filter would destroy ~60% of the value.

## 6. What was shipped (uncommitted, undeployed)

576 tests green, flake8 clean. Two adversarial review passes (31 findings raised, **3 confirmed**,
all fixed and re-verified).

* `Dossier.second_opinion_status` — `ok` / `failed` / `skipped_*`, `None` when the gate never ran.
  A best-effort pass that breaks in prod is now visible instead of looking ineligible.
* `Dossier.raw_verdict` — pre-gate snapshot (`model_copy(deep=True)`).
* `min_confidence` 50 → **25**, so every displayed lead is measured.
* **`min_boost_confidence` (new, 50)** — review fix. Dropping to 25 had made the fold
  one-directional at the bottom rung: `is_bare_lead` matches `low`, so a corroboration jumped a
  rung-25 lead two rungs (p_worth 0.50 → 0.97) while a high-confidence refute of a `low` lead is a
  no-op. Measurement coverage stays at 25; the band only moves from `medium` up.
* **Applied-move flags** `second_opinion_boosted` / `_clamped` / `_downgraded_strong` — review fix.
  `raw_verdict` alone cannot attribute a move, because the corroboration gate runs first and can
  move the same lead: bump-then-clamp persists raw == shipped == `medium`, identical to the SO
  doing nothing (and from `low` it *inverts*). The older `_corroborated`/`_refuted` flags record
  the SO's *opinion*, not an applied change.
* Both new fields are stripped in `parse_and_validate` (blindness guarantee).

## 7. Recommended next steps

1. **Add the deterministic first-seen check** (signature first-seen buildid vs candidate pushdate)
   as a downweight in `apply_deterministic_gates`. Free, no LLM, and it targets the one cause
   behind 10/10 band-moving refutations. **Downweight, not drop** — see §2's caveats.
2. **Consider it at ingestion too.** 67% of spend is on not-new signatures. Gating there is the
   single biggest cost lever, but it trades away volume-regression recall, so it needs a decision
   rather than a default.
3. ~~Fix `_regressor_nodes()` and refit the calibration table.~~ **DONE (labeller), and the refit
   turned out to be unnecessary** — see §4's correction: 0 of 54 recorded hits are tainted, so the
   shipped table stands.
4. **Keep the second opinion on.** At ~4% of spend and sens 0.93 / spec 1.00 it is the cheapest
   correct signal in the pipeline. Its main current value is as a *measurement*; once (1) lands,
   re-measure, because much of what it catches should be caught for free.
5. Re-check the 2 silent SO failures once `second_opinion_status` is deployed.

## Reproducing

```sh
# free (no LLM)
uv run python spike/verify_so_timing_claims.py  --in /tmp/so_leads.json --out spike/SO_TIMING_VERIFICATION.json
uv run python spike/signature_age_vs_spend.py   --in /tmp/prod_sigs.json --out spike/SIGNATURE_AGE_VS_SPEND.json
uv run python spike/audit_corpus_labels.py      --corpus corpus_ship     --out spike/CORPUS_LABEL_AUDIT.json

# ~$0.90/call
uv run python spike/so_instrument_calibration.py --n-pos 25 --n-neg 26 --effort max --out spike/SO_INSTRUMENT_CALIBRATION.json
```

All need `DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0`. The two `--in` files are
JSON dumps from the canary DB; the queries are in this session's history.

**Socorro gotchas** hit while building these: `_facets_size=10000` → HTTP 400; the `build_id` facet
is ordered by *count*, so truncating it silently drops the oldest build (sort ascending by
`build_id` with `_results_number=1` instead); a date range of exactly 365 days → 400, because the
implicit upper bound is *now* (clamp to 364); `date` filters the crash **report** date, not the
build date, so even a 30-day window surfaces very old buildids.
