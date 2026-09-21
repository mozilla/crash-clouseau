# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""The principal's prompt has a size, and it is now a reviewed number.

    DATABASE_URL=sqlite:// uv run python -m unittest tests.test_prompt_budget

WHY THIS FILE EXISTS. The v109 batch (deployed 2026-08-21 21:19) grew the principal's first user
message by a median +1,914 bytes (+20.3%, positive on 198 of 198 post-deploy runs) and `system.md`
by +638 bytes, and prod candidate-naming stepped 41.7% -> 24.2% (Fisher p = 5.5e-5) at that exact
release. The growth was found three days later, by rebuilding 500 prompts offline from a snapshot.

Nothing in the repo could have seen it. There are dense LOCAL caps -- `_short_value(limit=300)`,
`_SPIN_STACK_LIMIT`, `_MAX_THREAD_NAMES`, and the four tool-result caps -- each measured and pinned.
There was no number anywhere for the WHOLE. A `grep -rn "len(.*_crash_facts\\|len(.*_user_prompt\\|
len(.*_system_prompt" tests/` returned nothing against 56 references to those three names.

THIS IS NOT A THRESHOLD, IT IS A LEDGER ENTRY. Every constant below is the measured size today. If
your change moves one, that is fine and expected -- update the number, and say in the commit what
bought the bytes. The failure this prevents is not "the prompt got big", it is "the prompt got
bigger and nobody wrote it down". Had this file existed, the v109 batch would have had to state
`+2552` in a diff a reviewer read.

WHAT A BYTE COUNT CANNOT TELL YOU, stated here so the next reader does not over-trust it: 90.6% of
v109's growth was text telling the model NOT to accuse ("prefer a `lead` + soft `needinfo` over
accusing it as the culprit", "that absence is the BASE RATE, not evidence", "concentration is not
support for a bug either"). A ledger would have waved through `0563219` -- correctly, it costs +4
bytes/run -- and said nothing about the direction of the other 2,311. Size is the cheap half; the
question a reviewer still has to ask by hand is whether the new text pushes toward or away from
naming a candidate.
"""
import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite://")

from crashclouseau.agent import triage                                   # noqa: E402
from tests.test_hang_bucket import _IDLE_POOL, _ORIGIN, _SUGGEST, _hang  # noqa: E402

# A plain single-thread deref: the floor of what any run pays.
_PLAIN = {
    "uuid": "u-plain", "signature": "mozilla::dom::Foo::Bar", "channel": "nightly",
    "product": "Firefox", "buildid": "20260819092600", "version": "156.0a1",
    "raw_crash": {
        "reason": "EXCEPTION_ACCESS_VIOLATION_READ",
        "json_dump": {"crash_info": {"type": "EXCEPTION_ACCESS_VIOLATION_READ",
                                     "address": "0x0", "crashing_thread": 0}},
    },
}

_THREAD_NAMES = [
    "Gecko_IOThread", "IPDL Background", "StreamTrans #%d", "DOM Worker", "Compositor",
    "Renderer", "StyleThread#%d", "mozStorage #%d", "Cache2 I/O", "Timer",
    "SoftwareVsyncThread", "URL Classifier", "TaskController #%d",
]


def _parent_hang(n_threads=40):
    """A 40-thread parent hang -- the shape `_thread_inventory` costs the most on, and the one
    v109's second-largest grower (`fb1f22e`, +700 bytes/run) was written for."""
    threads = []
    for i in range(n_threads):
        name = _THREAD_NAMES[i % len(_THREAD_NAMES)]
        threads.append({"thread_name": (name % i) if "%d" in name else name,
                        "frames": [{"function": "mozilla::Foo%d::Bar" % i,
                                    "file": "dom/base/F%d.cpp" % i}]})
    return {
        "uuid": "u-hang", "signature": "shutdownhang | RtlWaitOnAddress", "channel": "nightly",
        "product": "Firefox", "buildid": "20260819092600", "version": "156.0a1",
        "raw_crash": {
            "reason": "EXCEPTION_BREAKPOINT", "process_type": "parent",
            "json_dump": {"crash_info": {"type": "EXCEPTION_BREAKPOINT", "address": "0x0",
                                         "crashing_thread": 0},
                          "threads": threads},
        },
    }


