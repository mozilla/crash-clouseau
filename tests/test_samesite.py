# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# Site matching, persistence helpers, the autofile hook and optional Postgres queries.
#   DATABASE_URL=sqlite:// python -m unittest tests.test_samesite
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import models, samesite  # noqa: E402
from tests.test_autofile import _is_postgres  # noqa: E402

_NODE = "f90a2bd32b2d"


def _f(pos, function, filename="", line=None):
    return {"stackpos": pos, "function": function, "filename": filename, "line": line}


# Fixtures sharing frame 0 with different callers.
_MAC = [_f(0, "mozilla::a11y::LocalAccessible::ContainerWidget() const",
           "accessible/generic/LocalAccessible.cpp", 3367),
        _f(1, "mozilla::a11y::LocalAccessible::ExplicitState() const",
           "accessible/generic/LocalAccessible.cpp", 1693),
        _f(2, "mozilla::a11y::Accessible::GroupPosition()", "accessible/basetypes/Accessible.cpp",
           294)]
_LINUX = [_MAC[0], _MAC[1], _f(2, "refStateSetCB(_AtkObject*)", "accessible/atk/AccessibleWrap.cpp",
                               777)]
# Sourceless frame 0 followed by frames with source lines.
_ASM = [_f(0, "ff_vp9_put_8tap_regular_4h_8_avx512icl"),
        _f(1, "inter_pred_8bpp(VP9TileData*)", "media/ffvpx/libavcodec/vp9_mc_template.c", 418),
        _f(2, "ff_vp9_inter_recon_8bpp(VP9TileData*)", "media/ffvpx/libavcodec/vp9recon.c", 657)]


class TestEligible(unittest.TestCase):
    def test_kinds_without_a_crash_site(self):
        for sig in ("shutdownhang | NtWaitForSingleObject",
                    "AsyncShutdownTimeout | profile-before-change | Foo",
                    "hang | Foo::Bar",
                    "IPCError-browser | ShutDownKill", ""):
            self.assertFalse(samesite.eligible(sig), sig)

    def test_ordinary_signatures(self):
        for sig in ("nsINode::GetBoolFlag", "stackoverflow | Foo::Bar",
                    "OOM | small | Foo::Alloc"):
            self.assertTrue(samesite.eligible(sig), sig)


class TestCrashSite(unittest.TestCase):
    def test_first_specific_frame_with_a_line(self):
        self.assertEqual(samesite.crash_site(_MAC), {
            "function": "mozilla::a11y::LocalAccessible::ContainerWidget",
            "file": "accessible/generic/LocalAccessible.cpp", "line": 3367})
        self.assertEqual(samesite.crash_site(_MAC), samesite.crash_site(_LINUX))

    def test_skip_listed_frames_are_passed_over(self):
        frames = [_f(0, "MOZ_Crash", "mfbt/Assertions.h", 10),
                  _f(1, "mozilla::ipc::FatalError(char const*, bool)",
                     "ipc/glue/ProtocolUtils.cpp", 205)] + _MAC
        self.assertEqual(samesite.crash_site(frames)["line"], 3367)

    def test_a_sourceless_specific_frame(self):
        self.assertIsNone(samesite.crash_site(_ASM))
        self.assertEqual(samesite.crash_site(_ASM, max_skip=1),
                         {"function": "inter_pred_8bpp",
                          "file": "media/ffvpx/libavcodec/vp9_mc_template.c", "line": 418})

    def test_line_zero_or_missing_is_no_line(self):
        for line in (0, -1, None):
            frames = [_f(0, "Foo::Bar()", "foo.cpp", line), _f(1, "Foo::Caller()", "foo.cpp", 40)]
            self.assertIsNone(samesite.crash_site(frames), line)
            self.assertEqual(samesite.crash_site(frames, max_skip=1)["function"], "Foo::Caller")

    def test_skip1_stops_at_the_second_sourceless_frame(self):
        frames = [_f(0, "Foo::A()"), _f(1, "Foo::B()"), _f(2, "Foo::C()", "foo.cpp", 3)]
        self.assertIsNone(samesite.crash_site(frames, max_skip=1))

    def test_no_frames(self):
        self.assertIsNone(samesite.crash_site([]))


