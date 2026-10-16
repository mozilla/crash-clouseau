# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Tests for module enrichment and spike-brief rendering."""
import os
import unittest
from unittest import mock

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from crashclouseau import coloaded  # noqa: E402
from crashclouseau.agent import spike_agent, spike_escalation  # noqa: E402


_COMMON = ["m{}.dll".format(i) for i in range(200)]


def _crash(*modules, stack=()):
    return {"modules": {m: "Vendor" for m in modules}, "stack": set(stack)}


class TestEnrichment(unittest.TestCase):
    def test_a_module_in_every_report_and_no_baseline_is_reported(self):
        sample = [_crash("xul.dll", "print.dll") for _ in range(20)]
        baseline = [_crash("xul.dll") for _ in range(40)]
        out = coloaded.enrichment(sample, baseline)
        self.assertEqual([r["modules"] for r in out["rows"]], [["print.dll"]])
        self.assertEqual((out["rows"][0]["in_sample"], out["rows"][0]["in_baseline"]), (20, 0))
        self.assertEqual(out["tested"], 2)

    def test_a_module_as_common_as_the_baseline_is_not(self):
        sample = [_crash("xul.dll", *(("av.dll",) if i % 2 else ())) for i in range(20)]
        baseline = [_crash("xul.dll", *(("av.dll",) if i % 2 else ())) for i in range(40)]
        self.assertEqual(coloaded.enrichment(sample, baseline)["rows"], [])

    def test_a_depleted_module_is_not_reported(self):
        sample = [_crash("xul.dll") for _ in range(20)]
        baseline = [_crash("xul.dll", "gpu.dll") for _ in range(40)]
        self.assertEqual(coloaded.enrichment(sample, baseline)["rows"], [])

    def test_too_few_installations_cannot_pass_the_correction(self):
        sample = [_crash(*_COMMON, "rare.dll") for _ in range(2)]
        baseline = [_crash(*_COMMON) for _ in range(80)]
        self.assertEqual(coloaded.enrichment(sample, baseline)["rows"], [])

    def test_modules_with_the_same_presence_are_one_row(self):
        sample = [_crash("xul.dll", *(("a.dll", "b.dll") if i < 15 else ())) for i in range(20)]
        baseline = [_crash("xul.dll") for _ in range(40)]
        rows = coloaded.enrichment(sample, baseline)["rows"]
        self.assertEqual([r["modules"] for r in rows], [["a.dll", "b.dll"]])

    def test_a_module_off_the_stack_is_not_grouped_with_one_on_it(self):
        sample = [_crash("xul.dll", "a.dll", "b.dll", stack=("a.dll",)) for _ in range(10)]
        baseline = [_crash("xul.dll") for _ in range(40)]
        rows = coloaded.enrichment(sample, baseline)["rows"]
        self.assertEqual(sorted((r["modules"], r["on_stack"]) for r in rows),
                         [(["a.dll"], 10), (["b.dll"], 0)])
        result = dict(coloaded.enrichment(sample, baseline), scope={"product": "Firefox"})
        text = "\n".join(coloaded.lines(result))
        self.assertIn("  - a.dll [Vendor]: 10/10 vs 0/40; on the crashing stack in 10", text)
        self.assertIn("  - b.dll [Vendor]: 10/10 vs 0/40\n", text + "\n")

    def test_the_crashing_stack_is_counted(self):
        sample = [_crash("xul.dll", "drv.dll", stack=("drv.dll",)) for _ in range(10)]
        baseline = [_crash("xul.dll") for _ in range(40)]
        self.assertEqual(coloaded.enrichment(sample, baseline)["rows"][0]["on_stack"], 10)


class TestSummarize(unittest.TestCase):
    def test_modules_and_crashing_stack(self):
        out = coloaded.summarize({
            "crashing_thread": 1,
            "json_dump": {
                "modules": [{"filename": "XUL.dll", "cert_subject": "Mozilla Corporation"},
                            {"filename": "Drv.dll", "cert_subject": None}, {"version": "1"}],
                "threads": [{"frames": [{"module": "other.dll"}]},
                            {"frames": [{"module": "Drv.dll"}, {"function": "f"}]}]}})
        self.assertEqual(out["modules"], {"xul.dll": "Mozilla Corporation", "drv.dll": None})
        self.assertEqual(out["stack"], {"drv.dll"})
        self.assertEqual(coloaded.summarize({}), {"modules": {}, "stack": set()})


