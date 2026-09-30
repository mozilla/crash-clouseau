"""Resolve in-tree paths to Bugzilla components using mozilla-central's latest
``source-bugzilla-info`` artifact. Cache compacted prefix rules in memory and a temp file
with a one-day TTL.
"""

import collections
import json
import logging
import os
import tempfile
import time

from crashclouseau import net

logger = logging.getLogger(__name__)

URL = (
    "https://firefox-ci-tc.services.mozilla.com/api/index/v1/task/"
    "gecko.v2.mozilla-central.latest.source.source-bugzilla-info/artifacts/public/"
    "components-normalized.json"
)
_CACHE_FILE = os.path.join(tempfile.gettempdir(), "crashclouseau-bug-components.json")
_TTL_S = 24 * 3600
_rules = None
_rules_at = 0.0

# Heuristic: skip shared infrastructure when estimating the crash's component.
STACK_SKIP = (
    "mfbt/", "mozglue/", "memory/", "xpcom/base/nsDebugImpl", "nsprpub/", "ipc/glue/",
    "ipc/chromium/", "xpcom/threads/", "toolkit/xre/", "xpcom/build/",
)
STACK_WINDOW = 8

_TEST_PARTS = ("/test/", "/tests/", "/crashtests/", "/reftests/", "/gtest/", "/mochitest")


def compact(normalized):
    """``{prefix: [product, component]}`` from ``components-normalized.json``.

    Directory rules (trailing ``/``) use the most common component below them; file and
    subdirectory overrides preserve exceptions. No root fallback is emitted, so unknown
    top-level paths remain unresolved."""
    comps = {int(k): list(v) for k, v in (normalized.get("components") or {}).items()}
    rules = {}

    def counts(node, c):
        for v in node.values():
            if isinstance(v, dict):
                counts(v, c)
            else:
                c[v] += 1
        return c

    def walk(node, prefix, inherited):
        c = counts(node, collections.Counter())
        mine = c.most_common(1)[0][0] if c else inherited
        if prefix and mine != inherited and mine in comps:
            rules[prefix] = comps[mine]
        for name, v in node.items():
            if isinstance(v, dict):
                walk(v, prefix + name + "/", mine if prefix else None)
            elif (v != mine or not prefix) and v in comps:
                rules[prefix + name] = comps[v]

    walk(normalized.get("paths") or {}, "", None)
    return rules


def _fetch():
    r = net.get(URL, timeout=120)
    r.raise_for_status()
    return compact(r.json())


def _load():
    """Load cached rules or fetch them; on fetch failure, use stale memory rules or ``{}``."""
    global _rules, _rules_at
    now = time.time()
    if _rules is not None and now - _rules_at < _TTL_S:
        return _rules
    try:
        if now - os.path.getmtime(_CACHE_FILE) < _TTL_S:
            with open(_CACHE_FILE) as f:
                _rules, _rules_at = json.load(f), now
            return _rules
    except (OSError, ValueError):
        pass
    try:
        rules = _fetch()
    except Exception:
        logger.warning("bugcomponents: could not fetch %s", URL, exc_info=True)
        return _rules or {}
    try:
        tmp = _CACHE_FILE + ".{}".format(os.getpid())
        with open(tmp, "w") as f:
            json.dump(rules, f)
        os.replace(tmp, _CACHE_FILE)
    except OSError:
        logger.warning("bugcomponents: could not cache the rules", exc_info=True)
    _rules, _rules_at = rules, now
    return rules


def component_for(path, rules=None):
    """``(product, component)`` for an in-tree path, or ``None``."""
    if not path:
        return None
    rules = _load() if rules is None else rules
    parts = path.lstrip("/").split("/")
    for i in range(len(parts), 0, -1):
        key = "/".join(parts[:i]) + ("" if i == len(parts) else "/")
        if key in rules:
            return tuple(rules[key])
    return None


def is_test(path):
    if path.startswith("testing/") or path.endswith((".list", ".ini", ".toml")):
        return True
    return any(p in path for p in _TEST_PARTS)


def majority(paths, rules=None):
    """The most common component among ``paths``; a tie goes to the first one listed."""
    rules = _load() if rules is None else rules
    pcs = [pc for pc in (component_for(p, rules) for p in paths) if pc]
    if not pcs:
        return None
    counts = collections.Counter(pcs)
    best = max(counts.values())
    return next(pc for pc in pcs if counts[pc] == best)


def file_components(changeset_files, stack_files, rules=None):
    """Return the most common ``[product, component]`` per group; omit unresolved groups.

    ``changeset`` excludes paths matched by the ``is_test`` heuristic. ``stack`` uses the
    first ``STACK_WINDOW`` resolvable frames outside ``STACK_SKIP``. ``overlap`` uses those
    frames whose files remain in the filtered changeset."""
    changed = [f for f in (changeset_files or ()) if f and not is_test(f)]
    frames = [f for f in (stack_files or ()) if f and not f.startswith(STACK_SKIP)]
    if not (changed or frames):
        return {}
    rules = _load() if rules is None else rules
    if not rules:
        return {}
    frames = [f for f in frames if component_for(f, rules)][:STACK_WINDOW]
    touched = set(changed)
    out = {
        "changeset": majority(changed, rules),
        "stack": majority(frames, rules),
        "overlap": majority([f for f in frames if f in touched], rules),
    }
    return {k: list(v) for k, v in out.items() if v}
