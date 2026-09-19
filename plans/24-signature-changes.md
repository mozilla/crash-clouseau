# Plan #24 — Signature changes: the same crash under a new name

> **Status:** audit + design 2026-09-18; **implemented 2026-09-19** (`crashclouseau/sigfamily.py`, steps 1-6 below; `bin/replay_sigfamily.py --acceptance` holds 12/12 on live crash-stats). Uncommitted at the time of writing. Trigger: Calixte relaying
> reviewer feedback — *"Clouseau seems to struggle with signature changes too (at least
> what I noticed from various JS crash bugs it filed)"*: a crash `A | B | C | D` becomes
> `A | E | C | D` when `B` is renamed, and Clouseau treats the new string as a new crash.
> Every claim below is measured against BMO (the ~150 crash bugs filed by
> `cdenizet@mozilla.com` and `clouseau-bot@mozilla.tld` since 2026-08-05), the prod
> dossiers, and crash-stats SuperSearch. Related: plan 17 (dedup beyond the signature),
> whose Step 3 (proto-signature family) is the venue half of this problem and was never
> built.

---

## 1. What a "signature change" is, in the filings we made

The Socorro signature is a lossy projection of the stack: skip lists, inlining, template
decoration, symbolication and Socorro-side prefixes all change the string without the
crash changing. Six distinct mechanisms produced filings a human then corrected:

| # | mechanism | our filing(s) | what the human said | what we should have done |
|---|---|---|---|---|
| 1 | **New infrastructure frame not on Socorro's skip list.** Chromium sandbox update introduced `logging::(anonymous namespace)::CheckLogMessage::~CheckLogMessage`; `logging::LogMessage::~LogMessage` and five sibling classes are on `irrelevant_signature_re.txt`, this one is not, so every sandbox `CHECK()` failure moved onto one new name. | **2073210** (release, `actionable`, ni :bobowen) | bobowen c1: *"This looks like a signature change for the various CHECK messages. These are probably mainly bug 1737467 and friends. We'll need to get these signatures ignored."* | Comment on bug 1737467 (open since 2021, whose `cf_crash_signature` history reads `LogMessage::~LogMessage` → `PatchNtdll`), attach the new signature there. Frame 4 of our own stack IS `sandbox::InterceptionManager::PatchNtdll`, the old signature. |
| 2 | **Code change altering the abort path.** bug 2070649 made `AutoEnterOOMUnsafeRegion::crash(size, reason)` forward the size: `OOM \| unknown` → `OOM \| large` AND a second `crash_impl` frame appeared. | **2071620** (INVALID), **2071606** (DUP) — both spike filings | iireland c3: *"This isn't a regression. I just fixed some of our OOM reporting ... which is causing us to report `OOM \| large` where we would have previously reported `OOM \| unknown`."* | Nothing. **The model said so itself**: comment 0 of 2071606 reads *"This signature is a renamed form of a longstanding SpiderMonkey OOM abort, not a new crash"*, 2071620 measured *"706 reports on builds BEFORE 20260911092915 ... exactly 0 reports FROM 20260911092915 on"* for the predecessor. The spike filer has no decline for a re-bucketing; it filed both. |
| 3 | **Vendor bump moving the failure point.** wgpu update turned a `MOZ_RELEASE_ASSERT` in `WebGPUParent::MapCallback` into a Rust panic in the callee `wgpu_server_buffer_get_mapped_range`. | **2069647** (DUP → 1976766), then **2070711** (DUP → 2069647) | ttanasoaia c3 (2070711): *"the bot is not checking / tracking previous issues that have been closed as duplicates or other bugs that contain the signature already."* | `WebGPUParent::MapCallback` is frame 12 of our stack and reported 14–44/day on nightly until 09-05; bug 1976766 (open) carried it. Comment there. The second filing is the dup-following gap fixed in `c802db7`; the FIRST is this plan. |
| 4 | **Symbolication gap.** A Windows/Linux build Socorro had no symbols for writes module names into the signature. | **2070554** (INVALID, `ntdll.dll \| kernelbase.dll`), **2069648** (DUP of our own 2065373, `libxul.so (deleted) × 10`), **2061962** (DUP of our own 2061960, `xul.dll \| _PR_MD_UNLOCK \| PR_Unlock \| xul.dll`) | jld c2 (2069648): *"Duplicate of 2065373, but with missing symbols."* | 2070554: predecessor `shutdownhang \| WaitOnAddress` ran 116 reports in the prior 28 d and stopped the day the new name appeared (`f02fffc` now withdraws the novelty claim for module frames, but the filing still happens). 2069648/2061962: a signature whose only symbols are module names is unsymbolicated — `_is_unsymbolicated` only recognises bare addresses. |
| 5 | **Inlining / template / demangling variants of one frame set.** Windows spells `style_traits::owned_slice::impl$1::drop`, Linux `<style_traits::owned_slice::OwnedSlice<T> as core::ops::drop::Drop>::drop`; `drop_in_place` vs `drop_in_place<T>`; a delegated constructor adds a second `mozilla::BitSet<T>::BitSet` frame on one platform. | **2072875**, **2073159** (DUPs of our own 2072488, two days later, different spellings); **2070376** (BitSet: filed on 1 report while 5 sat under the 3-frame spelling since 09-05); **2067059** | jstutte c2 (2067059): *"Signature search undercounts this because the site produces different signatures depending on inlining"* — 13 crashes under three signatures, we said 4. jcoppeard c2 (2070376): *"There is a single crash instance so far."* | Venue and volume by family, not by string. `utils.lambda_siblings` already does this for one transform (lambda demangling); it is the right idea applied to one case. |
| 6 | **A diagnostic added by a fix mints a new signature for an old condition.** bug 2066354's fix added a `MOZ_DIAGNOSTIC`/release assert on the LoadURI path. | **2071287** (FIXED, but regressor wrong) | g3b034lff c1: *"This is not regressed by bug 2066354, it's just another special case of the same, and is in fact regressed by bug 2048793 — this crash is already present in 155.0.x release."* | Name the exposer as exposer. The model has the concept (it called bug 2068336 an EXPOSER on 2070988, correctly) and did not apply it here. |

