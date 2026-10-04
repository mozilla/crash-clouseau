# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Link-screening tests with synthetic HTML fixtures and mocked Bugzilla requests."""

import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import bugzilla_apply, disclosure, links  # noqa: E402

# Representative crash-report, source, bug and attribution links.
_OURS = "\n".join([
    "Crash report: https://crash-stats.mozilla.org/report/index/84d14710-1b7e-4596-be43-493060261002",
    "- The failing code comes from [c065adeeac27](https://hg.mozilla.org/mozilla-central/rev/"
    "c065adeeac27) ([gh](https://github.com/mozilla-firefox/firefox/commit/0123abcd)).",
    "- [beta](https://hg.mozilla.org/releases/mozilla-beta/rev/32aab5bf983a)",
    "- [Keystore::encryptBytes](https://searchfox.org/firefox-main/source/mobile/Keystore.kt#259)",
    "[search](https://crash-stats.mozilla.org/search/?product=Fenix&_facets=url#crash-reports)",
    "see https://bugzilla.mozilla.org/show_bug.cgi?id=1961865, bug 1961865 and comment 2",
    "_Posted automatically by [Clouseau](https://github.com/mozilla/crash-clouseau), which "
    "analyses nightly crashes with an LLM._",
])


class TestAllowed(unittest.TestCase):
    def test_our_hosts_and_repositories(self):
        for url in ("https://searchfox.org/x", "https://hg.mozilla.org/x", "http://hg.mozilla.org",
                    "https://crash-stats.mozilla.org/report/index/u",
                    "https://bugzilla.mozilla.org/show_bug.cgi?id=1",
                    "HTTPS://SEARCHFOX.ORG/x", "https://github.com/mozilla-firefox/firefox/commit/a",
                    "https://github.com/mozilla/crash-clouseau"):
            self.assertTrue(links.allowed(url), url)

    def test_everything_else(self):
        for url in ("https://evil.example/", "https://github.com/evil/firefox",
                    "https://github.com/mozilla-firefox/firefox-evil", "ftp://searchfox.org/x",
                    "javascript:alert(1)", "//searchfox.org/x", "www.searchfox.org",
                    "https://searchfox.org.evil.example/", "https://searchfox.org@evil.example/",
                    "https://evil.example\\@searchfox.org/", "https://searchfox.org\t@evil.example/",
                    "https://searchfox.org @evil.example/", "/show_bug.cgi?id=1", "",
                    "https://github.com/mozilla/crash-clouseau/../../evil/repo",
                    "https://github.com/mozilla/crash-clouseau/%2e%2e/%2E%2E/evil/repo",
                    "https://github.com/mozilla-firefox/firefox/commit/./../../../evil/x",
                    "https://github.com/mozilla/crash-clouseau/.%2e/.%2E/evil",
                    "https://github.com/mozilla/crash-clouseau%2F..%2F..%2Fevil",
                    "https://[broken"):
            self.assertFalse(links.allowed(url), url)