class TestPick(unittest.TestCase):
    def test_one_report_per_installation_and_per_signature_cap(self):
        hits = [{"uuid": "u1", "signature": "a", "install_time": 1},
                {"uuid": "u2", "signature": "a", "install_time": 1},
                {"uuid": "u3", "signature": "a", "install_time": 2},
                {"uuid": "u4", "signature": "a", "install_time": 3},
                {"uuid": "u5", "signature": "b", "install_time": 4},
                {"uuid": "u6", "signature": "b", "install_time": None}]
        with mock.patch.object(coloaded, "_search_hits", return_value=hits):
            self.assertEqual([u for u, _ in coloaded._pick({}, 10)], ["u1", "u3", "u4", "u5"])
            self.assertEqual([u for u, _ in coloaded._pick({}, 10, per_signature=2)],
                             ["u1", "u3", "u5"])
            self.assertEqual(coloaded._pick({}, 2), [("u1", 1), ("u3", 2)])
            self.assertEqual([u for u, _ in coloaded._pick(
                {}, 10, skip_signatures={"b"}, skip_installs={2})], ["u1", "u4"])


class TestForSignature(unittest.TestCase):
    def test_a_sample_too_small_to_be_significant_skips_the_baseline(self):
        with mock.patch.object(coloaded, "_pick",
                               return_value=[("u1", 1), ("u2", 2)]) as pick, \
                mock.patch.object(coloaded, "_summaries",
                                  return_value=[_crash(*_COMMON), _crash(*_COMMON)]):
            self.assertIsNone(coloaded.for_signature(
                ["sig"], "Firefox", "release", "158.0", "parent", "Windows NT"))
        self.assertEqual(pick.call_count, 1)

    def test_the_baseline_matches_the_scope_and_excludes_the_signature(self):
        sample = [_crash("xul.dll", "print.dll") for _ in range(20)]
        baseline = [_crash("xul.dll") for _ in range(40)]
        with mock.patch.object(coloaded, "_pick", return_value=[("u", 7)]) as pick, \
                mock.patch.object(coloaded, "_summaries", side_effect=[sample, baseline]):
            out = coloaded.for_signature(["sig", "sig2"], "Firefox", "beta", "158.0b5",
                                         "parent", "Windows NT")
        first, second = (c.args[0] for c in pick.call_args_list)
        self.assertEqual(first["signature"], ["=sig", "=sig2"])
        self.assertEqual(second["signature"], "!=sig")
        self.assertEqual(pick.call_args_list[1].kwargs["skip_signatures"], {"sig", "sig2"})
        self.assertEqual(pick.call_args_list[1].kwargs["skip_installs"], {7})
        for params in (first, second):
            self.assertEqual(params["release_channel"], ["beta", "aurora"])
            self.assertEqual((params["version"], params["process_type"], params["platform"]),
                             ("158.0b5", "parent", "Windows NT"))
        self.assertEqual(pick.call_args_list[1].kwargs["per_signature"],
                         coloaded.BASELINE_PER_SIGNATURE)
        self.assertEqual(out["rows"][0]["modules"], ["print.dll"])

    def test_no_version_is_not_measured(self):
        self.assertIsNone(coloaded.for_signature(["sig"], "Firefox", "release", None, None, None))


class TestLines(unittest.TestCase):
    def test_rows_reach_the_spike_prompt(self):
        result = {"sample": 40, "baseline": 80, "tested": 300,
                  "scope": {"product": "Firefox", "version": "158.0", "process_type": "parent",
                            "platform": "Windows NT"},
                  "rows": [{"modules": ["a.dll", "b.dll"], "signers": ["Microsoft Windows"],
                            "in_sample": 28, "in_baseline": 0, "p": 1e-9, "on_stack": 0},
                           {"modules": ["drv.dll"], "signers": [], "in_sample": 12,
                            "in_baseline": 1, "p": 1e-4, "on_stack": 3}]}
        text = "\n".join(coloaded.lines(result))
        self.assertIn("other Firefox 158.0 parent crashes on Windows NT", text)
        self.assertIn("  - a.dll, b.dll [Microsoft Windows]: 28/40 vs 0/80", text)
        self.assertIn("  - drv.dll: 12/40 vs 1/80; on the crashing stack in 3", text)
        self.assertIn("does not by itself implicate the module", text)
        prompt = spike_agent._user_prompt({"signature": "sig", "coloaded": result})
        self.assertIn("MODULES LOADED MORE OFTEN", prompt)
        self.assertEqual(coloaded.lines(dict(result, rows=[])), [])
        self.assertNotIn("MODULES LOADED", spike_agent._user_prompt({"signature": "sig"}))

    def test_the_brief_asks_with_the_crash_scope(self):
        esc = mock.Mock(signature="sig", product="Firefox", channel="release")
        seed = {"raw_crash": {"version": "158.0", "process_type": "parent",
                              "os_name": "Windows NT"}}
        with mock.patch.object(coloaded, "for_signature", return_value=None) as measure:
            spike_escalation._coloaded(esc, ["other", "sig"], seed)
        measure.assert_called_once_with(["sig", "other"], "Firefox", "release", "158.0",
                                        "parent", "Windows NT")
        self.assertIn("coloaded", spike_escalation._public_brief({"coloaded": {"rows": []}}))


if __name__ == "__main__":
    unittest.main()
