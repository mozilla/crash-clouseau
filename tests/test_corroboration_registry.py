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


def _dict_keys(node):
    """The literal string keys of a dict display, ignoring its ``**merges``."""
    return {k.value for k in node.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)}


def _merged_into(node):
    """The expression behind every ``**x`` in a dict display (``keys`` holds ``None`` there)."""
    return [v for k, v in zip(node.keys, node.values) if k is None]


def _is_the_dict(target):
    """Is this target THE flag dict -- ``dossier.corroborations``, or a bare local of that name --
    rather than one key inside it?"""
    named = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", None)
    return named == "corroborations" and isinstance(target, (ast.Attribute, ast.Name))


def _literal_key(target):
    """``d["literal"] = v`` -> ``"literal"``, for a ``d`` already known to be a carrier."""
    if not isinstance(target, ast.Subscript) or not isinstance(target.slice, ast.Constant):
        return None
    return target.slice.value if isinstance(target.slice.value, str) else None


def _subscript_of(target, name):
    """``name["k"] = v``? (Split out of the caller only to keep the operator off a line break --
    the project's flake8 config drops the default ignores, so W503 and W504 are both live.)"""
    if not isinstance(target, ast.Subscript) or not isinstance(target.value, ast.Name):
        return False
    return target.value.id == name


def _update_call_on(node, name):
    """``name.update({...})``, with a dict display as the argument?"""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr != "update" or not isinstance(node.func.value, ast.Name):
        return False
    if node.func.value.id != name or not node.args:
        return False
    return isinstance(node.args[0], ast.Dict)


def _enclosing_scope(tree):
    """``node -> the FunctionDef it sits in`` (module if none), so a carrier local is looked up in
    the scope that built it instead of across the whole file."""
    parent = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[id(child)] = node

    def scope_of(node):
        cur = parent.get(id(node))
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return cur
            cur = parent.get(id(cur))
        return tree
    return scope_of