class TestFindLinks(unittest.TestCase):
    def _run(self, uuid, signature, stacks, runs=(), spikes=(), node=_NODE):
        with mock.patch.object(models.CrashStack, "native_frames",
                               side_effect=lambda u: stacks.get(u, [])) as frames, \
                mock.patch.object(models.Dossier, "runs_for_candidate",
                                  return_value=None if runs is None else list(runs)) as dq, \
                mock.patch.object(models.SpikeEscalation, "runs_for_culprit",
                                  return_value=None if spikes is None else list(spikes)) as sq:
            out = samesite.find_links(uuid, signature, node)
        return out, frames, dq, sq

    def test_new_bug_links_an_earlier_analysis(self):
        out, _, dq, sq = self._run(
            "linux", "nsINode::GetBoolFlag", {"linux": _LINUX, "mac": _MAC},
            runs=[{"uuid": "mac", "signature": "mozilla::a11y::LocalAccessible::ContainerWidget",
                   "source": "dossier"}],
            spikes=[{"uuid": "linux", "signature": "nsINode::GetBoolFlag", "source": "spike",
                     "bug": 2077022, "mode": "spike_new_bug"}])
        dq.assert_called_once_with(_NODE)
        sq.assert_called_once_with(_NODE)
        self.assertEqual(out["node"], _NODE)
        self.assertEqual(out["site"]["line"], 3367)
        self.assertEqual(out["links"], [{
            "uuid": "mac", "signature": "mozilla::a11y::LocalAccessible::ContainerWidget",
            "source": "dossier", "via": ["site", "site_skip1"]}])

    def test_later_run_links_the_filed_bug(self):
        out, *_ = self._run(
            "mac", "mozilla::a11y::LocalAccessible::ContainerWidget",
            {"linux": _LINUX, "mac": _MAC},
            runs=[{"uuid": "mac", "signature": "mozilla::a11y::LocalAccessible::ContainerWidget",
                   "source": "dossier"}],
            spikes=[{"uuid": "linux", "signature": "nsINode::GetBoolFlag", "source": "spike",
                     "bug": 2077022, "mode": "spike_new_bug"}])
        self.assertEqual([(lk["uuid"], lk.get("bug"), lk.get("mode")) for lk in out["links"]],
                         [("linux", 2077022, "spike_new_bug")])

    def test_same_signature_other_site_and_ineligible_are_not_links(self):
        other = [_f(0, "Foo::Elsewhere()", "foo.cpp", 9)]
        out, *_ = self._run(
            "a", "Foo::Bar", {"a": _MAC, "b": _MAC, "c": other, "d": _MAC, "e": _MAC},
            runs=[{"uuid": "b", "signature": "Foo::Bar", "source": "dossier", "bug": 1,
                   "mode": "new_bug"},
                  {"uuid": "c", "signature": "Foo::Other", "source": "dossier"},
                  {"uuid": "d", "signature": "shutdownhang | Foo::Bar", "source": "dossier"},
                  {"uuid": "e", "signature": "hang | Foo::Bar", "source": "dossier"}])
        self.assertEqual(out["links"], [])

    def test_skip1_only_link(self):
        sibling = [_f(0, "ff_vp9_avg_8tap_regular_16h_8_avx512icl")] + _ASM[1:]
        out, *_ = self._run("a", "ff_vp9_put_8tap_regular_4h_8_avx512icl",
                            {"a": _ASM, "b": sibling},
                            runs=[{"uuid": "b", "signature": "ff_vp9_avg_8tap_regular_16h_8_avx512icl",
                                   "source": "dossier", "bug": 7, "mode": "new_bug"}])
        self.assertIsNone(out["site"])
        self.assertEqual([lk["via"] for lk in out["links"]], [["site_skip1"]])

    def test_no_node_or_ineligible_signature_reads_nothing(self):
        for node, sig in (("", "Foo::Bar"), ("f90a2b", "Foo::Bar"), ("not-a-hex-node", "Foo::Bar"),
                          (_NODE, "shutdownhang | Foo")):
            out, frames, dq, _ = self._run("a", sig, {"a": _MAC}, node=node)
            self.assertIsNone(out)
            frames.assert_not_called()
            dq.assert_not_called()

    def test_a_40_digit_node_is_cut_to_12(self):
        out, _, dq, _ = self._run("a", "Foo::Bar", {"a": _MAC}, node=_NODE.upper() + "0" * 28)
        self.assertEqual(out["node"], _NODE)
        dq.assert_called_once_with(_NODE)

    def test_no_site_records_without_a_lookup(self):
        out, _, dq, _ = self._run("a", "Foo::Bar", {"a": [_f(0, "MOZ_Crash")]})
        self.assertEqual(out, {"node": _NODE, "site": None, "site_skip1": None, "links": []})
        dq.assert_not_called()

    def test_a_failed_lookup_is_none(self):
        self.assertIsNone(self._run("a", "Foo::Bar", {"a": _MAC}, runs=None)[0])
        self.assertIsNone(self._run("a", "Foo::Bar", {"a": _MAC}, spikes=None)[0])

    def test_links_are_capped(self):
        n = samesite._MAX_LINKS + 3
        runs = [{"uuid": "u{}".format(i), "signature": "Sig{}".format(i), "source": "dossier"}
                for i in range(n)]
        stacks = {"a": _MAC, **{r["uuid"]: _MAC for r in runs}}
        out, *_ = self._run("a", "Foo::Bar", stacks, runs=runs)
        self.assertEqual(len(out["links"]), samesite._MAX_LINKS)
        self.assertEqual(out["links_dropped"], 3)

    def test_each_uuid_is_read_once(self):
        runs = [{"uuid": "b", "signature": "Sig", "source": "dossier"}]
        spikes = [{"uuid": "b", "signature": "Sig", "source": "spike", "bug": 9,
                   "mode": "spike_new_bug"}]
        out, frames, *_ = self._run("a", "Foo::Bar", {"a": _MAC, "b": _MAC}, runs=runs,
                                    spikes=spikes)
        self.assertEqual(len(out["links"]), 2)
        self.assertEqual([c.args[0] for c in frames.call_args_list], ["a", "b"])


