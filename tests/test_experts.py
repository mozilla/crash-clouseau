# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# DATABASE_URL=sqlite:// python -m unittest tests.test_experts
import unittest

from crashclouseau.agent.experts import area_experts


def _cands(*specs):
    # each spec: (node, noise, backedout)
    return [
        {"node": n, "bug": 100 + i, "noise": noise, "backedout": bo}
        for i, (n, noise, bo) in enumerate(specs)
    ]


class TestAreaExperts(unittest.TestCase):
    def test_picks_top_nonnoise_authors_in_order(self):
        cands = _cands(("n1", False, False), ("n2", False, False))
        authors = {
            "n1": {"email": "a@m.org", "real": "Alice", "nick": "al"},
            "n2": {"email": "b@m.org", "real": "Bob", "nick": "bob"},
        }
        xs = area_experts(cands, authors, max_experts=3)
        self.assertEqual([x["email"] for x in xs], ["a@m.org", "b@m.org"])
        self.assertEqual(xs[0]["name"], "Alice")
        self.assertIn("n1", xs[0]["reason"])

    def test_skips_noise_backedout_bots_and_dupes(self):
        cands = _cands(
            ("n1", True, False),    # noise -> skip
            ("n2", False, True),    # backed out -> skip
            ("n3", False, False),   # bot -> skip
            ("n4", False, False),   # ok
            ("n5", False, False),   # same author as n4 -> deduped
        )
        authors = {
            "n1": {"email": "x@m.org", "real": "X", "nick": ""},
            "n2": {"email": "y@m.org", "real": "Y", "nick": ""},
            "n3": {"email": "ffxbld@mozilla.com", "real": "", "nick": ""},
            "n4": {"email": "c@m.org", "real": "Carol", "nick": "c"},
            "n5": {"email": "c@m.org", "real": "Carol", "nick": "c"},
        }
        xs = area_experts(cands, authors, max_experts=5)
        self.assertEqual([x["email"] for x in xs], ["c@m.org"])

    def test_cap(self):
        cands = _cands(("n1", False, False), ("n2", False, False), ("n3", False, False))
        authors = {n: {"email": n + "@m.org", "real": n, "nick": n}
                   for n in ("n1", "n2", "n3")}
        self.assertEqual(len(area_experts(cands, authors, max_experts=2)), 2)

    def test_no_authors_yields_empty(self):
        self.assertEqual(area_experts(_cands(("n1", False, False)), {}), [])

    def test_empty_author_skipped(self):
        cands = _cands(("n1", False, False))
        authors = {"n1": {"email": "", "real": "", "nick": ""}}
        self.assertEqual(area_experts(cands, authors), [])


if __name__ == "__main__":
    unittest.main()
