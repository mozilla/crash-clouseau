# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# DATABASE_URL=sqlite:// python -m unittest tests.test_experts
import unittest

from crashclouseau.agent.experts import _is_bot, area_experts


def _cands(*specs):
    # each spec: (node, noise, backedout)
    return [
        {"node": n, "bug": 100 + i, "noise": noise, "backedout": bo}
        for i, (n, noise, bo) in enumerate(specs)
    ]


# The counter-example table from the 2026-08-21 panel (a year of mozilla-central, 1,646
# distinct hg authors, plus the BMO accounts the filing path can reach). Each entry is
# (email, display name, is_bot). The humans are the wrong-direction cases: the substring
# rule this replaced returned True for the first four of them.
_BOT_PANEL = [
    ("ffxbld@mozilla.com", "ffxbld", True),
    ("ffxbld@lando.moz.tools", "ffxbld", True),
    ("updatebot@mozilla.com", "Updatebot", True),          # killed the `^bot@` anchor
    ("wptsync@mozilla.com", "moz-wptsync-bot", True),      # the OLD rule missed this one
    ("lando@lando.moz.tools", "Lando", True),              # 7 of 115 replay crashes
    ("lando@lando.test", "Lando", True),
    ("release+landoscript@mozilla.com", "Release Engineering Landoscript", True),
    ("l10n-bumper@mozilla.com", "L10n Bumper Bot", True),
    ("blink-w3c-test-autoroller@chromium.org", "Blink WPT Bot", True),
    ("luci-bisection@appspot.gserviceaccount.com", "", True),
    ("crash@system.gserviceaccount.com", "Chrome Crash", True),
    ("wpt-pr-bot@users.noreply.github.com", "wpt-pr-bot", True),
    ("49699333+dependabot[bot]@users.noreply.github.com", "dependabot", True),
    ("41898282+github-actions[bot]@users.noreply.github.com", "github-actions", True),
    ("32481905+servo-wpt-sync@users.noreply.github.com", "Servo WPT Sync", True),
    ("seabld@mozilla.com", "seabld", True),
    ("tbirdbld@mozilla.com", "tbirdbld", True),
    ("servo-vcs-sync@mozilla.com", "Servo VCS Sync", True),
    ("update-bot@bmo.tld", "Update Bot", True),            # the BMO account _match_author sees
    ("bot@mozilla.com", "", True),                         # the one case the old `bot@` caught
    ("release-mgmt-account-bot@mozilla.tld", "BugBot", True),
    ("keithamus@users.noreply.github.com", "Keith Cirkel", False),
    ("jan-ivar@users.noreply.github.com", "Jan-Ivar Bruaroey", False),
    ("48995920+mcarare@users.noreply.github.com", "mcarare", False),
    ("95208+alice@users.noreply.github.com", "Alice Boxhall", False),
    ("botond@mozilla.com", "Botond Ballo", False),
    ("abotella@igalia.com", "Andreu Botella", False),
    ("cronin@mozilla.com", "P Cronin", False),
    ("sync@example.org", "S Ync", False),                  # a whole local part is not a suffix
    ("anirudh@Anirudhs-MacBook-Air.local", "Anirudh", False),
]


class TestIsBot(unittest.TestCase):
    def test_counter_example_panel(self):
        for email, name, want in _BOT_PANEL:
            with self.subTest(email=email):
                self.assertEqual(_is_bot(email, name, ""), want)

    def test_the_display_name_is_not_consulted(self):
        """A human called "Bot" keeps their name; a service keeps its address. The old rule
        matched over ``email + name + nick``, which is how ``noreply`` swallowed 138 humans."""
        self.assertFalse(_is_bot("robert@mozilla.com", "Bot Robertson", "bot"))
        self.assertTrue(_is_bot("wptsync@mozilla.com", "", ""))

    def test_no_address_is_not_a_bot(self):
        self.assertFalse(_is_bot("", "Updatebot", "updatebot"))
        self.assertFalse(_is_bot(None, None, None))


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
            # `lando@lando.moz.tools` is the bot the substring rule MISSED and the only
            # one measured reaching a crash-stack file (7 of 115 replay crashes).
            "n3": {"email": "lando@lando.moz.tools", "real": "Lando", "nick": ""},
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

    def test_a_github_privacy_address_is_a_human(self):
        """The 138-false-positive case, end to end: `noreply` used to delete this expert."""
        cands = _cands(("n1", False, False))
        authors = {"n1": {"email": "keithamus@users.noreply.github.com",
                          "real": "Keith Cirkel", "nick": ""}}
        self.assertEqual([x["name"] for x in area_experts(cands, authors)], ["Keith Cirkel"])

    def test_no_authors_yields_empty(self):
        self.assertEqual(area_experts(_cands(("n1", False, False)), {}), [])

    def test_empty_author_skipped(self):
        cands = _cands(("n1", False, False))
        authors = {"n1": {"email": "", "real": "", "nick": ""}}
        self.assertEqual(area_experts(cands, authors), [])


if __name__ == "__main__":
    unittest.main()
