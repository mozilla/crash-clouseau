# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import unittest
from crashclouseau import buildhub, utils


class BuildhubTest(unittest.TestCase):
    def test_get(self):
        res = buildhub.get("20180201000000", "nightly", max_buildid="20180201110000")
        self.assertEqual(set(res.keys()), {"FennecAndroid", "Firefox", "Thunderbird"})
        for v in res.values():
            self.assertIn("nightly", v)
        self.assertIn(
            utils.get_build_date("20180201100053"), res["FennecAndroid"]["nightly"]
        )
        self.assertIn(utils.get_build_date("20180201100326"), res["Firefox"]["nightly"])
        self.assertIn(
            utils.get_build_date("20180201030201"), res["Thunderbird"]["nightly"]
        )

        self.assertEqual(
            res["FennecAndroid"]["nightly"][utils.get_build_date("20180201100053")],
            {"revision": "17ade9f88b6e", "version": "60.0a1"},
        )
        self.assertEqual(
            res["Firefox"]["nightly"][utils.get_build_date("20180201100326")],
            {"revision": "17ade9f88b6e", "version": "60.0a1"},
        )
        self.assertEqual(
            res["Thunderbird"]["nightly"][utils.get_build_date("20180201030201")],
            {"revision": "4ec396880934", "version": "60.0a1"},
        )

    def test_get_rev_from(self):
        rev = buildhub.get_rev_from("20180201100053", "nightly", "FennecAndroid")
        self.assertEqual(rev, "17ade9f88b6e")
        rev = buildhub.get_rev_from("20180201100326", "nightly", "Firefox")
        self.assertEqual(rev, "17ade9f88b6e")
        rev = buildhub.get_rev_from("20180201030201", "nightly", "Thunderbird")
        self.assertEqual(rev, "4ec396880934")

    def test_get_two_last(self):
        res = buildhub.get_two_last("20180201100053", "nightly", "FennecAndroid")
        self.assertEqual(
            res[0],
            {
                "buildid": "20180131100700",
                "revision": "7b46ef2ae141",
                "version": "60.0a1",
            },
        )
        self.assertEqual(
            res[1],
            {
                "buildid": "20180201100053",
                "revision": "17ade9f88b6e",
                "version": "60.0a1",
            },
        )

    def test_get_enclosing_builds(self):
        res = buildhub.get_enclosing_builds("20180206095500", "nightly", "Firefox")
        self.assertEqual(
            res,
            [
                {
                    "buildid": "20180205220102",
                    "revision": "0d806b3230fe",
                    "version": "60.0a1",
                },
                {
                    "buildid": "20180206100151",
                    "revision": "f1a4b64f19b0",
                    "version": "60.0a1",
                },
            ],
        )
