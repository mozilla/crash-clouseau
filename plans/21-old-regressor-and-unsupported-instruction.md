# 21 — The regressor older than the window: `absl::PrefetchToLocalCacheForWrite`

Status: **note, not started** (2026-09-06). Written from a hand analysis with the maintainer;
the fixes below are proposals, none is implemented.

## The miss, measured

Bug 2068902 (`Linux startup Crash in [@ absl::PrefetchToLocalCacheForWrite]`), filed by :aryx on
2026-09-03, P3/S3, **no `regressed_by`**, comment 0 only. Clouseau triaged one report of it,
uuid `88c377d7-773d-4678-9e9c-b55f90260818` (nightly 156.0a1, build 20260818092026), on
2026-08-18 and **abstained as hardware**: "SIGILL on a `prefetchw` instruction, which is
architecturally non-faulting, plus the crash reporter's own 'possible bit flip' annotation point
to hardware memory/register corruption, not a software regression." Cost $0.36, 8 turns.

Socorro, all channels, 2026-05-01..09-06:

| axis | value |
|---|---|
| reports | **1146** |
| platform / arch / reason | Linux 100%, amd64 100%, `SIGILL / ILL_ILLOPN` 100% |
| `cpu_info` | **`family 15 model 4` 100%** (six steppings; one `model 3`) — Intel NetBurst, Pentium 4 / Pentium D, 2004–05 |
| first seen | 153.0b11, 2026-07-13 (build 20260710112311) |
| reports on Firefox 152.x | **0** |
| release since 153.0 shipped (07-21) | 1073 reports / **95 installs** (~11 per install: a startup loop) |
| share of ALL Linux release crashes on family 15 in that period | 1073 / 1484 = **72%** |
| nightly | 6 reports on two builds (07-25: 2, 08-18: 4) |
| Core 2 (family 6 model 15 / 23) reports on Linux release, same period | 5821, **none** with this signature |

Root cause, verified from source:

* **Bug 1989886** — protobuf 21.2 → **35.0**, landed 2026-05-20/21, `target_milestone: 153
  Branch`. The new `MessageCreator::PlacementNew` (`message_lite.h:1619, 1637` at the crash rev)
  calls `absl::PrefetchToLocalCacheForWrite`. The pre-update `message_lite.h` has **zero**
  occurrences of `Prefetch` or `MessageCreator`.
* `third_party/abseil-cpp/absl/base/prefetch.h:161-162` (vendored 2025-01-13, unchanged):
  `#if defined(__x86_64__) && !defined(__PRFCHW__)` → `asm("prefetchw %0" ...)`, with the
  comment "PREFETCHW is recognized as a no-op on older Intel processors". True from Core 2 on;
  **false for family 15**, where opcode `0F 0D` is `#UD`. Firefox's baseline x86-64 build does not
  define `__PRFCHW__`. The Core 2 row above is the empirical confirmation of the boundary.
* Every Safe Browsing list update (`ProtocolParserProtobufV5::End` →
  `TcParser::AddMessage` → arena `MessageCreator::New`) executes it, shortly after every startup.

So: a deterministic, software-emitted illegal instruction on a supported CPU, shipped in 153,
crash-looping ~100 release installs, blamed on bit flips by the one run we made.

## Why the pipeline missed it — three independent blind spots

1. **Selection.** Nightly had 6 reports from ~2 installs. `utils.evaluate_days` needs
   `mature_installs` = 4 over `mature_after_days` = 5 → `immature` (07-25), then
   `untestable_prefix` (08-18). Release is not ingested. Nightly volume can never carry a
   regression that only bites 20-year-old hardware; the volume lived on release.
2. **Facts shown to the agent.** `orchestrator._hardware_noise` scopes `sigage.hardware_noise`
   to the crash's own channel (correctly, for the *bit-flip rate*: the docstring's bug 2062219
   case). But that also scopes `cpu_terms` / `top_cpu_share`, so `_cpu_spread_line` saw 6
   reports, never "1073 of 1073 on one family". A 100% single-family concentration over a
   thousand reports is evidence FOR a software fault on that hardware, not against it — the
   opposite of the bit-flip pattern, which scatters across hardware. And `_crash_facts` prints
   `CPU` and `Faulting instruction` side by side with nothing that knows `prefetchw` needs
   PRFCHW and family 15 predates it. The 0.25 bit-flip annotation was read as a verdict.
