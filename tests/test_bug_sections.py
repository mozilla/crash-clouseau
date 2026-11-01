"""Bug sections, code fences and attribution footers."""

import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
import unittest.mock  # noqa: E402

from crashclouseau import bugzilla_apply, report_bug, spike_report, utils  # noqa: E402
from crashclouseau.agent.schema import Verdict  # noqa: E402
from crashclouseau.agent.spike_agent import SpikeFindings  # noqa: E402
from tests import test_actionable_verdict as tav  # noqa: E402

DIAG = "MOZ_DIAGNOSTIC_ASSERT(!mAudio.mSeekRequest.Exists())"
GTEST = "TEST(MediaFormatReader, SeekRace) {\n  RefPtr<MediaFormatReader> r = Make();\n}"


class TestCodeFences(unittest.TestCase):
    def test_the_language_is_guessed(self):
        for want, code in (
                ("cpp", GTEST),
                ("cpp", "mAudio.mSeekRequest.DisconnectIfExists();"),
                ("js", 'add_task(async function () {\n  const v = document.body;\n});'),
                ("html", '<video src="a.webm" loop></video>\n<script>v.play()</script>'),
                ("rust", "fn decode(buf: &mut BytesMut) {\n  let mut n = 0;\n}"),
                ("kotlin", "override fun onCreate() {\n  val x = 1\n}"),
                ("shell", "MOZ_LOG=MediaDecoder:5 ./mach run"),
                ("text", "a plain log line")):
            self.assertEqual(utils.code_language(code), want, code)

    def test_untagged_fences_get_a_tag_and_tagged_ones_stay(self):
        text = "Intro\n\n```\n" + GTEST + "\n```\n\nThen\n\n```js\nconst a = 1;\n```"
        self.assertEqual(utils.tag_code_fences(text),
                         "Intro\n\n```cpp\n" + GTEST + "\n```\n\nThen\n\n```js\nconst a = 1;\n```")

    def test_an_unclosed_fence_and_plain_text_are_unchanged(self):
        for text in ("no code here", "```\nconst a = 1;", "", None):
            self.assertEqual(utils.tag_code_fences(text), text)

    def test_a_tagged_block_can_contain_shorter_fences(self):
        text = "````text\n```\nexample\n```\n````\n\n```\n" + GTEST + "\n```"
        tagged = "````text\n```\nexample\n```\n````\n\n```cpp\n" + GTEST + "\n```"
        self.assertEqual(utils.tag_code_fences(text), tagged)
        self.assertEqual(utils.tag_code_fences(tagged), tagged)


class TestTheVerdictSections(unittest.TestCase):
    def test_fields_are_normalised_and_optional(self):
        v = Verdict(decision="abstain", abstain_reason="x", fixes="one", confirm="  run it  ")
        self.assertEqual((v.fixes, v.confirm, v.without_check), (["one"], "run it", ""))
        v = Verdict(decision="abstain", abstain_reason="x", fixes=["a", "", "b", "c", "d"],
                    without_check="w" * 2001)
        self.assertEqual((v.fixes, v.without_check), (["a", "b", "c"], ""))
        self.assertEqual(Verdict(decision="abstain", abstain_reason="x").fixes, [])

    def test_impact_names_the_process_and_the_builds(self):
        v = {"without_check": "Release builds start a second audio seek."}
        self.assertEqual(
            report_bug.build_impact_section({"process_type": "content", "moz_crash_reason": DIAG}, v),
            "**Impact**\n\nA content process crashes; the tabs it hosts show a crash page. "
            "`MOZ_DIAGNOSTIC_ASSERT` is in nightly and Developer Edition builds only; beta, "
            "release and ESR builds run on past it. Release builds start a second audio seek.")
        self.assertIn("so the whole browser closes",
                      report_bug.build_impact_section({"process_type": "parent"}, {}))
        self.assertEqual(report_bug.build_impact_section({"process_type": "gpu"}, {}),
                         "**Impact**\n\nThe GPU process crashes.")
        self.assertIsNone(report_bug.build_impact_section({}, {}))

    def test_fixes_and_confirm_render_with_tagged_code(self):
        text = report_bug.build_fix_sections({
            "fixes": ["Make `ResetDecode` disconnect every track's `mSeekRequest`."],
            "confirm": "A gtest shows the race:\n\n```\n" + GTEST + "\n```"})
        self.assertEqual(text, "**Possible fixes**\n\n- Make `ResetDecode` disconnect every "
                               "track's `mSeekRequest`.\n\n**How to confirm**\n\nA gtest shows "
                               "the race:\n\n```cpp\n" + GTEST + "\n```")
        self.assertIsNone(report_bug.build_fix_sections({"fixes": [], "confirm": ""}))