Also in scope because the same instrument answers it: **2060920** (`OOM \| unknown \|` prefix
added to `memcpy_repmovs_Intel \| RTCEncodedFrameBase`; the 2-frame predecessor ran the week
before and stopped the day the OOM form appeared).

**Prevalence** (prototype detector, §3, replayed over the 150 filings; `/tmp/sigchange/refine.py`,
695 SuperSearches). The build-keyed handoff test flags **18 of 150**; hand-checked, **13 are the
same crash under an older name** (the five human-confirmed cases above that it can see —
2073210, 2071620, 2071606, 2070554, 2069457 — plus 2072770 `shutdownhang |
CanEnterBaselineJIT` → `shutdownhang | js::jit::CanBaselineInterpretScript`, a pure function
rename we titled `[new in release]` although the old name has 756 reports; 2069758
`WlLogHandler` → `~WaylandSurfaceLock`; 2071369, 2071544, 2067481, 2063864, 2066113,
2067511). The 5 false flags are AsyncShutdownTimeout blocker lists co-varying (3), the
`EnterJit` trampoline (1) and a different caller frame (1) — all excluded by treating blocker
names and JIT entry frames as non-identity. **17 of the 18 had an open bug on the old name at
filing time.** A further **7 filings carry no symbol at all** (module-only names:
`libvulkan_radeon.so`, `libc++abi.dylib`, `libvulkan.so.1`, `libxul.so (deleted) × 10`,
`xul.dll | _PR_MD_UNLOCK | PR_Unlock | xul.dll`, `shutdownhang | libc.so.6 | …`, `amdxx64.dll |
RtlAllocateHeap`) and 14 are live spelling variants of a family (4 with an open venue). So
roughly **one filing in eight was named for a crash that already had another name**, and the
wgpu case (2069647) needs the full stack: `MapCallback` is frame 12, past the 12-frame window
the prototype read.

