# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# Regressor-label validation in the corpus labeller. An audit of corpus_ship found 103 of 385
# resolved landing nodes (27%) unusable as regressor labels.
#   DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
#     python -m unittest tests.test_corpus_labels
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau.eval import corpus  # noqa: E402

_BUILD = datetime(2026, 3, 30, tzinfo=timezone.utc)


def _rev(desc, pushed, node="abc123def456", backedoutby=""):
    return {"node": node, "desc": desc, "pushdate": pushed.timestamp(),
            "backedoutby": backedoutby}


class TestRejectReason(unittest.TestCase):
    def _reason(self, rev, bug=2023381, build=_BUILD):
        with mock.patch.object(corpus.Revision, "get_revision", return_value=rev):
            reason, _pushed = corpus._reject_reason("abc123def456", "nightly", build, bug)
        return reason

    def test_a_normal_landing_before_the_build_is_kept(self):
        self.assertIsNone(self._reason(
            _rev("Bug 2023381 - Keep wake lock type r=emilio",
                 datetime(2026, 3, 20, tzinfo=timezone.utc))))

    def test_landing_after_the_crash_build_is_rejected(self):
        # Causally impossible, and the most common corpus label defect (10 of 64 positives).
        reason = self._reason(_rev("Bug 2023381 - a real fix",
                                   datetime(2026, 5, 12, tzinfo=timezone.utc)))
        self.assertIn("after the crash build", reason)

    def test_lando_autoformat_is_rejected(self):
        reason = self._reason(_rev("Bug 1992198, 672103: apply code formatting via Lando",
                                   datetime(2026, 3, 20, tzinfo=timezone.utc), ),
                              bug=1992198)
        self.assertIn("cannot introduce a crash", reason)
        self.assertIn("lando auto-format", reason)

    def test_backout_and_revert_descs_are_rejected(self):
        for desc in ("Backed out changeset abcdef123456 (bug 2023381)",
                     'Revert "Bug 1971400 - Part 4 - Remove UI tests"'):
            reason = self._reason(_rev(desc, datetime(2026, 3, 20, tzinfo=timezone.utc)),
                                  bug=None)
            self.assertIsNotNone(reason, desc)
            self.assertIn("cannot introduce a crash", reason)

    def test_merge_and_tagging_are_rejected(self):
        for desc in ("Merge mozilla-central to autoland", "No bug - tagging release"):
            reason = self._reason(_rev(desc, datetime(2026, 3, 20, tzinfo=timezone.utc)),
                                  bug=None)
            self.assertIsNotNone(reason, desc)

    def test_node_belonging_to_a_different_bug_is_rejected(self):
        reason = self._reason(
            _rev("Bug 1971400 - Part 4 - Remove Bottom-sheet UI tests",
                 datetime(2026, 3, 20, tzinfo=timezone.utc)),
            bug=1972649)
        self.assertIn("belongs to bug 1971400", reason)

    def test_missing_node_is_rejected(self):
        self.assertEqual(self._reason({}), "not found on hg")

    def test_hg_failure_is_rejected_not_raised(self):
        with mock.patch.object(corpus.Revision, "get_revision",
                               side_effect=RuntimeError("hg down")):
            reason, _ = corpus._reject_reason("abc123def456", "nightly", _BUILD, 1)
        self.assertEqual(reason, "unresolvable")

    def test_unknown_build_date_skips_only_the_timing_check(self):
        # No build date -> we cannot judge ordering, but the desc checks must still apply.
        self.assertIsNone(self._reason(
            _rev("Bug 2023381 - a real fix", datetime(2026, 5, 12, tzinfo=timezone.utc)),
            build=None))
        self.assertIsNotNone(self._reason(
            _rev("Bug 2023381: apply code formatting via Lando",
                 datetime(2026, 5, 12, tzinfo=timezone.utc)),
            build=None))

    def test_a_backed_out_landing_is_still_usable(self):
        # It may well have caused the crash before being backed out, so being backed out is a
        # tie-breaker at most, never a rejection.
        self.assertIsNone(self._reason(
            _rev("Bug 2023381 - real change", datetime(2026, 3, 20, tzinfo=timezone.utc),
                 backedoutby="fff111222333")))