def _beta(crash, ages=False):
    """The same crash on BETA. A separate fixture set, because every ledger entry above is
    nightly and the beta prompt is a DIFFERENT prompt: `_system_prompt` re-renders the
    revision-drift section for the beta branch, `_signature_age_lines` can state two ages, and
    the hardware paragraphs quote beta's own population rates. Without a beta fixture a
    beta-only sentence adds ZERO bytes to the four numbers above and the ledger stays green --
    which is exactly the v109 failure this file was written to prevent.

    ``ages=True`` adds the signature-age keys in the shape that triggers the beta-only
    "new on beta, old everywhere" block (`triage._channel_age_lines`)."""
    out = dict(crash, channel="beta", version="156.0b3")
    if ages:
        out.update({
            # All-time debut a month ago (the nightly debut of a change that rode the merge),
            # first beta report on this cycle's first beta build.
            "signature_first_seen_ever": "20260721000000",
            "signature_first_seen_buildid": "20260721000000",
            "signature_first_seen_any": "20260721000000",
            "signature_first_seen_channel": "20260817142839",
        })
    return out


# A FENIX JAVA crash (the 2026-09-15 live example, 3c426d92): `java` + `line_numbers_trusted`
# False on the seed, no `json_dump`, a `java_exception` chain and the Android device fields. A
# separate fixture set for the same reason the beta one exists: the Java prompt is a DIFFERENT
# prompt (`_system_prompt(channel, java=True)` inserts the inverted drift rule, `_java_lines` and
# the Java facts print, the candidate wording changes), and none of it adds a byte to a desktop
# row.
_JAVA_KT = ("mobile/android/android-components/components/lib/dataprotect/src/main/java/mozilla/"
            "components/lib/dataprotect/Keystore.kt")
_JAVA = {
    "uuid": "u-java", "channel": "nightly", "product": "Fenix", "buildid": "20260910214118",
    "version": "158.0a1", "java": True, "line_numbers_trusted": False,
    "signature": "java.security.ProviderException: at android.security.keystore2."
                 "AndroidKeyStoreKeyGeneratorSpi.engineGenerateKey(AndroidKeyStoreKeyGeneratorSpi"
                 ".java)",
    "raw_crash": {
        "product": "Fenix", "os_name": "Android", "os_version": "33",
        "os_pretty_version": "Android 33", "cpu_arch": "amd64", "cpu_info": "unknown",
        "android_manufacturer": "Google", "android_model": "octopus",
        "android_version": "33 (REL)", "android_cpu_abi": "x86_64", "process_type": "parent",
        "report_type": "crash", "install_time": 1789110875,
        "java_exception": {"exception": {"values": [
            {"stacktrace": {"type": "KeyStoreException", "module": "android.security",
                            "frames": []}},
            {"stacktrace": {"type": "ProviderException", "module": "java.security",
                            "frames": []}},
        ]}},
    },
    # `orchestrator._stack_text(frames, line_numbers_trusted=False)` for the two Keystore frames.
    "stack": ("#0 mozilla.components.lib.dataprotect.Keystore.generateKey  {kt}  (reported line "
              "269: R8-remapped, unreliable)\n#1 mozilla.components.lib.dataprotect.Keystore."
              "<init>  {kt}  (reported line 51: R8-remapped, unreliable)".format(kt=_JAVA_KT)),
    "candidates": [{"node": "abc", "score": 8, "bug": 1, "backedout": False, "pushdate": None,
                    "noise": False}],
}


