# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# The date anchors on report_bug's two Socorro lookups.
#   DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
#     python -m unittest tests.test_report_bug_date_anchor
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from datetime import datetime  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import report_bug  # noqa: E402


class TestBugCommentDateAnchors(unittest.TestCase):
    """SuperSearch with NO `date` searches only the last ~8 days of REPORT dates (measured
    2026-08-11: a bare nightly query answered 2026-08-04..08-11 and nothing older; a
    2026-07-26 uuid returned 0 hits bare and 1 hit anchored). Harmless while every analysed
    build was <=5 days old; with a 21-day window, an unanchored lookup silently answers
    "no crashes" and the filed bug says "0 crashes on 0 installations"."""

    def test_day_str_from_a_buildid(self):
        self.assertEqual(report_bug._day_str("20260731085738"), "2026-07-31")
        self.assertEqual(report_bug._day_str(20260731085738), "2026-07-31")

    def test_uuid_day_from_the_trailing_yymmdd(self):
        self.assertEqual(
            report_bug._uuid_day("5f52ad87-cab1-47a8-bd80-64e110260807"), "2026-08-07"
        )
        self.assertEqual(
            report_bug._uuid_day("b3951bb6-fdb8-4478-97f9-8fcbc0260726"), "2026-07-26"
        )

    def test_uuid_day_refuses_nonsense_rather_than_guessing(self):
        for bad in (None, "", "not-a-uuid", "x" * 36, "5f52ad87-cab1-47a8-bd80-64e110269932"):
            self.assertIsNone(report_bug._uuid_day(bad))

    def test_signature_stats_query_is_anchored_at_the_build(self):
        info = {
            "signature": "mozilla::places::History::History",
            "buildid": datetime(2026, 7, 31, 8, 57, 38),
            "product": "Firefox",
            "channel": "nightly",
        }
        seen = {}

        class FakeSearch:
            def __init__(self, params, handler, handlerdata):
                seen.update(params)

            def wait(self):
                return None

        report_bug._STATS_CACHE.clear()
        with mock.patch.object(report_bug.socorro, "SuperSearch", FakeSearch):
            report_bug.fetch_signature_stats("uuid-anchor-test", info)
        self.assertEqual(seen["date"], ">=2026-07-31")

    def test_crash_reason_query_is_anchored_at_the_report(self):
        seen = {}

        class FakeSearch:
            def __init__(self, params, handler, handlerdata):
                seen.update(params)

            def wait(self):
                return None

        uuid = "5f52ad87-cab1-47a8-bd80-64e110260807"
        report_bug._REASON_CACHE.clear()
        with mock.patch.object(report_bug.socorro, "SuperSearch", FakeSearch):
            report_bug.fetch_crash_reason(uuid)
        self.assertEqual(seen["date"], ">=2026-08-07")

    def test_crash_reason_omits_the_anchor_when_the_uuid_has_no_date(self):
        seen = {}

        class FakeSearch:
            def __init__(self, params, handler, handlerdata):
                seen.update(params)

            def wait(self):
                return None

        report_bug._REASON_CACHE.clear()
        with mock.patch.object(report_bug.socorro, "SuperSearch", FakeSearch):
            report_bug.fetch_crash_reason("no-date-here")
        self.assertNotIn("date", seen)


if __name__ == "__main__":
    unittest.main()