class TestValidateLandings(unittest.TestCase):
    def test_survivors_are_ordered_earliest_first(self):
        # The old code took sorted(nodes)[0] -- ordered by HASH, i.e. arbitrary. The earliest
        # surviving landing is the meaningful representative of when a regression was introduced.
        revs = {
            "ccc000000000": _rev("Bug 1 - part 3", datetime(2026, 3, 25, tzinfo=timezone.utc)),
            "aaa000000000": _rev("Bug 1 - part 1", datetime(2026, 3, 10, tzinfo=timezone.utc)),
            "bbb000000000": _rev("Bug 1 - part 2", datetime(2026, 3, 15, tzinfo=timezone.utc)),
        }
        landings = [
            {"node": n, "bug": 1}
            for n in ("ccc000000000", "aaa000000000", "bbb000000000")
        ]
        with mock.patch.object(corpus.Revision, "get_revision",
                               side_effect=lambda ch, n: revs[n]):
            kept, rejected = corpus._validate_landings(landings, "nightly", _BUILD)
        self.assertEqual([k["node"] for k in kept],
                         ["aaa000000000", "bbb000000000", "ccc000000000"])
        self.assertEqual(rejected, [])
        self.assertNotIn("_pushed", kept[0])          # scratch key must not leak into the label

    def test_rejects_carry_their_reason(self):
        revs = {
            "aaa000000000": _rev("Bug 1 - real", datetime(2026, 3, 10, tzinfo=timezone.utc)),
            "bbb000000000": _rev("Bug 1: apply code formatting via Lando",
                                 datetime(2026, 3, 11, tzinfo=timezone.utc)),
        }
        landings = [{"node": n, "bug": 1} for n in revs]
        with mock.patch.object(corpus.Revision, "get_revision",
                               side_effect=lambda ch, n: revs[n]):
            kept, rejected = corpus._validate_landings(landings, "nightly", _BUILD)
        self.assertEqual([k["node"] for k in kept], ["aaa000000000"])
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["node"], "bbb000000000")
        self.assertIn("lando auto-format", rejected[0]["reason"])

    def test_all_rejected_yields_no_label(self):
        with mock.patch.object(corpus.Revision, "get_revision", return_value={}):
            kept, rejected = corpus._validate_landings(
                [{"node": "aaa000000000", "bug": 1}], "nightly", _BUILD)
        self.assertEqual(kept, [])
        self.assertEqual(len(rejected), 1)


class TestRegressorLandings(unittest.TestCase):
    def test_each_node_keeps_its_own_bug(self):
        # Pooling nodes across bugs let regressor_bug describe a different changeset than
        # regressor_node -- one of the audited label defects.
        comments = {
            111: [{"text": "https://hg.mozilla.org/integration/autoland/rev/aaaaaaaaaaaa"}],
            222: [{"text": "https://hg.mozilla.org/integration/autoland/rev/bbbbbbbbbbbb"}],
        }

        class _FakeBZ:
            def __init__(self, ids, commenthandler=None):
                self._ids, self._h = ids, commenthandler

            def get_data(self):
                for bid in self._ids:
                    self._h({"comments": comments[int(bid)]}, int(bid))
                return self

            def wait(self):
                return self

            @staticmethod
            def get_landing_comments(cmts, channels):
                return [{"revision": c["text"].rsplit("/", 1)[-1], "comment": c}
                        for c in cmts]

        with mock.patch.object(corpus, "Bugzilla", _FakeBZ):
            got = corpus._regressor_landings([111, 222])
        self.assertEqual(got, [{"node": "aaaaaaaaaaaa", "bug": 111},
                               {"node": "bbbbbbbbbbbb", "bug": 222}])

    def test_no_bugs_short_circuits(self):
        self.assertEqual(corpus._regressor_landings([]), [])


class TestBuildDate(unittest.TestCase):
    def test_parses_a_buildid(self):
        self.assertEqual(corpus._build_date("20260330093332"),
                         datetime(2026, 3, 30, 9, 33, 32, tzinfo=timezone.utc))

    def test_junk_gives_none_instead_of_raising(self):
        for bad in (None, "", "not-a-buildid", "2026"):
            self.assertIsNone(corpus._build_date(bad), bad)


if __name__ == "__main__":
    unittest.main()
