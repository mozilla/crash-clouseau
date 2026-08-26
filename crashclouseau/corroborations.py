# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""What every ``dossier.corroborations`` key means, and who reads it.

A CORROBORATION FLAG IS THE ONLY COUPLING MECHANISM BETWEEN GATES. ``apply_deterministic_gates``
runs fifteen gates in a fixed order over one verdict; none of them calls another, and the only
way an earlier finding reaches a later decision is a key in this dict. Until this module there
was no list of them, and the 2026-08-21 overfitting audit found three separate defects that are
all the same structural gap:

* ``stale_signature_clamped`` had ONE writer and ZERO readers for the three days it existed. The
  gate lowered a lead's confidence for 202 days of signature staleness, the second-opinion fold
  put it back, and the filed bug said neither — a reader saw p_worth 0.9714, byte-identical to a
  clean rung-70 lead.
* ``_fold_second_opinion`` decided whether a corroborating second opinion may re-inflate a
  suppressed lead by testing ONE flag NAME (``downgraded_from_strong``), so the stale clamp —
  added three days later, and the only other suppression that runs before the fold — escaped it
  silently. That is now a declared policy per gate (``_SO_BOOST_POLICY``), not a name.
* ``models._INSTANCE_SUPPRESSED`` — the list that decides whether a suppression closes a
  proto-signature cluster FOREVER or only for this crash — names three of the eight suppressions,
  and the five omissions were never discussed anywhere.

So the rule this module enforces is not "document your flags". It is: A FLAG THAT NOTHING READS
IS EITHER DEAD OR AN UNFINISHED FEATURE, AND WHICH ONE IT IS MUST BE A DECISION SOMEBODY WROTE
DOWN. ``tests/test_corroboration_registry.py`` scans the tree for corroboration writes and fails
on any key that is not declared here, on any declaration nothing writes, and on any change to the
write-only set — so a new flag with no reader cannot be added by accident, only on purpose.

