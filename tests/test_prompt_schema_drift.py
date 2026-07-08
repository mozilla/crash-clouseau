# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# DATABASE_URL=sqlite:// python -m unittest tests.test_prompt_schema_drift
"""Guard against prompt<->schema drift.

The role prompts embed example JSON handoffs quoting enum tokens (failure_class,
skeptic status, citation kind, diff side). Those are hand-written duplicates of the
#03 schema. If a token drifts — a prompt edit invents one, or the schema renames a
value — the principal can copy it into the final dossier, `parse_and_validate`
rejects the whole thing, and it collapses to a FALSE ABSTAIN. This test keeps the
prompts' STRICT-enum tokens a subset of the schema. It deliberately ignores the
free-form `via`/`operation` fields (kept plain `str` in the schema precisely so the
model's descriptive values never fail validation)."""
import re
import typing
import unittest

from crashclouseau.agent import roles
from crashclouseau.agent.schema import (
    CitationKind,
    DiffLineCitation,
    FailureClass,
    SkepticStatus,
)

_PROMPTS = {name: spec["prompt"] for name, spec in roles._ROLES.items()}


def _quoted_tokens(text, field):
    """Every token quoted as ``"field":"a|b|c"`` (single value or pipe list) in text."""
    toks = set()
    for m in re.finditer(r'"%s"\s*:\s*"([^"]*)"' % re.escape(field), text):
        toks |= {t.strip() for t in m.group(1).split("|") if t.strip()}
    return toks


def _literal_values(model, field):
    return set(typing.get_args(model.model_fields[field].annotation))


class TestPromptSchemaDrift(unittest.TestCase):
    def _assert_subset(self, role, field, allowed):
        toks = _quoted_tokens(_PROMPTS[role], field)
        # Non-empty: if the shape is dropped/renamed the guard would silently pass, so
        # flag that the prompt no longer steers this enum at all.
        self.assertTrue(
            toks, "%s prompt no longer quotes any %r token (guard drift?)" % (role, field)
        )
        self.assertLessEqual(
            toks, allowed,
            "%s prompt %r quotes tokens absent from the schema: %s"
            % (role, field, sorted(toks - allowed)),
        )

    def test_failure_class_tokens(self):
        self._assert_subset(
            "crash-interpreter", "failure_class", {e.value for e in FailureClass}
        )

    def test_skeptic_status_tokens(self):
        self._assert_subset("skeptic", "status", {e.value for e in SkepticStatus})

    def test_diff_side_tokens(self):
        self._assert_subset(
            "patch-scout", "side", _literal_values(DiffLineCitation, "side")
        )

    def test_citation_kind_tokens_across_all_roles(self):
        allowed = {e.value for e in CitationKind}
        toks = set()
        for prompt in _PROMPTS.values():
            toks |= _quoted_tokens(prompt, "kind")
        self.assertIn("searchfox", toks)   # sanity: the pattern still matches
        self.assertLessEqual(
            toks, allowed,
            "a role prompt quotes citation kinds absent from the schema: %s"
            % sorted(toks - allowed),
        )

    def test_via_and_operation_stay_freeform(self):
        # These carry descriptive model values ("calls-from (virtual)", "search-hole",
        # "gc", ...) and MUST stay plain str in the schema; a Literal here reintroduces
        # the false-abstain class the guard exists to prevent.
        from crashclouseau.agent.schema import CallEdge, DataFlowHypothesis
        self.assertIs(CallEdge.model_fields["via"].annotation, str)
        self.assertIs(DataFlowHypothesis.model_fields["operation"].annotation, str)


if __name__ == "__main__":
    unittest.main()
