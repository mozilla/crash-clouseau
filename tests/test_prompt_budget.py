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


# name -> (measured bytes on 2026-08-24 at HEAD, tolerance). The tolerance is deliberately tight:
# v109's whole system.md change was +638 bytes and it has to be impossible to make that quietly.
_MEASURED = {
    "system.md": (16157, 400),
    "crash facts, plain deref": (219, 60),
    "user prompt, plain deref": (970, 120),
    "crash facts, 40-thread parent hang": (1901, 200),
    "user prompt, 40-thread parent hang": (2675, 300),
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

    def test_the_standing_system_prompt_is_pinned(self):
        """Read whole on every run, `lru_cache`d, identical for every crash -- so a byte here is
        the most expensive byte in the pipeline."""
        self._check("system.md", len(triage._system_prompt()))

    def test_the_per_crash_facts_are_pinned(self):
        """`_crash_facts` is shared BYTE-FOR-BYTE with the blind second opinion
        (`second_opinion._user_prompt`), so every byte added here is paid twice per crash."""
        self._check("crash facts, plain deref",
                    len("\n".join(triage._crash_facts(_PLAIN))))
        self._check("crash facts, 40-thread parent hang",
                    len("\n".join(triage._crash_facts(_parent_hang()))))

    def test_the_user_prompt_is_pinned(self):
        self._check("user prompt, plain deref", len(triage._user_prompt(_PLAIN)))
        self._check("user prompt, 40-thread parent hang",
                    len(triage._user_prompt(_parent_hang())))

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