class TestScreen(unittest.TestCase):
    def test_our_own_links_are_kept(self):
        self.assertEqual(links.screen(_OURS), (_OURS, []))

    def test_bare_urls(self):
        out, removed = links.screen("A https://evil.example/a, www.evil.example/b and "
                                    "ftp://evil.example/c.")
        self.assertEqual(out, "A (link removed), (link removed) and (link removed).")
        self.assertEqual(removed, ["https://evil.example/a", "www.evil.example/b",
                                   "ftp://evil.example/c"])

    def test_inline_links_keep_their_label(self):
        out, _ = links.screen("[label](https://evil.example/c \"t\") and ![i](https://evil.example/e) "
                              "and [https://evil.example/f](https://evil.example/f)")
        self.assertEqual(out, "label and i and (link removed)")

    def test_targets_that_leave_bmo(self):
        out, removed = links.screen("[p](//evil.example/p) [b](/\\evil.example/q) "
                                    "[n [x]](//evil.example/r) [a](<https://evil.example/s t>) "
                                    "[m](mailto:a@evil.example)")
        self.assertEqual(out, "p b [n [x]] a m")
        self.assertEqual(len(removed), 5)

    def test_reference_definitions_are_dropped(self):
        out, _ = links.screen("[ref][1] and [other][]\n\n[1]: https://evil.example/i\n"
                              "[other]: <https://evil.example/j> \"t\"\n[ok]: "
                              "https://searchfox.org/x\nend")
        self.assertEqual(out, "[ref][1] and [other][]\n\n[ok]: https://searchfox.org/x\nend")

    def test_code_is_screened_too(self):
        out, _ = links.screen("`https://evil.example/e`\n```\nE https://evil.example/g\n```")
        self.assertEqual(out, "`(link removed)`\n```\nE (link removed)\n```")

    def test_text_that_only_looks_like_links(self):
        text = ("handlers[i](event), [x](#c3), [rel](/show_bug.cgi?id=1), RefPtr<mozilla::dom::Foo>, "
                "x@evil.example, awww.example and C:\\Windows\\System32")
        self.assertEqual(links.screen(text), (text, []))

    def test_a_malformed_url_is_removed_and_logged(self):
        with self.assertLogs(level="WARNING") as logs:
            out = links.screen_write("x https://[broken y", "a comment")
        self.assertEqual(out, "x (link removed) y")
        self.assertIn("removed 1 link(s) to https://[broken from a comment", logs.output[0])

    def test_screen_write_logs_the_hosts(self):
        with self.assertLogs(level="WARNING") as logs:
            links.screen_write("x https://evil.example/a and www.other.example", "a comment")
        self.assertIn("removed 2 link(s) to evil.example, www.other.example from a comment",
                      logs.output[0])


class TestOffsite(unittest.TestCase):
    def test_bmo_renderings(self):
        rendered = (
            '<p>R <a href="https://evil.example/z" rel="nofollow">https://evil.example/z</a> '
            '<a href="/show_bug.cgi?id=1961865#c2" title="t">bug 1961865 comment 2</a> '
            '<a href="mailto:x@evil.example">x@evil.example</a> '
            '<a href="https://searchfox.org/x">s</a> <a href="">x</a> '
            '<a href="#c3">c3</a> <a href=\'//evil.example/p\'>p</a> '
            '<a href="https://evil.example&#x2F;q">q</a></p>')
        self.assertEqual(links.offsite(rendered), ["https://evil.example/z", "//evil.example/p",
                                                   "https://evil.example/q"])
        self.assertEqual(links.offsite(None), [])