class TestRecord(unittest.TestCase):
    def setUp(self):
        self.record = {"node": _NODE, "site": None, "site_skip1": None, "links": []}

    def _enabled(self, on):
        return mock.patch.object(samesite.config, "get_agent_same_site",
                                 return_value={"enabled": on})

    def test_disabled_reads_and_writes_nothing(self):
        with self._enabled(False), mock.patch.object(samesite, "find_links") as find, \
                mock.patch.object(models.Dossier, "merge_payload") as merge:
            self.assertIsNone(samesite.record_for_dossier("u", "Foo::Bar", _NODE))
        find.assert_not_called()
        merge.assert_not_called()

    def test_dossier_record_is_stored(self):
        with self._enabled(True), \
                mock.patch.object(samesite, "find_links", return_value=self.record) as find, \
                mock.patch.object(models.Dossier, "merge_payload") as merge:
            out = samesite.record_for_dossier("u", "Foo::Bar", _NODE)
        find.assert_called_once_with("u", "Foo::Bar", _NODE)
        self.assertIn("at", out)
        merge.assert_called_once_with("u", {"same_site": out})

    def test_nothing_to_record_writes_nothing(self):
        with self._enabled(True), mock.patch.object(samesite, "find_links", return_value=None), \
                mock.patch.object(models.Dossier, "merge_payload") as merge:
            self.assertIsNone(samesite.record_for_dossier("u", "Foo::Bar", None))
        merge.assert_not_called()

    def test_a_raising_lookup_is_contained(self):
        with self._enabled(True), \
                mock.patch.object(samesite, "find_links", side_effect=RuntimeError("db")), \
                mock.patch.object(samesite.db.session, "rollback") as rollback, \
                mock.patch.object(models.Dossier, "merge_payload") as merge:
            self.assertIsNone(samesite.record_for_dossier("u", "Foo::Bar", _NODE))
        rollback.assert_called_once()
        merge.assert_not_called()

    def test_a_raising_write_is_contained(self):
        with self._enabled(True), mock.patch.object(samesite, "find_links",
                                                    return_value=self.record), \
                mock.patch.object(samesite.db.session, "rollback") as rollback, \
                mock.patch.object(models.Dossier, "merge_payload", side_effect=RuntimeError):
            self.assertIsNone(samesite.record_for_dossier("u", "Foo::Bar", _NODE))
        rollback.assert_called_once()

    def test_spike_record_is_stored_on_the_escalation(self):
        esc = mock.Mock(id=3, uuid="u", signature="nsINode::GetBoolFlag")
        with self._enabled(True), \
                mock.patch.object(samesite, "find_links", return_value=self.record) as find:
            out = samesite.record_for_spike(esc, _NODE)
        find.assert_called_once_with("u", "nsINode::GetBoolFlag", _NODE)
        esc.merge_payload.assert_called_once_with({"same_site": out})

    def test_spike_without_a_crash_records_nothing(self):
        esc = mock.Mock(id=3, uuid=None, signature="Foo::Bar")
        with self._enabled(True), mock.patch.object(samesite, "find_links") as find:
            self.assertIsNone(samesite.record_for_spike(esc, _NODE))
        find.assert_not_called()
        esc.merge_payload.assert_not_called()


class TestAutofileHook(unittest.TestCase):
    def test_every_settled_run_is_recorded(self):
        from crashclouseau import bugzilla_apply
        from crashclouseau.agent import orchestrator
        info = {"signature": "Foo::Bar", "channel": "nightly", "product": "Firefox",
                "buildid": None}
        for res in ({"filed": True, "bug": 5, "mode": "new_bug"},
                    {"filed": False, "skipped": "verdict abstain not fileable"},
                    {"filed": False, "skipped": "autofile disabled"}):
            with mock.patch.object(models.CrashStack, "get_by_uuid", return_value=({}, info)), \
                    mock.patch.object(bugzilla_apply, "autofile_bug", return_value=res), \
                    mock.patch.object(models.Dossier, "record_filing_decline"), \
                    mock.patch.object(samesite, "record_for_dossier") as rec:
                orchestrator._autofile("u-1", {"dossier": {"candidate": {"node": _NODE}}},
                                       {"verdict": "lead", "confidence": 70})
            rec.assert_called_once_with("u-1", "Foo::Bar", _NODE)