def _written_literals():
    """Every string key written into a ``corroborations`` dict, found in the AST.

    THE SCANNER FOLLOWS CARRIER DICTS, because most gates never touch the flag dict directly:
    they build a local (``flags``, ``facts``) and merge it in with ``**``. Reading 500 prod
    dossiers on 2026-08-24 found six flags live and undeclared for exactly that reason -- the
    first scanner recognised ``flags["k"] = v`` but not ``flags = {"k": v}``, and knew nothing
    about a carrier under any other name or built by a helper in another module. So:

      1. seed on every ``<x>.corroborations = {...}``: take its literal keys, and treat the ``y``
         of each ``**y`` as a carrier, resolved in the function that built it;
      2. for a carrier, take literal keys from ``y = {...}``, ``y["k"] = v`` and ``y.update({...})``;
      3. when a carrier is assigned from a CALL, follow that function's ``return`` -- which is how
         ``sigage.age_facts`` (five keys, a different module) becomes visible at all;
      4. plus ``_record_suppression(dossier, "flag")``, whose key is an argument.

    STILL INVISIBLE, unavoidably: a key built from a variable -- ``facts["prefix_" + key]``, or
    ``{**c, key: True}`` as the bit-flip family does. No literal scanner can resolve those, so the
    standing rule is that a flag written into corroborations uses a LITERAL key;
    ``sigage.age_facts`` was unrolled to obey it. `test_every_declared_flag_is_written` runs the
    check in the other direction, so between them a flag must be both written and declared."""
    trees = [(os.path.relpath(path, _ROOT), ast.parse(io.open(path, encoding="utf-8").read()))
             for path in _sources()]
    funcs = {}
    for rel, tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                funcs.setdefault(node.name, []).append((rel, node))

    found = {}
    work, seen = [], set()

    def emit(keys, rel):
        for key in keys:
            found.setdefault(key, set()).add(rel)

    def carrier(rel, scope, name):
        if (rel, id(scope), name) not in seen:
            seen.add((rel, id(scope), name))
            work.append((rel, scope, name))

    def follow(expr, rel, scope):
        """A ``**y`` merge, or a ``y = helper(...)`` assignment: queue or read what it names."""
        if isinstance(expr, ast.Dict):
            # An inline `**{"flag": v}`, including the `**({...} if cond else {})` that the
            # backout gate uses -- a literal write that just never passes through a variable.
            emit(_dict_keys(expr), rel)
            for merged in _merged_into(expr):
                follow(merged, rel, scope)
        elif isinstance(expr, ast.IfExp):
            follow(expr.body, rel, scope)
            follow(expr.orelse, rel, scope)
        elif isinstance(expr, ast.BoolOp):
            for value in expr.values:
                follow(value, rel, scope)
        elif isinstance(expr, ast.Name):
            carrier(rel, scope, expr.id)
        elif isinstance(expr, ast.Call):
            func = expr.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            for frel, fdef in funcs.get(name, ()):
                for node in ast.walk(fdef):
                    if not isinstance(node, ast.Return):
                        continue
                    if isinstance(node.value, ast.Dict):
                        emit(_dict_keys(node.value), frel)
                    elif isinstance(node.value, ast.Name):
                        carrier(frel, fdef, node.value.id)

    for rel, tree in trees:
        scope_of = _enclosing_scope(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and node.targets:
                target = node.targets[0]
                if _is_the_dict(target) and isinstance(node.value, ast.Dict):
                    emit(_dict_keys(node.value), rel)
                    for merged in _merged_into(node.value):
                        follow(merged, rel, scope_of(node))
                elif isinstance(target, ast.Subscript) and _is_the_dict(target.value):
                    key = _literal_key(target)
                    if key:
                        emit({key}, rel)
            is_call = isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            is_record = is_call and node.func.id == "_record_suppression" and len(node.args) > 1
            if is_record and isinstance(node.args[1], ast.Constant):
                emit({node.args[1].value}, rel)

    while work:
        rel, scope, name = work.pop()
        for node in ast.walk(scope):
            if isinstance(node, ast.Assign) and node.targets:
                target = node.targets[0]
                if isinstance(target, ast.Name) and target.id == name:
                    if isinstance(node.value, ast.Dict):
                        emit(_dict_keys(node.value), rel)
                        for merged in _merged_into(node.value):
                            follow(merged, rel, scope)
                    else:
                        follow(node.value, rel, scope)
                elif _subscript_of(target, name):
                    key = _literal_key(target)
                    if key:
                        emit({key}, rel)
            elif _update_call_on(node, name):
                emit(_dict_keys(node.args[0]), rel)

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
        # The other direction: a declaration nothing writes is folklore, and it is how a registry
        # rots into a list of names that used to matter.
        #
        # This used to grep the tree for `"flag"` ANYWHERE, which a mention in a comment or
        # membership in `_INSTANCE_SUPPRESSED` satisfies just as well as a write does. The scanner
        # now sees every write shape in the tree -- 65 of 65 declared flags -- so the check can be
        # the real claim: somebody actually assigns this key.
        unwritten = sorted(set(corroborations.REGISTRY) - set(_written_literals()))
        self.assertEqual(unwritten, [], "declared but never written")

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
        self.assertGreaterEqual(len(written), 60, "the write scanner stopped finding writes")
        self.assertIn("stale_signature_clamped", written)
        self.assertIn("compiled_out_suppressed", written)

    def test_the_scanner_still_follows_carrier_dicts(self):
        # AND pin the WIDENING, one assertion per shape it was blind to until 2026-08-24 -- a
        # floor of 45 passed happily while six live prod flags went undeclared, so the count
        # alone does not defend this. Reverting the carrier-following would fail here.
        written = _written_literals()
        # `flags = {"machine_distinct_signatures": sigs}` -- a dict LITERAL assigned to a carrier,
        # where the old scanner only understood `flags["k"] = v`.
        self.assertIn("machine_distinct_signatures", written)
        # `sigage.age_facts` builds and returns its own dict, in a module that never names
        # `corroborations`: only following the carrier through the call reaches it.
        self.assertIn("signature_clock_drift_days", written)
        self.assertEqual(sorted(written["signature_clock_drift_days"]),
                         ["crashclouseau/sigage.py"])
        # And the keys that function used to compute -- the unroll is what makes them greppable,
        # so a loop rebuilt with `"signature_age_days_" + key` fails here.
        for flag in ("signature_first_seen_ever", "signature_age_days_ever",
                     "signature_first_seen_windowed", "signature_age_days_windowed"):
            self.assertIn(flag, written, flag)
        # A branch that picks the flag: the hardware gate's three used to share a `key` variable,
        # so reverting them to `key = "..."; {**c, key: True}` fails here.
        for flag in ("possible_bit_flip_suppressed", "broken_cpu_suppressed",
                     "hardware_noise_signature_suppressed"):
            self.assertIn(flag, written, flag)
        # And an inline `**({"flag": v} if v else {})`, which passes through no variable at all.
        self.assertIn("candidate_backout_same_push", written)


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
        "cpu_info", "machine_crash_count", "machine_distinct_cpus",
        "machine_distinct_signatures", "machine_span_seconds",
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

    def test_a_rung_mover_reaches_the_bug(self):
        """A `promotion` or a `clamp` changed the number the filed bug publishes, so the bug has
        to say why -- or `UNPUBLISHED` has to carry the argument for why not.

        This is the invariant behind the whole file's origin story: `stale_signature_clamped` had
        one writer and no reader, so a rung moved and the filed bug said nothing. Reviewing bug
        2065373, :jstutte corrected three claims the run could already have checked -- the class
        of failure is "we computed it and never published it", and this is the structural half of
        the fix."""
        for flag, (kind, readers, _n) in sorted(corroborations.REGISTRY.items()):
            if kind not in ("promotion", "clamp"):
                continue
            if flag in corroborations.UNPUBLISHED:
                continue
            self.assertIn(
                "report_bug.py", readers,
                "{} is declared `{}` -- it moved the published rung. Either give it a sentence "
                "in report_bug.py, or add it to corroborations.UNPUBLISHED with the measured "
                "argument for keeping it out of the bug.".format(flag, kind))

    def test_every_unpublished_rung_mover_is_a_rung_mover_that_says_why(self):
        for flag, why in sorted(corroborations.UNPUBLISHED.items()):
            declared = corroborations.declared(flag)
            self.assertIsNotNone(declared, flag)
            self.assertIn(declared[0], ("promotion", "clamp"), flag)
            self.assertNotIn("report_bug.py", declared[1],
                             "{} DOES reach the bug -- drop the exemption".format(flag))
            self.assertTrue(why.strip(), flag)