**Where it clusters.** 7 of the 9 rename-caused filings above are from 09-05 onwards. Two
reasons: release filing was armed 09-07 and release is where old names have the longest
histories; and a JS OOM reporting change, a wgpu bump and a Chromium sandbox update all
landed in the same fortnight. It will keep happening: Socorro's skip lists changed 9 times in
the last year (2025-11-06 *"Better triage for chromium sandbox CHECK failures"* is the
commit that made #1 possible, by listing `LogMessage` siblings but not the class Chromium
added later; 2026-09-15 *"Improve signatures for wgpu storage lookup failures"* landed the
day our wgpu filings were being duped).

## 2. Why the pipeline cannot see it today

The signature string is the crash's identity everywhere that matters:

* **Novelty.** Three instruments, all keyed on the exact string: `sigage.signature_history`
  (364-day SuperSearch), `sigage.first_seen_ever` (`SignatureFirstDate`), and the
  reprocessing inversion `signature_rename_suspected` (`sigage.age_facts`). The last is the
  ONLY rename detector and it has fired **3 times in 5968 prod dossiers** — by construction
  it can only see crashes re-signatured by reprocessing, never a new frame, a symbol gap or a
  moved abort. Its own comment says so: *"the biggest artefact classes — a
  MOZ_DIAGNOSTIC_ASSERT prepended to an ancient crash site, a driver frame decorating it —
  mint a name that genuinely never existed before and leave no inversion at all."*
  `novelty_facts` (`module_frames`, `late_first_report`, `f02fffc`) withdraws "new" in two
  situations; it does not know what the crash WAS called.
* **The prompt knows the gap.** `_NEW_SIGNATURE_GUIDANCE`: *"The one thing that would undo
  it is a renaming — an old crash re-signatured onto a new name — and where we can detect
  that, it is said above."* We almost never can.
* **Venue.** `_open_bugs_for_signature`, `_fixed_after_build_bug`, `_known_on_train_bug`,
  `resolve_venue_below_public` all take the string (plus its lambda spellings). Plan 17 §3
  proposed the proto-signature family for exactly this and was not built.
* **Spike selection.** `spikes.judge_build_day` sees `0,0,0 → N` on the new name and calls
  it an appearance; `spike_report.is_new_signature` then earns the `[new in release]`
  prefix. Nothing asks whether another name went `N → 0` on the same build.
* **Nothing consumes the model's own finding.** `SpikeFindings.status` carries the
  structured population label (`longstanding`, ...) and `summary` said "renamed form" in
  plain words on 2071606; `file_spike_bug` reads neither. In the ordinary triage path there
  is no verdict/abstain reason for "same crash, new name".
* **Volume.** `report_bug.fetch_signature_stats` counts the string; jstutte's 13-vs-4.
* **Unsymbolicated.** `_is_unsymbolicated` = every part is a bare address; module-only
  names pass.

## 3. The instrument: predecessor and sibling signatures (`sigfamily`)

One deterministic lookup at seed time, from data we already hold (the signature S, the
crash's `proto_signature`, build, channel), answering: **what else has this crash been
called, and did the old name stop when the new one started?**

### 3.1 Candidate siblings — two queries plus generated spellings

1. **Frame-variant siblings.** For each *specific* frame of S (not on Socorro's
   irrelevant/prefix lists, not a module name, not `OOM`/`large`/`unknown`/`shutdownhang`
   etc.): SuperSearch `signature=~<frame>`, `_histogram.date=signature` (or
   `_histogram.build_id`), product + the crash's channel (`aurora` folded into beta as
   usual), retention window. Keep signatures whose normalised frame list is within edit
   distance 2 of S and shares ≥ 1 specific frame. Normalisation: strip `<...>` template args,
   `impl$N`, lambda suffixes (superset of `utils.lambda_family`), and map the Rust trait
   spelling `<X as Trait>::m` / `impl$N::m` onto the module path + method.