class _Resp:
    def __init__(self, data):
        self._data = data
        self.status_code = 200
        self.text = ""

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class TestTheWriteHelpers(unittest.TestCase):
    def setUp(self):
        self.posts = []
        self.html = "<p>ok</p>"

        def post(url, **kw):
            self.posts.append((url, kw.get("json")))
            if url.endswith("/comment/render"):
                return _Resp({"html": self.html})
            return _Resp({"id": 7})
        for p in (mock.patch.object(bugzilla_apply.net, "post", side_effect=post),
                  mock.patch.object(bugzilla_apply.net, "put",
                                    side_effect=lambda url, **kw: self.posts.append(
                                        (url, kw.get("json"))) or _Resp({})),
                  mock.patch("crashclouseau.disclosure.public_bugs",
                             side_effect=lambda ids: {int(i) for i in ids})):
            p.start()
            self.addCleanup(p.stop)

    def _sent(self):
        return [body for url, body in self.posts if not url.endswith("/comment/render")]

    def test_a_comment_is_screened_and_rendered_before_posting(self):
        bugzilla_apply._post_comment(55, "see https://evil.example/a", False, "tok")
        self.assertTrue(self.posts[0][0].endswith("/bug/comment/render"))
        self.assertEqual(self.posts[0][1], {"text": "see (link removed)"})
        self.assertEqual(self._sent(), [{"comment": "see (link removed)", "is_private": False}])

    def test_an_update_comment_is_screened(self):
        bugzilla_apply._put_bug(55, {"comment": {"body": "[x](https://evil.example/a)"},
                                     "flags": []}, "tok")
        self.assertEqual(self._sent(), [{"comment": {"body": "x"}, "flags": []}])

    def test_a_flag_only_update_is_not_rendered(self):
        bugzilla_apply._put_bug(55, {"flags": []}, "tok")
        self.assertEqual(self.posts, [("https://bugzilla.mozilla.org/rest/bug/55", {"flags": []})])

    def test_every_text_field_of_an_update_is_screened(self):
        bugzilla_apply._put_bug(55, {
            "summary": "Crash https://evil.example", "whiteboard": "[see www.evil.example]",
            "cf_crash_signature": "[@ https://kept.example]",
            "flags": [{"name": "needinfo", "status": "?", "requestee": "a@moz.example",
                       "new": True}],
            "see_also": {"add": ["https://bugzilla.mozilla.org/show_bug.cgi?id=1"],
                         "remove": ["https://evil.example/old"]},
            "url": "https://searchfox.org/x"}, "tok")
        self.assertEqual(self._sent(), [{
            "summary": "Crash (link removed)", "whiteboard": "[see (link removed)]",
            "cf_crash_signature": "[@ https://kept.example]",
            "flags": [{"name": "needinfo", "status": "?", "requestee": "a@moz.example",
                       "new": True}],
            "see_also": {"add": ["https://bugzilla.mozilla.org/show_bug.cgi?id=1"],
                         "remove": ["https://evil.example/old"]},
            "url": "https://searchfox.org/x"}])
        # No comment, so nothing to render.
        self.assertEqual([u for u, _ in self.posts if u.endswith("/comment/render")], [])

    def test_link_fields_outside_the_allowlist_are_refused(self):
        for changes in ({"url": "https:\\\\evil.example/x"},
                        {"see_also": {"add": ["https://evil.example/y"]}},
                        {"see_also": ["https://evil.example/z"]},
                        {"see_also": {"set": [123]}}):
            with self.assertRaises(links.LinkRefused, msg=changes):
                bugzilla_apply._put_bug(55, changes, "tok")
        with self.assertRaises(links.LinkRefused):
            bugzilla_apply._create_bug({"summary": "s", "description": "d",
                                        "url": "https://evil.example"}, "tok")
        self.assertEqual(self._sent(), [])

    def test_a_new_bug_is_screened(self):
        bugzilla_apply._create_bug({"summary": "Crash www.evil.example", "groups": ["g"],
                                    "description": "https://evil.example/a"}, "tok")
        self.assertEqual(self._sent(), [{"summary": "Crash (link removed)", "groups": ["g"],
                                         "description": "(link removed)"}])

    def test_an_encoded_link_bmo_would_render_is_refused(self):
        self.html = '<p><a href="https://evil.example/z">https://evil.example/z</a></p>'
        for write in (lambda: bugzilla_apply._post_comment(55, "&#104;ttps://evil.example/z",
                                                           False, "tok"),
                      lambda: bugzilla_apply._put_bug(55, {"comment": {"body": "x"}}, "tok"),
                      lambda: bugzilla_apply._create_bug({"summary": "s", "description": "x"},
                                                         "tok")):
            with self.assertRaises(links.LinkRefused):
                write()
        self.assertEqual(self._sent(), [])

    def test_an_unreadable_rendering_is_refused(self):
        self.html = None
        with self.assertRaises(links.LinkRefused):
            bugzilla_apply._post_comment(55, "x", True, "tok")
        self.assertEqual(self._sent(), [])

    def test_a_disclosure_refusal_comes_before_the_render(self):
        with mock.patch("crashclouseau.disclosure.public_bugs", return_value={55}):
            with self.assertRaises(disclosure.DisclosureRefused):
                bugzilla_apply._post_comment(55, "bug 1855742", False, "tok")
        self.assertEqual(self.posts, [])


if __name__ == "__main__":
    unittest.main()
