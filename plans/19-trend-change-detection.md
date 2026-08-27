# 19 — Trend-change detection: catching what the spike rule structurally cannot

## The miss, measured

Bug 2063336 (`AsyncShutdownTimeout | profile-before-change | CookiePersistentStorage:
cookies.sqlite closing`), filed by :aryx on **2026-08-13**, RESOLVED FIXED 2026-08-19 by baku
("cap the cookie database WAL size in bytes instead of pages", uplifted to beta and esr153).
Aryx's stated reason for filing is the whole problem:

> "Signature went from single digit crashes per version to 14 reports from 14 installs of
> Firefox 155.0a1."

Firefox **nightly**, 2026-03-01..08-26, that signature:

| axis | value |
|---|---|
| total nightly reports, 6 months | **24** |
| per nightly version | 150.0a1: 1, 153.0a1: 1, 154.0a1: **0**, 155.0a1: **19**, 156.0a1: 3 |
| max reports on any single crash-day | **3** (2026-08-12) |
| max reports on any single BUILD-day | **2** |
| distinct buildids carrying the 24 reports | **18** |

The deployed detector (`utils.is_spike`, nightly `floor=3`, `ratio=3`, baseline = `max` of the
preceding `ndays=3` **build-days**) needs `n >= 3` **and** `n >= 3 * max(before)` on ONE
build-day. This signature's ceiling is 2. **No knob setting of the current rule can fire on it**
short of `floor=1, ratio=1`, which fires on everything. The blind spot is not a threshold, it is
the *aggregation window*: a signature whose rate goes from ~0.03/day to ~1/day carries a 30x
change and never puts 3 crashes on one build-day.

This is a THIRD blind spot, distinct from the two already recorded:
* `untestable_prefix` — the oldest `ndays` build-days are never tested (`utils.evaluate_days`).
* new-signature novelty — `sigage` first-seen is blind to brand-new signatures.
* **this one** — a multiplicative rate change too slow and too quiet to land 3 crashes on one
  build-day. Aryx's rule aggregates over a ~4-week VERSION; ours over a 1-day build.

## What the study must answer

1. **How big is the class?** Of the human-filed crash bugs we did not file, how many are
   "existing signature, volume went up" rather than "new signature"? (449 crash bugs with a
   signature since 2026-03-01; 76 from aryx, 124 with `regressed_by`.)
2. **Which statistic catches them**, at what latency, for what alarm budget?
3. **Can the change point be localized well enough to name pushlogs?** A 7-14 day accumulation
   window buys sensitivity and pays in a fuzzy start date. How many nightly builds / pushes wide
   is the resulting candidate window, and does the true regressor fall inside it?
4. **Is the cause even in the tree?** A rate change can come from the population (a Windows
   update, a third-party DLL, a server-side rollout) rather than from a push. Sending those to
   the LLM pipeline spends money on an unanswerable question. Measure the share and find a
   cheap discriminator.

## Replay discipline (the part that invalidates a study if it is wrong)

* **`date` in Socorro SuperSearch IS `processed_crash.date_processed`** (verified against
  `/api/SuperSearchFields/`). So slicing the panel at `date < D+1` reproduces exactly what was
  visible on day D — no late-arrival leakage. This is what makes an as-of replay honest, and it
  is the one fact the whole harness rests on.
* **Deployed cadence, never retrospective.** Every detector is evaluated once per run-day with
  only days `<= D`, the same way `update.py` would have run it. (The Fenix study measured 36 vs
  246 selections on this exact distinction.)
* **INSTALLS, not reports, for anything that looks like volume.** One machine produced 81,843 of
  86,196 reports in a past measurement; 7 of the 59 loudest nightly signatures came from ONE
  installation. Every statistic is computed both ways and the report-based variant is only kept
  where it beats the install-based one.
* **`aurora` IS beta** — `utils.get_search_channel`. Omitting it loses ~36% of beta reports.
* **Tune/holdout split.** Feb 1 - May 31 tunes, Jun 1 - Aug 26 tests. A threshold fitted on
  2063336 (or on any single motivating case) is overfitting by construction; the miss is the
  *hypothesis generator*, never the *validation set*.
* **Multiple testing is real.** ~250-300 distinct nightly signatures per day, ~2,000+ over a
  season. A nominal p < 0.01 per signature per day is ~2-3 alarms/day from noise alone. Report
  BH-FDR and the raw alarm count, never the nominal p alone.

## Data

`spike/trend/00_universe.py` -> `cache/universe/<channel>/<day>.json.gz`, one query per
(channel, day), 2026-02-01..2026-08-26 (206 days):

    _aggs.signature = [build_id, _cardinality.install_time]
    _aggs.build_id  = [_cardinality.install_time]
    _facets         = [_cardinality.install_time]      # channel exposure for the day