WHAT THIS IS NOT. It is not a schema and it does not validate at runtime: the dict stays a plain
dict, gates keep writing it directly, and a flag missing from a payload is normal (most gates do
not fire). Enforcing at write time would mean touching fifty call sites in the gates, which is
the refactor most likely to change behaviour while claiming not to.
"""

# The five KINDS, which are about what a flag DOES to the run, not what it describes:
#
#   evidence     a fact the run established, recorded for the reader and for later measurement.
#   promotion    it raised the verdict's confidence rung.
#   clamp        it lowered the rung by one, leaving a reportable lead.
#   suppression  it turned the verdict into an abstain: nothing is filed.
#   diagnostic   deliberately read by nothing in the pipeline — it exists so a decision can be
#                re-measured later from the persisted dossiers. This is a legitimate answer and
#                it is why the registry asks for the kind rather than just "has a reader".
KINDS = ("evidence", "promotion", "clamp", "suppression", "diagnostic")

# Readers that are not a file: a flag can be read by being a MEMBER of a declared list, which no
# grep for `get("flag")` will ever find. Both lists are pinned against this registry by the test.
POLICY_READERS = ("policy:_INSTANCE_SUPPRESSED", "policy:_SO_BOOST_POLICY")

# flag -> (kind, readers, note). `readers` is empty ONLY for a `diagnostic`.
#
# Keep the note to what a reader of the FLAG NAME could not guess. Where the flag has a gate with
# a full docstring, point at it rather than restating it.
REGISTRY = {
    # -- the fault-address <-> struct-field corroboration, and its promotion -----------------
    "fault_address_offset_match": (
        "promotion", ("agent/orchestrator.py", "templates/crashstack.html"),
        "The one deterministic PROMOTER in the pipeline. Verified against searchfox at gate "
        "time since 2026-08-21; before that it was model-reported while the docstring called it "
        "'a signal the model cannot fabricate'."),
    "fault_offset": ("evidence", ("agent/orchestrator.py", "templates/crashstack.html"), ""),
    "fault_field": ("evidence", ("agent/orchestrator.py", "templates/crashstack.html"), ""),
    "fault_type": ("evidence", ("agent/orchestrator.py", "templates/crashstack.html"), ""),
    "fault_offset_unverified": (
        "diagnostic", (),
        "Why a struct_layout citation did NOT promote: `refuted` (searchfox says another field "
        "starts there — a model problem) vs `unresolved` (searchfox could not answer, or the "
        "offset is inside an unenumerated base-class subobject — a tool problem). The two must "
        "not share a bucket or the refuted count means nothing."),
    "prior_signature_match": (
        "promotion", ("agent/orchestrator.py", "templates/crashstack.html"),
        "Ties to the CANDIDATE, unlike its sibling above, and carries a focus guard "
        "(exactly one in-window prior)."),
    "prior_regressor_bug": ("evidence", ("agent/orchestrator.py",), ""),

    # -- the off-stack precision gates (dead in prod while offstack.enabled=false) -----------
    "call_path_verified": (
        "diagnostic", (),
        "SF-3's positive answer. The gate acts on its ABSENCE, so nothing reads the flag."),
    "exposer_signals": ("evidence", ("report_bug.py",), ""),
    "exposer_strong": ("evidence", ("report_bug.py",), ""),
    "exposer_suspected": (
        "diagnostic", (),
        "The weaker exposer band. Since 2026-08-21 the exposer clamps to `probable` rather "
        "than `medium`, so a poison fault no longer costs the filing floor by itself."),
    "offstack_observe_only": (
        "evidence", ("bugzilla_apply.py",),
        "Canary switch: the filer must not act on an off-stack verdict while observing."),
    "downgraded_from_strong": (
        "clamp", ("policy:_SO_BOOST_POLICY",),
        "Written by `_downgrade_to_lead_or_abstain` for the exposer/SF-3 downgrades. Read ONLY "
        "through the boost policy, which is why a grep for it finds no reader."),

    # -- the second opinion ------------------------------------------------------------------
    "second_opinion_corroborated": (
        "evidence", ("templates/crashstack.html",),
        "The SO's OPINION, set even when the band never moved. Not the same as `_boosted`."),
    "second_opinion_boosted": (
        "promotion", ("report_bug.py", "templates/crashstack.html"),
        "The boost was APPLIED. 14 of the 29 fileable verdicts in one prod month owe their rung "
        "to this, making it the pipeline's single largest promoter."),
    "second_opinion_refuted": (
        "evidence", ("report_bug.py", "templates/crashstack.html"),
        "Since 2026-08-24 it also PRINTS, via `build_dissent_note`. The symmetry with "
        "`_corroborated` is deliberately broken: on the 500-dossier snapshot 17 of 17 filed bugs "
        "carried a corroborating SO (entropy zero, and both known-wrong filings carried one at "
        "confidence `high`), so publishing agreement is authority inflation while publishing "
        "disagreement is the only variance the reader can act on."),
    "second_opinion_clamped": (
        "diagnostic", (),
        "A refutation cost the lead one rung. Correctly write-only: it clamps a `probable` lead "
        "to `medium` = rung 50, below `autofile.min_confidence`, so no bug comment exists to "
        "print it on. Contrast `_clamped_strong`, which lands ON the filing floor."),
    "second_opinion_clamped_strong": (
        "clamp", ("report_bug.py", "templates/crashstack.html"),
        "A `medium` refutation costs strong-evidence one band, to a `probable` lead. Until "
        "2026-08-24 this case set `_refuted` and moved nothing, so the top rung was the only one "
        "a blind refutation could not touch (`ca6ebc17`, culprit/85). The clamped verdict is "
        "still ABOVE the filing floor, which is why this one must print."),
    "second_opinion_downgraded_strong": (
        "diagnostic", (),
        "A high-confidence refutation took strong-evidence down to a lead."),
    "second_opinion_abstained": (
        "diagnostic", (),
        "A refutation abstained a weak lead. This is the invisible channel: an abstain files no "
        "bug and never reaches `Feedback`, so this flag is the only trace the run leaves."),

    # -- signature age ------------------------------------------------------------------------
    "stale_signature": ("evidence", ("report_bug.py", "templates/crashstack.html"), ""),
    "signature_first_seen_buildid": (
        "evidence",
        ("report_bug.py", "agent/triage.py", "agent/orchestrator.py",
         "templates/crashstack.html"),
        "The 364-day windowed clock, NOT `SignatureFirstDate`'s all-time one; the two are "
        "documented as disagreeing, so both surfaces name the build."),
    "candidate_landed_after_first_seen_days": (
        "evidence", ("report_bug.py", "templates/crashstack.html"), ""),
    "stale_signature_clamped": (
        "clamp", ("report_bug.py", "templates/crashstack.html", "policy:_SO_BOOST_POLICY"),
        "`allow` in the boost policy, on the axis argument: the clamp rules on ORIGIN, an "
        "independent blind agreement is about the MECHANISM. Both surfaces now say so — that "
        "round trip was invisible until 2026-08-21."),
    "signature_report_count": ("evidence", ("agent/orchestrator.py",), ""),

    # The next six come out of `sigage.age_facts`, whose keys were COMPUTED
    # (`"signature_first_seen_" + key`) until 2026-08-24 and so were invisible to the registry
    # scanner: five of them were live in prod dossiers, undeclared, for weeks. `agent/triage.py`
    # prints the same numbers but re-derives them by calling `age_facts` itself, so it reads the
    # arithmetic and not these flags.
    "signature_first_seen_ever": (
        "evidence", ("report_bug.py",),
        "Socorro's all-time `SignatureFirstDate`, and NOT the stale-signature gate's clock: "
        "`sigage.first_seen_ever` measures that substituting it there would clamp eight of the "
        "sixteen filings a human acted on. THE TRAP: `build_seed` puts the same NAME in the seed, "
        "and `agent/orchestrator.py` reads that one, not this flag."),
    "signature_age_days_ever": ("evidence", ("report_bug.py",), ""),
    "signature_first_seen_windowed": (
        "evidence", ("report_bug.py",),
        "The 364-day answer, carried beside the all-time one so the error between the two clocks "
        "stays measurable in prod instead of arguable."),
    "signature_age_days_windowed": ("evidence", ("report_bug.py",), ""),
    "signature_clock_drift_days": (
        "evidence", ("report_bug.py",),
        "`ever` minus `observed`. Negative past `sigage.RENAME_DRIFT_DAYS` means the signature was "
        "RENAMED, which is the only thing this number is used for."),
    "signature_rename_suspected": (
        "evidence", ("report_bug.py",),
        "The drift verdict, not a second measurement. It had not fired once in the 500 prod "
        "dossiers read on 2026-08-24 -- absence here is untested, not quiet."),

    # -- hardware: the bit-flip / broken-machine family ----------------------------------------
    "possible_bit_flip_confidence": ("evidence", ("report_bug.py",), ""),
    "possible_bit_flip_suppressed": (
        "suppression", ("policy:_INSTANCE_SUPPRESSED",),
        "Instance-scoped: one machine's flipped bit says nothing about the next report."),
    "broken_cpu_suppressed": ("suppression", ("policy:_INSTANCE_SUPPRESSED",), ""),
    "bad_machine_suppressed": ("suppression", ("policy:_INSTANCE_SUPPRESSED",), ""),
    "hardware_noise_signature_suppressed": (
        "suppression", (),
        "Deliberately NOT instance-scoped and deliberately not in `_INSTANCE_SUPPRESSED`: it "
        "says the SIGNATURE is mostly hardware error, which is equally true of every report in "
        "the cluster, and re-deriving it per crash costs ~$3 for an answer that cannot differ."),
    "report_on_broken_cpu": ("evidence", ("report_bug.py", "agent/orchestrator.py"), ""),
    "signature_bit_flip_rate": ("evidence", ("report_bug.py",), ""),
    "signature_broken_cpu_rate": ("evidence", ("report_bug.py",), ""),
    "signature_hardware_sample": ("evidence", ("report_bug.py",), ""),
    "signature_cpu_reports": ("evidence", ("report_bug.py",), ""),
    "signature_cpu_terms": ("evidence", ("report_bug.py",), ""),
    "signature_top_cpu_term": ("evidence", ("report_bug.py",), ""),
    "signature_top_cpu_share": (
        "evidence", ("report_bug.py",),
        "REPORTED, never gated: every concentration threshold from 0.40 to 0.95 eats at least "
        "one of the 19 controls, and at the gate's own sample floor it discriminates below "
        "chance (AUC 0.333)."),
    "cpu_info": (
        "diagnostic", (),
        "The crashing machine's own CPU string, recorded by the bad-machine gate. Read by "
        "nothing in the pipeline and kept anyway: until 2026-08-19 this field had never reached "
        "any prompt at all, which is how a reviewer came to be the first to notice that 55 of a "
        "signature's 58 reports sat on one ordinary CPU."),
    "machine_crash_count": ("diagnostic", (), "The bad-machine gate's inputs, kept so the "
                            "thresholds can be re-fit from persisted dossiers."),
    "machine_distinct_cpus": ("diagnostic", (), ""),
    "machine_distinct_signatures": (
        "diagnostic", (),
        "The fourth of the same family, and the conjunct the gate actually branches on -- off the "
        "seed's `install_history`, though, never off this flag. The first scanner missed it "
        "because it is a key in `flags = {...}` rather than a later `flags[...] =` line."),
    "machine_span_seconds": (
        "diagnostic", (),
        "The one bad-machine threshold with no panel behind it (worklist rank 16): 1800s was "
        "read off bug 2047016, whose machine the diversity conjunct already spares."),

    # -- the candidate's own history -----------------------------------------------------------
    "candidate_backedout": ("evidence", ("templates/crashstack.html",), ""),
    "candidate_backedout_by": ("evidence", ("templates/crashstack.html",), ""),
    "candidate_backedout_suppressed": (
        "suppression", ("templates/crashstack.html",),
        "NOT in `_INSTANCE_SUPPRESSED`, so it closes the cluster forever — and a RELANDED patch "
        "is still suppressed, because `backedoutby` stays set and hg exposes no 'relanded as' "
        "pointer (worklist rank 18)."),
    "candidate_is_backout": ("evidence", ("templates/crashstack.html",), ""),
    "candidate_backout_same_push": ("evidence", ("templates/crashstack.html",), ""),
    "candidate_backout_suppressed": ("suppression", ("templates/crashstack.html",), ""),
    "candidate_backout_capped": ("clamp", ("templates/crashstack.html",), ""),
    "candidate_arrived_by_merge": (
        "evidence", ("report_bug.py",),
        "The candidate reached the channel with a MERGE push (a whole cycle at one pushdate), "
        "so `candidate_in_pushlog_window` is true and means nothing: 5,192 changesets for the "
        "beta build after the 2026-08-13 merge against 45-122 for an ordinary one. Read by "
        "`is_suspected_regression`, which returns False on it -- no `regression` keyword, no "
        "`regressed_by`, no \"Suspected regressor\". Off-stack only: a merge push gets no "
        "`changesets` rows (`pushlog.collect`), so nothing can score onto a frame."),
    "candidate_in_pushlog_window": (
        "evidence", ("report_bug.py", "eval/calibrate.py"),
        "Gates the filed bug's regression PROSE, its `regression` keyword and its `regressed_by` "
        "field. It does NOT select a calibration table: keying the published probability on it "
        "was tried and measured dead (Fisher p=0.257 on the rows whose label can vary)."),

    # -- compiled-out --------------------------------------------------------------------------
    "compiled_out_suppressed": (
        "suppression", ("agent/schema.py", "templates/crashstack.html"),
        "Also read by `_skeptic_veto`: it is what lets a build-flag `fail` bind."),
    "compiled_out_symbol": ("evidence", ("templates/crashstack.html",), ""),
    "compiled_out_macro": ("diagnostic", (), ""),
    "compiled_out_provenance": (
        "evidence", ("templates/crashstack.html",),
        "`mechanism` when the verdict's own statement names the symbol, `diff` when only the "
        "candidate's diff does. The published sentence differs, because only the first licenses "
        "'the mechanism rests on X'."),
    "compiled_out_rev": (
        "diagnostic", (),
        "Which revision the moz.configure switch was read at. Recorded because the body clock "
        "(searchfox, tip) and the switch clock (hg, build rev) are different clocks."),

    # -- sensitivity ----------------------------------------------------------------------------
    "memory_unsafe": (
        "evidence", ("sensitive.py",),
        "The crash report itself proves the process touched freed/poisoned memory, so the "
        "ANALYSIS is withheld from every unauthenticated surface. Read by `sensitive.is_withheld`, "
        "which `bugzilla_apply.build_evidence` and `html._draft_evidence` gate on. Reads as "
        "`evidence` and not `suppression` on purpose: it moves no rung and blocks no filing, it "
        "only decides who may read the mechanism."),
    "memory_unsafe_signals": (
        "evidence", ("bugzilla_apply.py",),
        "Which deterministic test fired, so a withheld page can say why without re-deriving it."),

    # -- threads ---------------------------------------------------------------------------------
    "absent_named_threads": (
        "diagnostic", (),
        "The quoted thread names a verdict asserted that this process does not have."),
    "absent_thread_clamped": ("diagnostic", (), ""),
    "archetypes": (
        "evidence", ("feedback.py",),
        "The slugs that fired, copied onto `Feedback` so `scoreboard()[\"by_archetype\"]` can "
        "ask whether a row helped. Note the denominator is FILED bugs, so a row that fires on "
        "19 of 840 analysed crashes and files none scores zero by construction."),
    "skeptic_build_flag_unbound": (
        "diagnostic", (),
        "A skeptic `fail` that rested on a compile-flag claim and was NOT allowed to bind. Its "
        "failure mode is a false abstain, which reaches no scoreboard, so this is the count."),
}


# A `promotion` or a `clamp` MOVED the rung the filed bug publishes, so the bug has to say why --
# otherwise a reader sees a number with no reason, which is the exact complaint that produced this
# file (see the `stale_signature_clamped` story at the top). `test_a_rung_mover_reaches_the_bug`
# requires `report_bug.py` among the readers of every such flag, and the exceptions below are
# DECLARED rather than tolerated.
#
# All four fired on 0 of the 17 filed bugs in the 500-dossier prod snapshot of 2026-08-24
# (all-500 counts in the notes), so this invariant carries no debt today. That is the point: it is
# a guard against the NEXT rung-mover being invisible, not a repair of an existing gap.
UNPUBLISHED = {
    "fault_address_offset_match":
        "7 of 500 runs, 0 of 17 filings. The offset match itself is published as prose by "
        "`_explanation_comment` when the verdict rests on it; the FLAG is the promotion's "
        "audit trail, not the reader's sentence.",
    "prior_signature_match":
        "2 of 500 runs, 0 of 17 filings.",
    "downgraded_from_strong":
        "0 of 500 runs. Read only through `_SO_BOOST_POLICY`; the downgrade it records is "
        "already spoken by whichever gate called `_downgrade_to_lead_or_abstain`.",
    "candidate_backout_capped":
        "0 of 500 runs. Caps a backout candidate's rung; the backout itself is printed.",
}


def declared(flag):
    """The ``(kind, readers, note)`` declaration for *flag*, or ``None``."""
    return REGISTRY.get(flag)


def write_only():
    """The flags nothing reads — every one a deliberate ``diagnostic``, or the test fails."""
    return {f for f, (_kind, readers, _n) in REGISTRY.items() if not readers}


def suppressions():
    """The flags that turn a verdict into an abstain."""
    return {f for f, (kind, _r, _n) in REGISTRY.items() if kind == "suppression"}
