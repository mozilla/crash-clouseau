# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""The registry is only worth having if it cannot go stale.

    DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \
        uv run python -m unittest tests.test_corroboration_registry

These tests scan the tree rather than trusting the registry, so the failure mode of every gap the
2026-08-21 audit found is now a red test: a new flag nothing reads, a declaration nothing writes,
a suppression missing from `_INSTANCE_SUPPRESSED`, a boost policy naming a flag that does not
exist. The scanner's own blind spot is pinned too (see `test_the_scanner_still_sees_the_writes`).
"""
import ast
import glob
import io
import os
import re
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from crashclouseau import corroborations, models                      # noqa: E402
from crashclouseau.agent import orchestrator                          # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _sources():
    for path in sorted(glob.glob(os.path.join(_ROOT, "crashclouseau", "**", "*.py"),
                                 recursive=True)):
        if "/vendor/" not in path:
            yield path


def _written_literals():
    """Every string key written into a ``corroborations`` dict, found in the AST.

    Deliberately literal-only. A gate that builds its key in a variable (the bit-flip family
    does) is invisible here, which is why `test_every_declared_flag_is_written` runs the check
    in the other direction as well -- between them a flag has to be both written and declared."""
    found = {}
    for path in _sources():
        tree = ast.parse(io.open(path, encoding="utf-8").read())
        rel = os.path.relpath(path, _ROOT)
        for node in ast.walk(tree):
            keys = set()
            if isinstance(node, ast.Assign) and node.targets:
                target = node.targets[0]
                name = None
                if isinstance(target, ast.Attribute):
                    name = target.attr
                elif isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
                    name = target.value.id
                    literal = isinstance(target.slice, ast.Constant) and isinstance(
                        target.slice.value, str)
                    if name in ("flags", "corroborations") and literal:
                        keys.add(target.slice.value)
                if name == "corroborations" and isinstance(node.value, ast.Dict):
                    keys |= {k.value for k in node.value.keys
                             if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            is_call = isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            is_record = is_call and node.func.id == "_record_suppression"
            if is_record and len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                keys.add(node.args[1].value)
            for key in keys:
                found.setdefault(key, set()).add(rel)
    return found


class TestRegistryCoversTheCode(unittest.TestCase):
    def test_every_written_flag_is_declared(self):
        # THE FORCING FUNCTION. `stale_signature_clamped` existed for three days with one writer
        # and no reader, and nothing anywhere would have said so.
        undeclared = sorted(set(_written_literals()) - set(corroborations.REGISTRY))
        self.assertEqual(
            undeclared, [],
            "corroboration flag(s) written but not declared in crashclouseau/corroborations.py "
            "-- add them with a kind and a reader, or with kind='diagnostic' and the reason "
            "nothing reads them")

    def test_every_declared_flag_is_written(self):
        # The other direction: a declaration nothing writes is folklore, and it is how a
        # registry rots into a list of names that used to matter.
        blob = "".join(io.open(p, encoding="utf-8").read() for p in _sources())
        missing = [f for f in sorted(corroborations.REGISTRY) if '"%s"' % f not in blob]
        self.assertEqual(missing, [], "declared but never written")

    def test_every_declared_reader_really_reads_it(self):
        # A reader list is a claim about other files. Check it the way a grep would.
        for flag, (_kind, readers, _note) in sorted(corroborations.REGISTRY.items()):
            for reader in readers:
                if reader.startswith("policy:"):
                    continue
                path = os.path.join(_ROOT, reader if reader.startswith("templates/")
                                    else os.path.join("crashclouseau", reader))
                text = io.open(path, encoding="utf-8").read()
                self.assertRegex(
                    text,
                    r'(?:get\(\s*["\']{f}["\']|\[\s*["\']{f}["\']\s*\])'.format(f=re.escape(flag)),
                    "%s claims %s reads it, and it does not" % (flag, reader))

    def test_the_scanner_still_sees_the_writes(self):
        # The scanner is literal-only, so a refactor that moves writes behind a helper would
        # silently empty it and every check above would pass vacuously. Pin the floor.
        written = _written_literals()
        self.assertGreaterEqual(len(written), 45, "the write scanner stopped finding writes")
        self.assertIn("stale_signature_clamped", written)
        self.assertIn("compiled_out_suppressed", written)


class TestTheTwoPolicyListsAgree(unittest.TestCase):
    def test_instance_suppressed_is_a_subset_of_the_declared_suppressions(self):
        for flag in models._INSTANCE_SUPPRESSED:
            self.assertIn(flag, corroborations.REGISTRY, flag)
            self.assertEqual(corroborations.REGISTRY[flag][0], "suppression", flag)
            self.assertIn("policy:_INSTANCE_SUPPRESSED", corroborations.REGISTRY[flag][1], flag)

    def test_a_suppression_declaring_the_policy_is_in_it(self):
        for flag, (_kind, readers, _note) in corroborations.REGISTRY.items():
            if "policy:_INSTANCE_SUPPRESSED" in readers:
                self.assertIn(flag, models._INSTANCE_SUPPRESSED, flag)

    def test_the_so_boost_policy_only_names_declared_flags(self):
        for flag in orchestrator._SO_BOOST_POLICY:
            self.assertIn(flag, corroborations.REGISTRY, flag)
            self.assertIn("policy:_SO_BOOST_POLICY", corroborations.REGISTRY[flag][1], flag)

    def test_a_flag_declaring_the_boost_policy_is_in_it(self):
        for flag, (_kind, readers, _note) in corroborations.REGISTRY.items():
            if "policy:_SO_BOOST_POLICY" in readers:
                self.assertIn(flag, orchestrator._SO_BOOST_POLICY, flag)


class TestWriteOnlyFlagsAreADecision(unittest.TestCase):
    # THE POINT OF THE WHOLE MODULE. Each of these is read by nothing; each is `diagnostic`
    # ON PURPOSE, because it exists to be re-measured from the persisted dossiers later. Adding
    # a flag to this set is a decision that has to be made here, in a diff, with a reason --
    # which is exactly what did not happen for `stale_signature_clamped`.
    EXPECTED = {
        "absent_named_threads", "absent_thread_clamped", "call_path_verified",
        "compiled_out_macro", "compiled_out_rev", "exposer_suspected",
        "fault_offset_unverified", "hardware_noise_signature_suppressed",
        "cpu_info", "machine_crash_count", "machine_distinct_cpus", "machine_span_seconds",
        "second_opinion_abstained", "second_opinion_clamped",
        "second_opinion_downgraded_strong", "skeptic_build_flag_unbound",
    }

    def test_the_write_only_set_is_exactly_this(self):
        self.assertEqual(corroborations.write_only(), self.EXPECTED)

    def test_every_write_only_flag_says_why(self):
        for flag in sorted(corroborations.write_only()):
            kind, _readers, note = corroborations.REGISTRY[flag]
            # `hardware_noise_signature_suppressed` is the one suppression nothing reads, and
            # its note is the argument for that; everything else here is a diagnostic.
            self.assertIn(kind, ("diagnostic", "suppression"), flag)
            if kind == "suppression":
                self.assertTrue(note, flag)

    def test_every_kind_is_one_of_the_five(self):
        for flag, (kind, _r, _n) in corroborations.REGISTRY.items():
            self.assertIn(kind, corroborations.KINDS, flag)