# A shutdown hang whose awaited thread is IN the dump: crash 37d5021a (bug 2073349) in shape, an
# idle `BgIOThreadPool` worker and the busy one in Suggest/viaduct. The `AWAITED WORK` block is
# the whole difference from the 40-thread hang row above: the thread's 14 frames and the rule
# sentence, shared with the blind second opinion.
_AWAITED = {
    "uuid": "u-hang2", "channel": "release", "product": "Firefox", "buildid": "20260903215306",
    "version": "155.0.1",
    "signature": "shutdownhang | mozilla::SpinEventLoopUntil<T> | nsThreadPool::ShutdownWithTimeout",
    "raw_crash": _hang([_IDLE_POOL, _SUGGEST]),
    # The blame of the awaited work (`hang_awaited_origin`): one more line, the candidate an
    # actionable verdict is routed by.
    "hang_awaited_origin": _ORIGIN,
}


# name -> (measured bytes, tolerance). Nightly rows measured 2026-08-24 at HEAD; the beta rows
# and the two age rows 2026-08-25. The tolerance is deliberately tight: v109's whole system.md
# change was +638 bytes and it has to be impossible to make that quietly.
#
# 2026-08-27, system.md +958 (16157 -> 17115, and beta with it): the `abstain_kind` vocabulary,
# which is the price of learning what our 55-abstains-a-day actually are. It replaces a line
# saying `abstain` is "ONLY for genuine noise" -- measurably false, 71% of abstains are
# model-authored conclusions -- with eight words and a clause each. The first draft cost 1,715
# and this ledger is what sent it back to be halved.
#
# 2026-09-03, the two HANG rows +607 (1901 -> 2508, 2675 -> 3285), faults untouched: the
# `_watchdog_lines` block, three sentences saying what a timeout crash is. It buys back the two
# arguments that refuted a confirmed regressor on 2026-08-15 -- "touches no code on the stack"
# and "the signature predates the change" -- both of which are right for a fault and inverted
# for a watchdog. The first draft was 700; this ledger sent it back.
# 2026-09-09, system.md +1947 (17117 -> 19064, and beta with it): three rules bought by bugs
# 2070489 and 2070554, both filed the same morning naming the same innocent pref flip. "A
# MECHANISM IS NOT EVIDENCE UNTIL ONE LINK IS OBSERVED" (every link of both stories was a
# "could"), "A RATE CLAIM IS A MEASUREMENT CLAIM" (the 4.5x was a population shift plus a
# train-hop deployment) and "A CANDIDATE THAT RESTORES AN EARLIER STATE HAS A CONTROL GROUP"
# (a Nimbus rollout had already turned the pref off for every 155.0 user, with zero crashes).
_MEASURED = {
    # +2 on 2026-09-07: "treat `Bash` as a last resort" became "and no shell or file tools here"
    # when the built-in toolset was switched off (`triage.build_options`, `tools=`).
    # +1520 on 2026-09-17: the `actionable` decision -- rule 3 under "Rules for the verdict",
    # the shape line, the `pre_existing` pointer and the rate-claim sentence.
    # +156 the same day: "do not argue that nothing in the window explains it" -- the first two
    # live runs each closed their second statement with exactly that.
    # +130 on 2026-09-18: `title` in the verdict shape -- a bug on a signature a [meta] holds is
    # named for its cause, not the signature (bug 2073349 c1).
    # +817 on 2026-09-19 (beta and java with it): "A DIAGNOSTIC IS AN EXPOSER, NOT A CAUSE" --
    # a change that adds an assert/CHECK/annotation or moves an abort mints a new signature for
    # an old condition and is named as the exposer, never as "regressed by". Bought by bug
    # 2071287, where the assert-adding fix was named as the regressor and the module owner
    # corrected it to the change three releases earlier that made the condition true.
    # +126 on 2026-09-21 (beta and java with it): name the violated invariant and path, not
    # crash/assert/panic mechanics (bug 2074119 c0).
    # +1437 on 2026-09-21 (beta and java with it): actionable bugs publish only the complete
    # mechanism, and the OOM rule defines the annotations and deterministic gate.
    "system.md": (23250, 400),
    "crash facts, plain deref": (219, 60),
    "user prompt, plain deref": (970, 120),
    "crash facts, 40-thread parent hang": (2508, 200),
    "user prompt, 40-thread parent hang": (3285, 300),
    # 2026-09-18, the AWAITED WORK block (bug 2073349): ~2,550 bytes over the same hang without
    # it -- the awaited thread's 14 frames (long Rust symbols and source paths) and the rule
    # sentence. Paid only on a hang whose spin-loop stack names a thread the dump has.
    # +576 on 2026-09-18 (second pass): the origin line -- who last changed the awaited work,
    # the `candidate` an actionable verdict is routed by -- and the rule that the mechanism
    # starts with the work and does not restate how the wait works (Jens, 2073349 c1).
    "crash facts, hang with awaited work": (5449, 300),
    "user prompt, hang with awaited work": (6277, 400),
    # BETA. system.md is +540 over nightly's, all of it the revision-drift rewrite: the beta
    # branch and trunk have diverged, so "a small line delta is expected drift" needed the
    # sentence saying which tree the tools read and that trunk code is not what shipped.
    "system.md, beta": (23790, 400),
    # +0 crash-facts bytes and -3 user-prompt bytes for the channel alone ("beta" is shorter
    # than "nightly"): the channel is a switch, not a paragraph. This row exists to keep it that
    # way -- if it grows, a beta-only sentence has been added to the per-crash surface.
    "crash facts, plain deref (beta)": (219, 60),
    "user prompt, plain deref (beta)": (985, 120),
    # The two-age block, which only a non-nightly channel can produce. Compare the nightly
    # single-age fixture below: the second age plus its guidance is what the difference buys.
    "crash facts, beta with two signature ages": (1342, 200),
    "user prompt, beta with two signature ages": (2108, 300),
    # +229 on 2026-09-17: `_OLD_SIGNATURE_GUIDANCE` points at `actionable` when the mechanism
    # is established and no changeset explains the crash.
    "crash facts, nightly with one signature age": (1269, 200),
    # FENIX / JAVA, measured 2026-09-15. system.md is +1163 over nightly's: the `## Java/Kotlin
    # stacks` section, which INVERTS the revision-drift rule for an R8 stack (a line mismatch is
    # expected there, not drift to be forgiven -- and the line is not evidence either way). The
    # user prompt is the R8 block (`_java_lines`, three sentences shared with the second
    # opinion), the Java facts (exception chain + device, +106 bytes over the plain deref) and the
    # long Java signature / paths; it carries no line numbers on its frames.
    "system.md, java": (24413, 400),
    "crash facts, fenix java": (325, 60),
    "user prompt, fenix java": (3240, 300),
}