@unittest.skipUnless(_is_postgres(), "the JSONB queries need a disposable Postgres")
class TestQueries(unittest.TestCase):
    def setUp(self):
        from datetime import datetime, timezone
        from crashclouseau import db
        models.create()
        self.db = db
        build = models.Build(datetime(2026, 9, 23, 9, 13, 38, tzinfo=timezone.utc), "Firefox",
                             "nightly", "158.0a1", None)
        db.session.add(build)
        db.session.commit()
        self.build = build
        self.addCleanup(self._cleanup)
        self.escs = []

    def _cleanup(self):
        db = self.db
        db.session.rollback()
        for esc in self.escs:
            db.session.delete(esc)
        db.session.delete(self.build)
        db.session.commit()

    def _uuid(self, name, signature, candidate=None, filed=None, status="done", frames=()):
        db = self.db
        sigid = models.Signature.get_id(signature)
        row = models.UUID(name, sigid, name, self.build.id)
        db.session.add(row)
        db.session.commit()
        for pos, fn, filename, line in frames:
            db.session.add(models.CrashStack(row.id, pos, False, "", "", filename, fn, line,
                                             "", True))
        db.session.add(models.CrashStack(row.id, 0, True, "", "", "Foo.java", "java.Foo", 1,
                                         "", True))
        db.session.commit()
        if candidate is not None:
            models.Dossier.upsert(name, payload={"dossier": {"candidate": {"node": candidate}}},
                                  status=status)
        if filed is not None:
            models.Dossier.record_filed_bug(name, filed)

    def test_native_frames(self):
        self._uuid("ss-f", "Foo::Bar", frames=[(1, "Foo::Caller()", "foo.cpp", 40),
                                               (0, "Foo::Bar()", "foo.cpp", 12)])
        self.assertEqual(models.CrashStack.native_frames("ss-f"), [
            {"stackpos": 0, "function": "Foo::Bar()", "filename": "foo.cpp", "line": 12},
            {"stackpos": 1, "function": "Foo::Caller()", "filename": "foo.cpp", "line": 40}])
        self.assertEqual(models.CrashStack.native_frames("missing"), [])

    def test_runs_for_candidate(self):
        self._uuid("ss-1", "Sig::One", candidate=_NODE)
        self._uuid("ss-2", "Sig::Two", candidate=_NODE + "0123456789abcdef0123456789ab",
                   filed={"filed": True, "bug": 2077022, "mode": "new_bug"})
        self._uuid("ss-3", "Sig::Three", candidate=_NODE,
                   filed={"filed": False, "skipped": "x", "bug": 9})
        self._uuid("ss-4", "Sig::Four", candidate="0" * 12)
        self._uuid("ss-5", "Sig::Five", candidate=_NODE, status="error")
        self.assertEqual(models.Dossier.runs_for_candidate(_NODE), [
            {"uuid": "ss-1", "signature": "Sig::One", "source": "dossier"},
            {"uuid": "ss-2", "signature": "Sig::Two", "source": "dossier", "bug": 2077022,
             "mode": "new_bug"},
            {"uuid": "ss-3", "signature": "Sig::Three", "source": "dossier"}])

    def test_runs_for_culprit(self):
        from datetime import date
        db = self.db
        for i, (node, filing, status) in enumerate((
                (_NODE + "00", {"filed": True, "bug": 2077022, "mode": "spike_new_bug"}, "done"),
                (_NODE, {"filed": False, "skipped": "x"}, "done"),
                ("1" * 12, {"filed": True, "bug": 1, "mode": "spike_new_bug"}, "done"),
                (_NODE, {}, "error"))):
            esc = models.SpikeEscalation.create("Spike::Sig{}".format(i), "Firefox", "beta",
                                                date(2026, 9, 28), uuid="sp-{}".format(i),
                                                payload={"findings": {"culprit": {"node": node}},
                                                         "filing": filing})
            esc.status = status
            db.session.commit()
            self.escs.append(esc)
        self.assertEqual(models.SpikeEscalation.runs_for_culprit(_NODE), [
            {"uuid": "sp-0", "signature": "Spike::Sig0", "source": "spike", "bug": 2077022,
             "mode": "spike_new_bug"},
            {"uuid": "sp-1", "signature": "Spike::Sig1", "source": "spike"}])


if __name__ == "__main__":
    unittest.main()
