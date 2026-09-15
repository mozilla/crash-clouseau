# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""The findings of the 2026-09-15 adversarial review of the Fenix nightly change, each pinned.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \\
        uv run python -m unittest tests.test_fenix_review_fixes
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

import requests  # noqa: E402

from crashclouseau import config, datacollector, models, report_bug, sigage  # noqa: E402
from crashclouseau import tcindex, trigger, utils  # noqa: E402


def _resp(status, body=None, text=None):
    r = mock.Mock()
    r.status_code = status
    if text is not None:
        r.json.side_effect = ValueError("not json")
    else:
        r.json.return_value = body
    return r


class TestTcindexNeverRaises(unittest.TestCase):
    """`tcindex.get` runs in `update.update_builds`, outside `update()`'s try/except: a
    transport error or a CDN interstitial must be a logged miss, not the end of the tick."""

    def test_a_transport_error_is_a_transient_miss_not_an_exception(self):
        day = utils.get_build_date("20260910214118").date()
        with mock.patch.object(tcindex.net, "post",
                               side_effect=requests.ConnectionError("refused")) as post, \
                mock.patch.object(tcindex.time, "sleep") as sleep, \
                self.assertLogs(level="WARNING") as logs:
            self.assertEqual(tcindex._day_children("mozilla-central", day), [])
        self.assertEqual(post.call_count, 2)           # one retry, then give up
        sleep.assert_called_once()
        self.assertTrue(any("unreachable" in line for line in logs.output))

    def test_a_non_json_200_is_a_transient_miss(self):
        day = utils.get_build_date("20260910214118").date()
        with mock.patch.object(tcindex.net, "post", return_value=_resp(200, text="<html>")), \
                self.assertLogs(level="WARNING"):
            self.assertEqual(tcindex._day_children("mozilla-central", day), [])
        with mock.patch.object(tcindex.net, "get", return_value=_resp(200, text="<html>")), \
                self.assertLogs(level="WARNING"):
            self.assertIsNone(tcindex._leaf("mozilla-central", day, "20260910214118",
                                            "fenix-nightly"))
        with mock.patch.object(tcindex.net, "get", return_value=_resp(200, body=["not", "a",
                                                                                 "dict"])), \
                self.assertLogs(level="WARNING"):
            self.assertIsNone(tcindex._task_rev("SDaKrPdWSoy_lk9_oP7QRg"))

    def test_a_read_timeout_on_the_leaf_probe_is_not_read_as_no_build(self):
        day = utils.get_build_date("20260910214118").date()
        with mock.patch.object(tcindex.net, "get",
                               side_effect=requests.ReadTimeout("slow")), \
                mock.patch.object(tcindex.time, "sleep"), \
                self.assertLogs(level="WARNING") as logs:
            self.assertIsNone(tcindex._leaf("mozilla-central", day, "20260910214118",
                                            "fenix-nightly"))
        self.assertTrue(any("not read as 'no build'" in line for line in logs.output))

    def test_get_survives_a_source_that_is_down(self):
        with mock.patch.object(tcindex.net, "post",
                               side_effect=requests.ConnectionError("down")), \
                mock.patch.object(tcindex.time, "sleep"), \
                self.assertLogs(level="WARNING"):
            self.assertEqual(tcindex.get("20260910000000", "nightly", prods="Fenix",
                                         max_buildid="20260910235959"), {})


class TestTriggerProductChannels(unittest.TestCase):
    def test_a_fenix_uuid_off_nightly_is_refused_for_the_right_reason(self):
        """D1: Fenix is nightly-only. A Fenix release report has a configured channel label
        and no builds row, and the old message blamed the ingestion window for a pair that
        never has one."""
        processed = {"product": "Fenix", "release_channel": "release", "version": "157.0",
                     "build": "20260909172920", "signature": "java.lang.Foo: at a.B.c(B.kt:1)"}
        with mock.patch.object(trigger.inspector, "get_crash_data", return_value=processed), \
                mock.patch.object(models.Build, "get_id") as get_id:
            with self.assertRaisesRegex(trigger.TriggerError, "ingested on nightly only"):
                trigger.ingest("u-1")
        get_id.assert_not_called()

    def test_a_known_uuid_without_a_build_row_answers_with_the_two_columns(self):
        with mock.patch.object(models.UUID, "get_info", return_value=None), \
                mock.patch.object(models.UUID, "get_channel", return_value="nightly"), \
                mock.patch.object(models.UUID, "get_signature", return_value="sig"), \
                self.assertLogs(level="WARNING"):
            self.assertEqual(trigger._known("u-1"),
                             {"product": None, "channel": "nightly", "signature": "sig"})