2. **Pushed-down siblings** (the rename case proper). For the first three specific frames of
   the crash's proto that are NOT in S — in stack order, after skipping frames Socorro itself
   skips, so a Rust panic's `panic_hook`/`panic_fmt` machinery never becomes an anchor —
   SuperSearch `proto_signature=~<frame>` faceted the same way. Keep signatures whose
   specific frames all appear in S's proto: the old name is still in the stack, one or more
   new frames took over the string. This is CheckLogMessage → `PatchNtdll`, wgpu →
   `WebGPUParent::MapCallback`, ntdll → `shutdownhang | WaitOnAddress`.
3. **Generated spellings**, queried exactly: `OOM | unknown|large|small` prefix variants,
   with/without `shutdownhang |`, lambda spellings.

Two to four SuperSearches, one round-trip via `libmozdata` `Query` batching, same
best-effort contract as `signature_history` (a failure yields no facts, never "new").

### 3.2 Classification — the timeline, on the crash's own channel

For each sibling P against S's first appearance on the channel (`build_id`-keyed, not date,
where the crash has a build):

* **handoff** — P had ≥ 5 reports in the 28 days before S's first build and its rate
  after is ≤ 25% of before (or ≤ 2 reports when S is under 3 days old), and P's last report
  is within 3 days of S's first. **S is P renamed.** The crash's age is P's
  `SignatureFirstDate`.
* **coexisting** — both live before and after: one crash, several names (inlining,
  platform spelling). Venue and volume must include P; novelty unchanged.
* **older-variant** — P is older and still live, S is a new spelling: venue includes P;
  S's novelty is doubtful, say so.

Two discriminators worth recording as facts, because they change what the model may claim:

* **build-aligned vs date-aligned.** A code rename hands off at a build boundary on one
  channel first; a Socorro skip-list change (or a symbol gap) hands off on a DATE across
  every live build and version at once. The second cannot have a changeset as its cause.
* **fan-in.** One new name absorbing ≥ 2 predecessors with different stacks below the new
  frame (CheckLogMessage took every sandbox CHECK) is a **catch-all** minted by a generic
  frame. The right action is a Socorro skip-list change, not a crash bug; the filing should
  say so and name the frame.

### 3.3 Consumers