3. **Window.** `_offstack_window` bounds candidates by the previous build (or `_RISE_WINDOW_HOURS`).
   The agent ran file history, **saw `message_lite.h` last touched 2026-05-20, and discarded it
   as outside the window.** It had the regressor's date in hand. Nothing in the pipeline can
   reach a regressor that landed three months before the crash build, even when the signature is
   provably absent from the previous version.

## Proposed fixes, in the order to do them

### A. Instruction-support fact (small, local, decisive)

In `triage._crash_facts`, next to `("Faulting instruction", ...)`: a table from mnemonic /
operand class to required CPU feature (`prefetchw` → PRFCHW; `tzcnt`/`lzcnt` → BMI1/LZCNT;
`shlx`/`shrx`/`andn`/`bextr` → BMI1/2; `popcnt`; `ymm*` operands → AVX/AVX2; `pclmulqdq`;
`aes*`; `f16c`; `vfmadd*` → FMA; `movbe`; `sha*`), plus a per-vendor family/model floor for each
feature (Intel: PRFCHW from Broadwell, family 6 ≥ 0x3D; BMI/AVX2 from Haswell; POPCNT/SSE4.2 from
Nehalem; **family 15 = none of them**). Emit one line: *"Faulting instruction `prefetchw` requires
PRFCHW; this CPU (family 15 model 4, Intel NetBurst 2004–05) predates it — a SIGILL here is the
compiler/asm emitting an unsupported instruction, not corruption."* No network call.

### B. All-channel CPU concentration line (small)

Keep `hardware_noise` channel-scoped for the *rates*. Add a second, clearly labelled
concentration line computed over all channels when the sample is large enough (≥ 50 reports):
top `cpu_info` family share and report count. State it as concentration, never as a
suppression: `_cpu_spread_line`'s docstring already explains why bare "N of N on one model" is
an abstain instruction; the all-channel line must carry the same background sentence.

### C. Archetype: "unsupported instruction" (small, `archetypes.py`)

Matcher: reason `SIGILL`, faulting instruction in table A, top CPU family share ≥ 0.9 over ≥ 50
all-channel reports, zero reports on the previous version. Guidance: *"Do not look for a
corrupting patch in the pushlog window. Run file history on the crashing frames' files and find
the change inside the affected version that introduced this instruction — usually a vendored
library update or a build-flag change."* Source bug: 2068902.

### D. Version-boundary window (structural — wants its own sub-plan before code)

When `sigage.signature_history` shows the signature **absent on the previous version and present
on this one across channels**, the candidate range should be *branch point of the affected
version → first-seen build*, not the previous-build window. Weeks of pushlog are too many to
enumerate, so filter by **files on the stack**: file history (`mcp__history__file_history`, which
the agent already calls) on the top-N frame files, admit only changesets inside the range. For
this crash the result is exactly one candidate, bug 1989886. Cheaper interim rule: a changeset
that touched a crashing-frame file **after the last clean version** is never discarded as
"outside the window", regardless of `_offstack_window`.

### E. Borrowed maturity in the selector (decides whether a run happens at all)

In `utils.evaluate_days`: treat a nightly signature as mature when it is present on nightly at
all and `total_other_channels` (already computed by `signature_history`) shows real volume with a
clean previous version; and/or count **crashes per install** — ≥ 5/day per install is a startup
loop and should pass maturity at two installs. This is what turns "one abstain" into "a run with
the facts from A–D".

## Keep as is

The bit-flip annotation stays in the prompt, but with its confidence stated ("0.25, the lowest
Socorro assigns"), so a weak hint reads as weak. The channel scoping of the bit-flip *rate* stays
(bug 2062219).

## Replay set

* `88c377d7-773d-4678-9e9c-b55f90260818` — must become a lead on bug 1989886, or at minimum an
  abstain of kind `no_candidate_explains_it` that names the version boundary, never `hardware`.
* Any Raptor Lake (`family 6 model 183 stepping 1`) abstain from the existing corpus — must stay
  an abstain: A–C must not turn genuine defective-silicon reports into leads. The distinguishing
  fact is instruction support, not concentration alone.
