# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import os

# crashclouseau/__init__.py builds a Flask-SQLAlchemy app at import time, which
# needs a DB URL. Tests never touch the DB, so a dummy sqlite URL is enough and
# keeps this module runnable standalone.
os.environ.setdefault("DATABASE_URL", "sqlite://")

import subprocess  # noqa: E402
import sys  # noqa: E402
import types  # noqa: E402
import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import searchfox as sf  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "searchfox")


def load(name):
    with open(os.path.join(FIX, name + ".md")) as f:
        return f.read()


def _proc(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(
        returncode=returncode, stdout=stdout, stderr=stderr
    )


class FakeRun:
    """Stand-in for ``subprocess.run`` that pops queued responses and records
    the argv (and kwargs) of every call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.kwargs = []

    def __call__(self, cmd, *a, **kw):
        self.calls.append(cmd)
        self.kwargs.append(kw)
        r = self.responses.pop(0) if self.responses else _proc(0, "", "")
        if isinstance(r, BaseException):
            raise r
        return r


# --- parser tests (no binary required) --------------------------------------


class ParserTest(unittest.TestCase):
    R = sf.Repo.CENTRAL

    def test_no_llm_deps_imported(self):
        # Acceptance criterion: the adapter pulls in no LLM/dossier deps.
        self.assertNotIn("anthropic", sys.modules)

    def test_calls_from(self):
        g = sf._parse_call_graph(load("calls_from"), self.R, rev_label="rev12")
        self.assertEqual(g.direction, "from")
        self.assertEqual(g.depth, 1)
        self.assertTrue(g.queried_tip)
        self.assertEqual(g.rev_label, "rev12")
        # root is echoed in its own output: enriched, not emitted as a self-edge
        self.assertEqual(g.root.pretty, "mozilla::dom::AudioContext::CreateGain")
        self.assertTrue(g.root.symbol_id.startswith("_ZN7mozilla3dom12AudioContext"))
        self.assertEqual(len(g.edges), 2)
        callees = {e.callee.pretty for e in g.edges}
        self.assertEqual(
            callees,
            {
                "mozilla::dom::GainNode::Create",
                "mozilla::dom::GainOptions::GainOptions",
            },
        )
        for e in g.edges:
            self.assertEqual(e.caller.pretty, g.root.pretty)
            self.assertEqual(e.depth, 1)
            self.assertIsNotNone(e.callee.symbol_id)
            self.assertEqual(e.permalink, e.callee.permalink)
            self.assertTrue(
                e.callee.permalink.startswith(
                    "https://searchfox.org/firefox-main/source/"
                )
            )
            self.assertEqual(e.callee.rev, "rev12")

    def test_calls_from_depth2_free_functions_group(self):
        g = sf._parse_call_graph(load("calls_from_depth2"), self.R)
        self.assertEqual(g.depth, 2)
        # 8 bullets, one of which is the echoed root -> 7 edges
        self.assertEqual(len(g.edges), 7)
        pretties = {e.callee.pretty for e in g.edges}
        self.assertIn("NS_warn_if_impl", pretties)  # "## Free functions" group
        self.assertIn("JS::Handle::Handle<T>", pretties)  # template in name

    def test_calls_from_overloads(self):
        # Every node here is a multi-line "(N overloads)" block: each overload
        # is a distinct mangled id sharing one pretty name -> one edge each.
        g = sf._parse_call_graph(load("calls_from_overloads"), self.R)
        self.assertEqual(len(g.edges), 6)  # 3 groups x 2 overloads
        for e in g.edges:
            self.assertIsNotNone(e.callee.symbol_id)
            self.assertTrue(e.callee.permalink.startswith("https://searchfox.org/"))
        # the two overloads of one pretty name have distinct mangled ids
        analyser = [
            e for e in g.edges
            if e.callee.pretty == "mozilla::dom::AnalyserNode::SizeOfIncludingThis"
        ]
        self.assertEqual(len(analyser), 2)
        self.assertNotEqual(analyser[0].callee.symbol_id, analyser[1].callee.symbol_id)

    def test_calls_from_mixed_inline_and_overloads(self):
        # A graph mixing one inline bullet and one overloads block must yield
        # ALL edges (regression: overloaded nodes were silently dropped).
        g = sf._parse_call_graph(load("calls_from_mixed"), self.R)
        self.assertEqual(len(g.edges), 3)  # 1 inline + 2 overloads
        pretties = sorted(e.callee.pretty for e in g.edges)
        self.assertEqual(
            pretties,
            [
                "mozilla::dom::AudioNode::SizeOfExcludingThis",
                "mozilla::dom::AudioNode::SizeOfExcludingThis",
                "mozilla::dom::GainNode::Create",
            ],
        )

    def test_calls_to_swaps_direction(self):
        g = sf._parse_call_graph(load("calls_to"), self.R)
        self.assertEqual(g.direction, "to")
        self.assertEqual(len(g.edges), 2)
        # calls-to: listed functions are the *callers* of the root
        for e in g.edges:
            self.assertEqual(e.callee.pretty, "mozilla::dom::GainNode::Create")
        callers = {e.caller.pretty for e in g.edges}
        self.assertEqual(
            callers,
            {
                "mozilla::dom::AudioContext::CreateGain",
                "mozilla::dom::GainNode::Constructor",
            },
        )

    def test_calls_between(self):
        g = sf._parse_calls_between(load("calls_between"), self.R)
        self.assertEqual(g.direction, "between")
        self.assertEqual(g.depth, 3)
        self.assertEqual(len(g.edges), 3)
        for e in g.edges:
            # both ends carry a mangled id and a synthesised source link
            self.assertTrue(e.caller.symbol_id.startswith("_Z"))
            self.assertTrue(e.callee.symbol_id.startswith("_Z"))
            self.assertEqual(e.depth, 1)  # each listed pair is a direct call
            self.assertTrue(e.permalink.startswith("https://searchfox.org/"))
        e0 = g.edges[0]
        self.assertEqual(e0.caller.pretty, "mozilla::dom::AudioContext::CollectReports")
        self.assertEqual(e0.callee.pretty, "mozilla::dom::AudioNode::SizeOfIncludingThis")

    def test_define(self):
        d = sf._parse_definition(
            load("define"), self.R, "revX", load("define_permalink")
        )
        self.assertEqual(d.start_line, 119)
        self.assertEqual(d.end_line, 131)
        # line-number prefixes stripped, real indentation + blank lines kept
        self.assertTrue(d.source.startswith("already_AddRefed<GainNode> GainNode::Create"))
        self.assertEqual(d.source.splitlines()[-1], "}")
        self.assertEqual(d.source.splitlines()[4], "")  # line 123 is blank
        self.assertEqual(
            d.permalink,
            "https://searchfox.org/firefox-main/rev/"
            "0a7f146ccac85b8f413264042dcd764028d419ec/"
            "dom/media/webaudio/GainNode.cpp#119-131",
        )
        self.assertEqual(d.symbol.file, "dom/media/webaudio/GainNode.cpp")
        self.assertEqual(d.symbol.rev, "0a7f146ccac85b8f413264042dcd764028d419ec")

    def test_define_without_permalink(self):
        d = sf._parse_definition(load("define"), self.R)
        self.assertIsNone(d.permalink)
        self.assertEqual(d.start_line, 119)
        self.assertTrue(d.source)

    def test_define_overloads_returns_first_block_only(self):
        # An overloaded symbol yields several >>>-marked blocks; the parser must
        # return ONE coherent body (the first), not a glued Frankenstein span.
        d = sf._parse_definition(
            load("define_overloads"),
            self.R,
            None,
            load("define_overloads_permalink"),
        )
        self.assertEqual(d.start_line, 1055)
        self.assertEqual(d.end_line, 1060)  # first block only, not 1071
        self.assertNotIn("const value_type&", d.source)  # 2nd overload excluded
        self.assertEqual(d.source.splitlines()[-1], "  }")
        # permalink must match the returned block, not the second range
        self.assertTrue(d.permalink.endswith("#1055-1060"))

    def test_define_no_numbered_lines_raises(self):
        with self.assertRaises(sf.SearchfoxParseError):
            sf._parse_definition("no numbered source here", self.R)

    def test_search(self):
        hits = sf._parse_search(load("search"), self.R)
        self.assertEqual(len(hits), 3)
        self.assertEqual(hits[0].file, "dom/media/webaudio/AudioContext.h")
        self.assertEqual(hits[0].line, 260)
        self.assertIn("CreateGain", hits[0].text)
        self.assertEqual(
            hits[2].file, "__GENERATED__/dom/bindings/BaseAudioContextBinding.cpp"
        )
        for h in hits:
            self.assertTrue(
                h.permalink.startswith("https://searchfox.org/firefox-main/source/")
            )

    def test_empty_results(self):
        self.assertEqual(len(sf._parse_call_graph(load("empty_calls_from"), self.R).edges), 0)
        self.assertEqual(
            len(sf._parse_calls_between(load("empty_calls_between"), self.R).edges), 0
        )
        self.assertEqual(len(sf._parse_search(load("search_empty"), self.R)), 0)

    def test_malformed_raises(self):
        md = load("malformed")
        with self.assertRaises(sf.SearchfoxParseError):
            sf._parse_call_graph(md, self.R)
        with self.assertRaises(sf.SearchfoxParseError):
            sf._parse_calls_between(md, self.R)
        with self.assertRaises(sf.SearchfoxParseError):
            sf._parse_search(md, self.R)

    def test_repo_tree_mapping(self):
        self.assertEqual(sf.Repo.CENTRAL.tree, "firefox-main")
        self.assertEqual(sf.Repo.BETA.tree, "firefox-beta")
        self.assertEqual(sf.Repo.RELEASE.tree, "firefox-release")
        self.assertEqual(sf.Repo.ESR140.tree, "firefox-esr140")
        self.assertEqual(sf.Repo.COMM.tree, "comm-central")
        self.assertNotIn("autoland", {r.value for r in sf.Repo})

    def test_source_url_and_permalink_extract(self):
        self.assertEqual(
            sf._source_url(sf.Repo.CENTRAL, "a/b.cpp", 10),
            "https://searchfox.org/firefox-main/source/a/b.cpp#10",
        )
        info = sf._extract_permalink(load("define_permalink"))
        self.assertEqual(info["rev"], "0a7f146ccac85b8f413264042dcd764028d419ec")
        self.assertEqual(info["start"], 119)
        self.assertEqual(info["end"], 131)
        self.assertIsNone(sf._extract_permalink("no url here"))

    def test_settings_ignores_null_config(self):
        # a present-but-null config value must fall back to the built-in default,
        # not clobber it (which would later crash int()/float() coercion).
        with mock.patch.object(
            sf.config,
            "get_searchfox",
            return_value={"timeout_secs": None, "bin": None, "retries": 5},
        ):
            cfg = sf._settings()
        self.assertEqual(cfg["timeout_secs"], sf._DEFAULTS["timeout_secs"])
        self.assertEqual(cfg["bin"], sf._DEFAULTS["bin"])
        self.assertEqual(cfg["retries"], 5)  # non-null override still applies

    def test_symbol_cleaning(self):
        self.assertEqual(
            sf._clean_symbol("NS_ProcessNextEvent(nsIThread*, bool)"),
            "NS_ProcessNextEvent",
        )
        self.assertEqual(sf._clean_symbol("mozilla::Maybe<T>::ref"), "mozilla::Maybe::ref")
        self.assertEqual(
            sf._reduce_symbol("crate::module::Renderer::render"), "Renderer::render"
        )
        self.assertEqual(sf._reduce_symbol("Foo::Bar"), "Foo::Bar")


# --- client tests (subprocess.run monkeypatched; no real binary) ------------


class ClientTest(unittest.TestCase):
    def setUp(self):
        p_which = mock.patch.object(
            sf.shutil, "which", return_value="/fake/searchfox-cli"
        )
        p_sleep = mock.patch.object(sf.time, "sleep", return_value=None)
        p_which.start()
        p_sleep.start()
        self.addCleanup(p_which.stop)
        self.addCleanup(p_sleep.stop)

    def make_client(self, responses, **kw):
        fake = FakeRun(responses)
        p = mock.patch.object(sf.subprocess, "run", fake)
        p.start()
        self.addCleanup(p.stop)
        return sf.SearchfoxClient(**kw), fake

    # -- resolution / errors --

    def test_missing_binary(self):
        with mock.patch.object(sf.shutil, "which", return_value=None):
            with self.assertRaises(sf.SearchfoxNotFound):
                sf.SearchfoxClient()

    def test_env_override_bin(self):
        with mock.patch.dict(os.environ, {"SEARCHFOX_CLI": "/opt/sfx"}):
            with mock.patch.object(sf.shutil, "which") as which:
                which.return_value = "/opt/sfx"
                sf.SearchfoxClient()
                which.assert_called_with("/opt/sfx")

    # -- flag-form argv --

    def test_calls_from_argv(self):
        client, fake = self.make_client([_proc(0, load("calls_from"))])
        client.calls_from("mozilla::dom::AudioContext::CreateGain", depth=2)
        self.assertEqual(
            fake.calls[0],
            [
                "/fake/searchfox-cli",
                "--calls-from",
                "mozilla::dom::AudioContext::CreateGain",
                "--depth",
                "2",
                "-R",
                "mozilla-central",
            ],
        )

    def test_calls_from_cleans_symbol_in_argv(self):
        client, fake = self.make_client([_proc(0, load("calls_from"))])
        client.calls_from("mozilla::dom::AudioContext::CreateGain(ErrorResult&)")
        self.assertIn("mozilla::dom::AudioContext::CreateGain", fake.calls[0])
        self.assertNotIn(
            "mozilla::dom::AudioContext::CreateGain(ErrorResult&)", fake.calls[0]
        )

    def test_depth_clamped_to_max(self):
        client, fake = self.make_client([_proc(0, load("calls_from"))])
        client.calls_from("mozilla::dom::AudioContext::CreateGain", depth=99)
        # config max_depth is 4
        i = fake.calls[0].index("--depth")
        self.assertEqual(fake.calls[0][i + 1], "4")

    def test_depth_floor(self):
        client, fake = self.make_client([_proc(0, load("calls_from"))])
        client.calls_from("mozilla::dom::AudioContext::CreateGain", depth=0)
        i = fake.calls[0].index("--depth")
        self.assertEqual(fake.calls[0][i + 1], "1")

    def test_repo_override_argv(self):
        client, fake = self.make_client([_proc(0, load("calls_from"))])
        client.calls_from("mozilla::dom::AudioContext::CreateGain", repo="mozilla-beta")
        self.assertIn("mozilla-beta", fake.calls[0])
        self.assertNotIn("mozilla-central", fake.calls[0])

    def test_unknown_repo_raises(self):
        client, _ = self.make_client([_proc(0, load("calls_from"))])
        with self.assertRaises(sf.SearchfoxInvocationError):
            client.calls_from("x::Y", repo="autoland")

    def test_constructor_default_repo_is_honored(self):
        # regression: a repo-less call must use the client's default_repo, not
        # silently fall back to the global-config default.
        client, fake = self.make_client(
            [_proc(0, load("calls_from"))], default_repo="mozilla-esr128"
        )
        client.calls_from("mozilla::dom::AudioContext::CreateGain")
        self.assertIn("mozilla-esr128", fake.calls[0])
        self.assertNotIn("mozilla-central", fake.calls[0])

    def test_subprocess_invoked_safely(self):
        # no shell=True (injection) and a timeout is always applied.
        client, fake = self.make_client([_proc(0, load("calls_from"))])
        client.calls_from("mozilla::dom::AudioContext::CreateGain")
        kw = fake.kwargs[0]
        self.assertFalse(kw.get("shell", False))
        self.assertEqual(kw.get("timeout"), client.timeout)

    # -- results / no-result --

    def test_calls_from_returns_graph(self):
        client, _ = self.make_client([_proc(0, load("calls_from"))])
        g = client.calls_from("mozilla::dom::AudioContext::CreateGain")
        self.assertEqual(len(g.edges), 2)

    def test_calls_from_no_result(self):
        # 2-part symbol: reduce == symbol, so only one invocation, then NoResult
        client, fake = self.make_client([_proc(0, load("empty_calls_from"))])
        with self.assertRaises(sf.SearchfoxNoResult):
            client.calls_from("Foo::Bar")
        self.assertEqual(len(fake.calls), 1)

    def test_calls_from_reduce_fallback(self):
        # full crate path returns empty; reduced Type::method returns edges
        client, fake = self.make_client(
            [_proc(0, load("empty_calls_from")), _proc(0, load("calls_from"))]
        )
        g = client.calls_from("crate::module::AudioContext::CreateGain")
        self.assertEqual(len(g.edges), 2)
        self.assertEqual(len(fake.calls), 2)
        self.assertIn("AudioContext::CreateGain", fake.calls[1])  # reduced form

    def test_calls_between_argv_and_result(self):
        client, fake = self.make_client([_proc(0, load("calls_between"))])
        g = client.calls_between(
            "mozilla::dom::AudioContext", "mozilla::dom::GainNode", depth=3
        )
        self.assertEqual(len(g.edges), 3)
        self.assertEqual(
            fake.calls[0][1:5],
            [
                "--calls-between",
                "mozilla::dom::AudioContext,mozilla::dom::GainNode",
                "--depth",
                "3",
            ],
        )

    def test_calls_between_no_path(self):
        client, _ = self.make_client([_proc(0, load("empty_calls_between"))])
        with self.assertRaises(sf.SearchfoxNoResult):
            client.calls_between("a::B", "c::D")

    def test_define_two_invocations(self):
        client, fake = self.make_client(
            [_proc(0, load("define")), _proc(0, load("define_permalink"))]
        )
        d = client.define("mozilla::dom::GainNode::Create")
        self.assertEqual(len(fake.calls), 2)
        self.assertNotIn("--permalink", fake.calls[0])
        self.assertIn("--permalink", fake.calls[1])
        self.assertEqual(d.symbol.pretty, "mozilla::dom::GainNode::Create")
        self.assertTrue(d.permalink.startswith("https://searchfox.org/firefox-main/rev/"))
        self.assertTrue(d.source.startswith("already_AddRefed"))

    def test_define_no_result(self):
        client, fake = self.make_client([_proc(0, "")])  # empty stdout = miss
        with self.assertRaises(sf.SearchfoxNoResult):
            client.define("mozilla::totally::Bogus")
        self.assertEqual(len(fake.calls), 1)  # no permalink call on a miss

    def test_search_argv_and_hits(self):
        client, fake = self.make_client([_proc(0, load("search"))])
        hits = client.search("AudioContext::CreateGain", regex=True, limit=10)
        self.assertEqual(len(hits), 3)
        self.assertEqual(
            fake.calls[0],
            [
                "/fake/searchfox-cli",
                "-q",
                "AudioContext::CreateGain",
                "-l",
                "10",
                "-r",
                "-R",
                "mozilla-central",
            ],
        )

    def test_search_empty_returns_list(self):
        client, _ = self.make_client([_proc(0, load("search_empty"))])
        self.assertEqual(client.search("zzznope"), [])

    def test_lookup(self):
        client, _ = self.make_client([_proc(0, load("search"))])
        refs = client.lookup("CreateGain")
        self.assertEqual(len(refs), 3)
        self.assertTrue(all(r.symbol_id is None for r in refs))
        self.assertTrue(all(r.pretty == "CreateGain" for r in refs))

    # -- retries / timeout --

    def test_timeout_raises_after_retries(self):
        exc = subprocess.TimeoutExpired("searchfox-cli", 60)
        client, fake = self.make_client([exc, exc, exc], retries=2)
        with self.assertRaises(sf.SearchfoxTimeout):
            client.calls_from("mozilla::dom::AudioContext::CreateGain")
        self.assertEqual(len(fake.calls), 3)  # initial + 2 retries

    def test_nonzero_nontransient_no_retry(self):
        client, fake = self.make_client([_proc(1, "", "some fatal error")], retries=2)
        with self.assertRaises(sf.SearchfoxInvocationError) as cm:
            client.calls_from("mozilla::dom::AudioContext::CreateGain")
        self.assertEqual(len(fake.calls), 1)  # not retried
        self.assertIn("some fatal error", cm.exception.stderr)
        self.assertEqual(cm.exception.returncode, 1)

    def test_transient_retry_then_success(self):
        client, fake = self.make_client(
            [_proc(1, "", "502 Bad Gateway"), _proc(0, load("calls_from"))],
            retries=2,
        )
        g = client.calls_from("mozilla::dom::AudioContext::CreateGain")
        self.assertEqual(len(g.edges), 2)
        self.assertEqual(len(fake.calls), 2)

    def test_transient_exhausted_raises(self):
        t = _proc(1, "", "Service Unavailable")
        client, fake = self.make_client([t, t, t], retries=2)
        with self.assertRaises(sf.SearchfoxInvocationError):
            client.calls_from("mozilla::dom::AudioContext::CreateGain")
        self.assertEqual(len(fake.calls), 3)

    # -- cache --

    def test_cache_hit(self):
        client, fake = self.make_client([_proc(0, load("calls_from"))])
        client.calls_from("mozilla::dom::AudioContext::CreateGain")
        client.calls_from("mozilla::dom::AudioContext::CreateGain")
        self.assertEqual(len(fake.calls), 1)  # second served from cache

    def test_cache_disabled(self):
        client, fake = self.make_client(
            [_proc(0, load("calls_from")), _proc(0, load("calls_from"))], cache=False
        )
        client.calls_from("mozilla::dom::AudioContext::CreateGain")
        client.calls_from("mozilla::dom::AudioContext::CreateGain")
        self.assertEqual(len(fake.calls), 2)

    def test_clear_cache(self):
        client, fake = self.make_client(
            [_proc(0, load("calls_from")), _proc(0, load("calls_from"))]
        )
        client.calls_from("mozilla::dom::AudioContext::CreateGain")
        client.clear_cache()
        client.calls_from("mozilla::dom::AudioContext::CreateGain")
        self.assertEqual(len(fake.calls), 2)


if __name__ == "__main__":
    unittest.main()