| where | change |
|---|---|
| `orchestrator.build_seed` | call `sigfamily.lookup(signature, proto, product, channel, buildid)`; seed keys `signature_predecessors` (handoff), `signature_siblings` (coexisting/older), `signature_family_first_seen_ever` (min over S + handoff predecessors), `signature_handoff_alignment` (`build`/`date`), `signature_fan_in`. Recorded into `corroborations` by `_record_signature_age_facts` (declare in `corroborations.REGISTRY`; the registry test enforces a reader per flag). |
| `triage._signature_age_lines` | a `SIGNATURE RENAME:` block when a handoff exists: name P, the frame that changed, P's volume before/after, P's first-seen. State the crash's age from the family clock. Guidance: a candidate that only renamed/moved/added the changed frame is the renamer, not the regressor; judge the crash on P's history; if the alignment is `date`, no changeset can be the cause. Extend `novelty_facts` with reason `predecessor_handoff` so the existing `_novelty_caveat` machinery withdraws "new" in the bug text too. |
| `_apply_signature_age_gate` | clock = the family's first-seen for handoff predecessors only (a coexisting sibling is not proof of age). Same pushdate-vs-first-seen comparison, so a renamer landing 2 years after P's first report gets the existing downweight. Keep `TestGateClockIsUnchanged` for the no-predecessor case. |
| `bugzilla_apply` venue functions | search S ∪ handoff predecessors ∪ coexisting/older siblings (the `spellings` loop in `_open_bugs_for_signature` already iterates a set — widen it; same for `_fixed_bugs_about`, `_known_on_train_bug`). A venue reached through a sibling is commented with one extra sentence (*"since build B this crash reports under `S`; frame X became Y"*) and **gets `[@ S]` appended to `cf_crash_signature`** — Calixte's rule from bug 2063003, and what closes the loop for Socorro and the next triager. |
| `spike_escalation._sweep_channel` / `file_spike_bug` | before spending a run: if S's spike is a handoff (a predecessor lost what S gained on the same build), record the escalation `done` with `skipped="re-bucketing from <P>"` — the pre-LLM decline that would have stopped 2071620/2071606 for $0. `is_new_signature` false when a handoff or older sibling exists, so `[new in release]` is not printed. `siblings` in the brief become the family, so `resolve_venue_below_public` and `classic_runs` see it. |
| `report_bug.fetch_signature_stats` + the volume sentence | count S and its coexisting siblings; print *"N crashes under this signature, M more under sibling spellings P1, P2"*. |
| `bugzilla_apply._is_unsymbolicated` | also true when the name has no *specific* frame at all: every part is a bare address, a module name (`libxul.so (deleted) × 10 \| libnspr4.so (deleted)`) or a frame on Socorro's own prefix/irrelevant lists (`xul.dll \| _PR_MD_UNLOCK \| PR_Unlock \| xul.dll`: the two NSPR lock frames are generic, the crash's own frames are all `xul.dll`). Such a name cannot be searched, blamed or deduplicated; decline and let the symbolicated sibling file. |
| `agent/prompts/system.md` | the exposer rule the model already applies sometimes, made explicit: a change that ADDS an assert/CHECK/diagnostic or moves an abort names the exposer; the regressor is whoever made the condition true — and when the family clock says the condition predates the exposer, say "exposed", never "regressed by". |

Kill switch only (`agent.signature_family.enabled`), no rollout flag — it is an external
Socorro dependency, which is the one case a switch is for.

## 4. Validation

1. **Replay over the 150 filings** (`/tmp/sigchange/sweep_ch.py` → `analyze.py` →
   `venues.py`, ~25 min, no BMO write). Acceptance, case by case:
   2073210 → venue 1737467 via pushed-down `PatchNtdll`; 2069647 → venue 1976766 via
   `WebGPUParent::MapCallback`; 2071620/2071606 → spike decline (handoff from the
   `OOM | unknown` forms on nightly); 2070554 → handoff from `shutdownhang | WaitOnAddress`,
   date-aligned; 2069648 → unsymbolicated decline; 2072875/2073159 → venue 2072488 via
   family spelling; 2070376 → volume 6 not 1.
2. **What must NOT change**: FIXED filings on old, reused signatures still file —
   2061960 (`nsFind`, 326 d old), 2062286 (`FindSafeLength`, 2010), 2062119
   (`nsJARProtocolHandler::MimeService`, 2014). A coexisting sibling must never clamp; only a
   handoff moves the clock.
3. **Cost**: 2–4 SuperSearches per seed at ~300 ms each on a 20-minute run; the spike
   pre-check replaces a $3–8 model run each time it fires.

## 5. Not fixed here

* The Socorro skip lists themselves. When fan-in fires, the bug can *propose* the
  `irrelevant_signature_re.txt` entry (bobowen: *"We'll need to get these signatures
  ignored"*); filing that PR is a human's call.
* JS generic OOM buckets spiking on nightly (2072063, 2070317): a rate problem on a
  catch-all, not a rename. Plan 19 territory.
* `2071287`-style exposer attribution is a prompt rule here, not a gate; whether the model
  applies it is a measurement to take after it ships.