_HOWTO = (
    "\n\nThis is a LEDGER, not a limit. If your change legitimately moves this number, update it "
    "here and say in the commit message what bought the bytes. See this file's docstring: v109 "
    "added ~2,552 bytes to this surface with no reviewer ever seeing a number, and prod "
    "candidate-naming stepped 41.7% -> 24.2% at that release."
)


class TestPromptBudget(unittest.TestCase):
    def _check(self, name, actual):
        want, tol = _MEASURED[name]
        self.assertAlmostEqual(
            actual, want, delta=tol,
            msg="{} is {} bytes, last measured at {} (+/-{}).{}".format(
                name, actual, want, tol, _HOWTO))

    def test_the_awaited_work_block_is_pinned(self):
        """The one per-crash block added since the ledger was written; a hang without an
        awaited thread in its dump must not pay for it."""
        self._check("crash facts, hang with awaited work",
                    len("\n".join(triage._crash_facts(_AWAITED))))
        self._check("user prompt, hang with awaited work", len(triage._user_prompt(_AWAITED)))
        self.assertIn("AWAITED WORK", triage._user_prompt(_AWAITED))
        self.assertNotIn("AWAITED WORK", triage._user_prompt(_parent_hang()))

    def test_the_standing_system_prompt_is_pinned(self):
        """Read whole on every run, `lru_cache`d, identical for every crash ON ONE CHANNEL -- so
        a byte here is the most expensive byte in the pipeline."""
        self._check("system.md", len(triage._system_prompt()))
        self._check("system.md, beta", len(triage._system_prompt("beta")))

    def test_the_java_prompt_is_a_different_prompt_and_the_desktop_one_is_not(self):
        """The Java rows exist for the same reason the beta rows do. And the inverse: a Fenix
        seed that is NOT Java (a native Fenix crash) must get the desktop system prompt."""
        self._check("system.md, java", len(triage._system_prompt(None, True)))
        self._check("crash facts, fenix java", len("\n".join(triage._crash_facts(_JAVA))))
        self._check("user prompt, fenix java", len(triage._user_prompt(_JAVA)))
        self.assertIn("## Java/Kotlin stacks", triage._system_prompt(None, True))
        self.assertNotIn("## Java/Kotlin stacks", triage._system_prompt())
        self.assertNotIn("## Java/Kotlin stacks", triage._system_prompt("nightly", False))

    def test_the_beta_prompt_is_a_different_prompt(self):
        """Not a size assertion -- the reason the beta rows exist. If these three ever stop
        holding, the fixtures have stopped covering the channel."""
        beta = triage._system_prompt("beta")
        self.assertIn("mozilla-beta", beta)
        self.assertNotIn('"repo": "mozilla-central"', beta)
        self.assertNotEqual(beta, triage._system_prompt())

    def test_the_per_crash_facts_are_pinned(self):
        """`_crash_facts` is shared BYTE-FOR-BYTE with the blind second opinion
        (`second_opinion._user_prompt`), so every byte added here is paid twice per crash."""
        self._check("crash facts, plain deref",
                    len("\n".join(triage._crash_facts(_PLAIN))))
        self._check("crash facts, 40-thread parent hang",
                    len("\n".join(triage._crash_facts(_parent_hang()))))
        self._check("crash facts, plain deref (beta)",
                    len("\n".join(triage._crash_facts(_beta(_PLAIN)))))

    def test_the_signature_age_block_is_pinned_on_both_channels(self):
        """The two-age block is beta-only prose (`triage._channel_age_lines`), so it is invisible
        to every nightly fixture. Both rows, so the DELTA is what a reviewer reads."""
        self._check("crash facts, nightly with one signature age",
                    len("\n".join(triage._crash_facts(dict(
                        _PLAIN,
                        signature_first_seen_ever="20260721000000",
                        signature_first_seen_buildid="20260721000000",
                        signature_first_seen_any="20260721000000")))))
        self._check("crash facts, beta with two signature ages",
                    len("\n".join(triage._crash_facts(_beta(_PLAIN, ages=True)))))
        self._check("user prompt, beta with two signature ages",
                    len(triage._user_prompt(_beta(_PLAIN, ages=True))))

    def test_the_user_prompt_is_pinned(self):
        self._check("user prompt, plain deref", len(triage._user_prompt(_PLAIN)))
        self._check("user prompt, 40-thread parent hang",
                    len(triage._user_prompt(_parent_hang())))
        self._check("user prompt, plain deref (beta)",
                    len(triage._user_prompt(_beta(_PLAIN))))

    def test_the_thread_block_is_the_dominant_per_crash_term(self):
        """Not a size assertion -- a shape one, and the reason the hang fixture exists. If this
        ever stops holding, the fixture set above has stopped covering the expensive case."""
        plain = len("\n".join(triage._crash_facts(_PLAIN)))
        hang = len("\n".join(triage._crash_facts(_parent_hang())))
        self.assertGreater(hang, 4 * plain)


class TestPromptBudgetIsLogged(unittest.TestCase):
    def test_every_run_logs_its_prompt_size(self):
        """A pinned test only fires when someone runs the suite, and `.taskcluster.yml` runs CI on
        PRs and pushes to `master` only -- so work on a feature branch is never checked by it. The
        log line is the half that reports from production."""
        # `crashclouseau.logger` configures the ROOT logger (`logging.getLogger()`), so
        # `assertLogs` must watch the root, not a named child.
        with self.assertLogs(level="INFO") as cm:
            triage._log_prompt_budget("x" * 100, "y" * 40, _PLAIN)
        line = "\n".join(cm.output)
        self.assertIn("prompt bytes", line)
        self.assertIn("system=100", line)
        self.assertIn("user=40", line)
        self.assertIn("total=140", line)
        self.assertIn("u-plain", line)


if __name__ == "__main__":
    unittest.main()