giving, per day: channel reports + distinct installs; per build: reports + distinct installs;
per signature: reports, distinct installs, and the per-build breakdown. ~70 KB/day, 0.6 s/query.
Nightly (295 sigs/day) and beta (289 sigs/day) fit under `_facets_size=2000` with no truncation;
release (3,196 sigs/day) is NOT collected as a universe — it is queried per-signature as an
environmental control.

`01_groundtruth.py` — the 449 crash-keyword bugs with `cf_crash_signature` since 2026-03-01,
their comment 0 (the filer's stated reason), creator class (human triager / bot /
intermittent-filer / **us**), `regressed_by`, and resolution.

`02_casepanel.py` — for every ground-truth signature plus a matched control sample: the daily
series on nightly, beta+aurora, release and esr, so a nightly-only rise can be told from an
all-channel one.

## Detector families (all replayed as-of, all on both report and install counts)

| id | family | why it might beat the current rule |
|---|---|---|
| F0 | deployed `is_spike` on build-days | the baseline to beat |
| F1 | F0 with knobs moved (floor 1-3, ratio 1.5-3, ndays 3-14) | cheapest possible fix; must be measured before anything is built |
| F2 | rolling-window count ratio, crash-date axis (W in 3/7/14 vs B in 14/28/56) | a 7-day sum is inherently free of weekly seasonality |
| F3 | Poisson exact test, count in W vs baseline rate x exposure | low volume handled honestly; a p-value maps to a fixed alarm budget |
| F4 | F3 on distinct installs | kills the one-machine flood |
| F5 | EWMA / CUSUM on the install-normalised rate | built for slow drift, which is the miss |
| F6 | per-version cumulative (Aryx's own rule), corrected for days-into-cycle | the human's rule; the correction is the trap |
| F7 | build-axis rolling rate: crashes per 1,000 install-exposures of the last K builds | localises to a build range instead of a day |
| F8 | cross-channel differential: nightly rate change minus release rate change | separates in-tree causes from environmental ones |
| F9 | segment-conditional (largest platform / version slice) | a rise concentrated in one slice is diluted channel-wide |

## Metrics

* **Recall / lead time** — per positive bug: did the detector alarm on that signature before
  `creation_time`, and how many days before. Credit is only given for cases F0 does NOT already
  catch (no double-counting the current detector's work).
* **Cost** — alarms per run-day over the whole universe, at each operating point, plus what the
  alarms are made of (boilerplate `OOM | small` / `shutdownhang` / `IPCError` share). The
  pipeline runs ~85-120 dossiers/day today; a candidate detector is priced in added runs/day.
* **Seasonality sanity** — alarms by weekday. A detector that fires every Monday is measuring
  the work week, not the code.
* **Attribution** — for each alarm: change-point estimate and interval, number of nightly builds
  and pushes inside it, and (where `regressed_by` exists) whether the true regressor's pushdate
  is contained.
* **In-tree vs environmental** — share of positives whose rise is nightly-only (candidate
  in-tree cause) vs simultaneous across channels (population/environment).

## Known traps, stated up front

* ~~**Nightly build exposure decays in ~1-3 days**, so build-aligned and date-aligned changes are
  nearly unidentifiable on nightly.~~ **MEASURED AND INVERTED** (see REPORT.md §11). Nightly
  exposure by build age over 166 days: 9.3% same-day, 50.3% within 2 days, but **27.2% on builds
  >=7 days old and 16.9% on builds >=14 days old** — 50.8% of the first week's exposure after a
  change still sits on builds that predate it, so the test needs only ~5 crashes. The trap was
  real for the change-point ESTIMATOR (whose interval is a median 16 days wide and not
  distinguishable from noise), not for identifiability. What actually localises is a flat
  **24-hour window on the build axis**, and it is a hard knee.
* **Signature renames.** Socorro changes signature generation; an old signature dying and a new
  one appearing is not a trend change. Check every positive for a same-day mirror-image drop.
* **A signature is shared across applications** — Thunderbird and SeaMonkey report Gecko
  signatures. The panel is `product=Firefox`, so this is contained here, but the ground-truth
  bugs are not: exclude Thunderbird-family components.
* **Our own filings** (`cdenizet@mozilla.com`, 63 of the 449) are not ground truth for a miss.
* **`release-mgmt-account-bot`** files from its own volume heuristics — a separate, interesting
  class, but a bot's rule is not a human's judgement. Kept, labelled, reported separately.