class _FakeSuperSearch(object):
    """`socorro.SuperSearch(params=, handler=, handlerdata=)` that hands the handler one
    canned response."""

    payload = None

    def __init__(self, params=None, handler=None, handlerdata=None, **_):
        handler(self.payload, handlerdata)

    def wait(self):
        return None


class TestHardwareNoiseIgnoresThePlaceholder(unittest.TestCase):
    def test_unknown_is_not_a_processor_model(self):
        """`cpu_info == "unknown"` on 6,489 of 16,368 Fenix nightly reports (2026-09-08..15) and
        on 10 of a week's desktop reports: counted as a model it reads "one CPU at 86%" into the
        prompt and into `signature_top_cpu_term`. Same rule as `machine._known_cpu`."""
        _FakeSuperSearch.payload = {
            "total": 200,
            "facets": {"possible_bit_flips_max_confidence": [],
                       "cpu_info": [{"term": "unknown", "count": 125},
                                    {"term": "family 65 model 3458 stepping 0", "count": 20},
                                    {"term": "family 65 model 3393 stepping 1", "count": 5}]},
        }
        with mock.patch.object(sigage.socorro, "SuperSearch", _FakeSuperSearch):
            noise = sigage.hardware_noise("libc.so | foo", product="Fenix", channel="nightly")
        self.assertEqual(noise["cpu_terms"], 2)
        self.assertEqual(noise["cpu_reports"], 25)
        self.assertNotEqual(noise["top_cpu_term"], "unknown")
        self.assertAlmostEqual(noise["top_cpu_share"], 20 / 25)
        # Only placeholders: no processor is known, exactly like an empty facet.
        _FakeSuperSearch.payload["facets"]["cpu_info"] = [{"term": "unknown", "count": 125}]
        with mock.patch.object(sigage.socorro, "SuperSearch", _FakeSuperSearch):
            noise = sigage.hardware_noise("libc.so | foo", product="Fenix", channel="nightly")
        self.assertIsNone(noise["cpu_reports"])
        self.assertIsNone(noise["cpu_terms"])
        self.assertIsNone(noise["top_cpu_term"])


class TestHardwareNotesArePerProduct(unittest.TestCase):
    def test_the_bug_comment_asks_the_population_about_the_crash_product(self):
        corr = {"signature_bit_flip_rate": 0.5, "signature_broken_cpu_rate": 0.2,
                "signature_hardware_sample": 40, "signature_top_cpu_share": 0.6,
                "signature_top_cpu_term": "family 6 model 183", "signature_cpu_terms": 3}
        with mock.patch.object(sigage, "population_bit_flip_rate", return_value=None) as flip, \
                mock.patch.object(sigage, "population_broken_cpu_rate", return_value=None), \
                mock.patch.object(sigage, "population_label", return_value="Fenix"), \
                mock.patch.object(sigage, "population_top_cpu_share_median",
                                  return_value=None) as med:
            report_bug.build_hardware_note(corr, "nightly", product="Fenix")
        for call in flip.call_args_list + med.call_args_list:
            self.assertEqual(call.args, ("nightly", "Fenix"))

    def test_a_fenix_note_never_quotes_the_firefox_nightly_population(self):
        corr = {"signature_bit_flip_rate": 0.5, "signature_broken_cpu_rate": 0.2,
                "signature_hardware_sample": 40, "signature_top_cpu_share": 0.6,
                "signature_top_cpu_term": "family 6 model 183", "signature_cpu_terms": 3,
                "signature_cpu_reports": 40}
        note = report_bug.build_hardware_note(corr, "nightly", product="Fenix")
        self.assertNotIn("Firefox-nightly", note)
        # Desktop: unchanged, the population comparison is quoted.
        note = report_bug.build_hardware_note(corr, "nightly", product="Firefox")
        self.assertIn("Firefox-nightly", note)


class TestNoProtosReadRollsBack(unittest.TestCase):
    def test_a_failed_select_rolls_the_session_back(self):
        with mock.patch.object(models.Selection, "recent", side_effect=RuntimeError("db")), \
                mock.patch.object(models.db.session, "rollback") as rb, \
                self.assertLogs(level="ERROR"):
            self.assertEqual(datacollector._no_protos_recently("Fenix", "nightly"), set())
        rb.assert_called_once_with()


class TestConfigStillShipsTheDecisions(unittest.TestCase):
    def test_fenix_is_nightly_only(self):
        self.assertEqual(config.get_product_channels("Fenix"), ["nightly"])


if __name__ == "__main__":
    unittest.main()