class TestRebuiltVerdictsKeepTheSections(unittest.TestCase):
    SECTIONS = {"fixes": ["Guard it."], "confirm": "Run the gtest.",
                "without_check": "Release continues."}

    def _strong(self, **kw):
        from tests.test_offstack import _strong

        d = _strong(**kw)
        d.verdict = d.verdict.model_copy(update=self.SECTIONS)
        return d

    def _kept(self, verdict):
        return {k: getattr(verdict, k) for k in self.SECTIONS}

    def test_the_exposer_downgrade_to_a_lead_keeps_them(self):
        from crashclouseau.agent import orchestrator
        from crashclouseau.agent.schema import Confidence, Decision

        from tests.test_offstack import _SF_EDGE

        d = self._strong(callpath_edges=[_SF_EDGE])
        orchestrator._classify_exposer(
            d, {"uuid": "u", "raw_crash": {"json_dump": {"crash_info": {"address": "0xe5e5e5e5"}}}})
        self.assertEqual((d.verdict.decision, d.verdict.confidence),
                         (Decision.lead, Confidence.probable))
        self.assertEqual(self._kept(d.verdict), self.SECTIONS)

    def test_an_abstain_that_keeps_the_mechanism_keeps_them(self):
        from crashclouseau.agent import orchestrator
        from crashclouseau.agent.schema import Decision

        d = self._strong()
        v = orchestrator._hot_engine_verdict("js/src/", 400, prior=d.verdict)
        self.assertEqual((v.decision, v.mechanism), (Decision.abstain, d.verdict.mechanism))
        self.assertEqual(self._kept(v), self.SECTIONS)

    def test_an_abstain_that_drops_the_mechanism_drops_them(self):
        from crashclouseau.agent import orchestrator
        from crashclouseau.agent.schema import Decision

        d = self._strong(callpath_edges=None, candidate=False)
        orchestrator._apply_callpath_gate(d, {"uuid": "u"})
        self.assertEqual((d.verdict.decision, d.verdict.mechanism), (Decision.abstain, None))
        self.assertEqual(d.verdict.fixes, [])

    def test_no_rebuild_copies_the_mechanism_by_hand(self):
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent / "crashclouseau" / "agent"
        for name in ("orchestrator.py", "schema.py"):
            text = (root / name).read_text()
            self.assertIsNone(re.search(r"mechanism=(?:\w+\.mechanism|getattr\()", text), name)


class TestTheActionableBug(unittest.TestCase):
    def _comment(self, **verdict):
        d = tav._preview_dossier()
        d["verdict"].update(verdict)
        return tav._build_preview(d, details={"moz_crash_reason": DIAG,
                                              "process_type": "content"})["comment"]

    def test_the_sections_follow_the_analysis_in_order(self):
        c = self._comment(fixes=["Disconnect every track's seek."],
                          confirm="```\n" + GTEST + "\n```")
        order = [c.find(x) for x in ("**This bug looks actionable because:**", "**Impact**",
                                     "**Possible fixes**", "**How to confirm**", "<details>")]
        self.assertEqual(order, sorted(order))
        self.assertNotIn(-1, order)
        self.assertIn("```cpp\n" + GTEST, c)

    def test_no_fix_or_confirm_means_no_section(self):
        c = self._comment()
        self.assertIn("**Impact**", c)
        self.assertNotIn("**Possible fixes**", c)
        self.assertNotIn("**How to confirm**", c)

    def test_code_in_the_mechanism_is_tagged(self):
        c = self._comment(mechanism={"statement": "It fails.\n\n```\nconst a = 1;\n```",
                                     "citations": [tav._SF_DICT]})
        self.assertIn("```js", c)


class TestTheRegressionBug(unittest.TestCase):
    def test_the_sections_follow_the_analysis(self):
        d = {"candidate": {"node": "507a4c21a8eb", "bug": 2010557, "author": "Bob <b@x.com>"},
             "corroborations": {},
             "verdict": {"decision": "lead", "confidence": "probable",
                         "mechanism": {"statement": "It fails.", "citations": []},
                         "fixes": ["Guard it."], "confirm": "Run the gtest."}}
        with unittest.mock.patch("crashclouseau.models.Node.authors_for", return_value={}):
            c = report_bug.build_bug_comment({"uuid": "u-1", "channel": "nightly"}, {}, d,
                                             details={"process_type": "parent"})
        order = [c.find(x) for x in ("It fails.", "**Impact**", "**Possible fixes**",
                                     "**How to confirm**", "<details>")]
        self.assertEqual(order, sorted(order))
        self.assertNotIn(-1, order)


class TestTheFooter(unittest.TestCase):
    def test_it_is_collapsed_and_its_summary_keeps_the_attribution(self):
        footer = report_bug._provenance("nightly")
        self.assertTrue(footer.startswith("<details>\n<summary>Filed automatically by Clouseau "
                                          "with an LLM, from a submitted crash report: not an "
                                          "independent Mozilla discovery</summary>"))
        self.assertTrue(footer.endswith("</details>"))
        self.assertIn("analyses nightly crashes", footer)
        self.assertTrue(bugzilla_apply._is_ours({"creator": "x@y.z", "text": footer}))

    def test_a_comment_on_an_existing_bug_swaps_it_for_the_short_footer(self):
        text = "Body\n\n" + report_bug._provenance("beta")
        self.assertEqual(report_bug.existing_bug_comment(text, "beta"),
                         "Body\n\n" + report_bug._comment_provenance("beta"))


class TestTheSpikeAnalysis(unittest.TestCase):
    def test_code_in_the_findings_is_tagged(self):
        findings = SpikeFindings(summary="A reproducer:\n\n```\n<video loop></video>\n```")
        self.assertIn("```html", spike_report.analysis_section(findings, {}))


if __name__ == "__main__":
    unittest.main()
