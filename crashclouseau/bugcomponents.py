"""Resolve in-tree paths to Bugzilla components using mozilla-central's latest
``source-bugzilla-info`` artifact. Cache compacted prefix rules in memory and a temp file
with a one-day TTL. ``history_component`` refines a ``General`` answer from file history.
"""

import collections
import fractions
import json
import logging
import os
import re
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

GITHUB_GRAPHQL = "https://api.github.com/graphql"
HISTORY_COMMITS = 30
# Ignore large commits when estimating component ownership.
HISTORY_BULK_FILES = 40
HISTORY_BUGS = 10
HISTORY_MIN_BUGS = 5
HISTORY_DIR_LEVELS = 4
HISTORY_SHARE = 0.5
HISTORY_VOTES = 3
_BUG_HEADLINE = re.compile(r"^\s*bug[ \t]*([0-9]+)", re.I)
_REVERT_HEADLINE = re.compile(r"^\s*(?:revert|back(?:ed|ing|s)?[ _-]*out)", re.I)


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


def _path_histories(paths):
    """Fetch ``{path: [commit]}`` from GitHub's ``main`` history in API order.

    Return ``None`` without ``GITHUB_TOKEN`` or on request failure.
    """
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        return None
    if not paths:
        return {}
    fields = " ".join(
        "h{0}: history(first: {1}, path: $p{0}) {{ nodes {{ messageHeadline "
        "changedFilesIfAvailable }} }}".format(i, HISTORY_COMMITS) for i in range(len(paths)))
    query = ("query({}) {{ repository(owner: \"mozilla-firefox\", name: \"firefox\") {{ "
             "object(expression: \"main\") {{ ... on Commit {{ {} }} }} }} }}").format(
        ", ".join("$p{}: String!".format(i) for i in range(len(paths))), fields)
    try:
        r = net.post(GITHUB_GRAPHQL, headers={"Authorization": "Bearer " + token},
                     json={"query": query,
                           "variables": {"p{}".format(i): p for i, p in enumerate(paths)}},
                     timeout=(5, 30))
        data = r.json() if r.status_code == 200 else None
    except Exception:
        logger.warning("bugcomponents: file history request failed", exc_info=True)
        return None
    if not data or data.get("errors"):
        logger.warning("bugcomponents: file history refused (%s): %s",
                       r.status_code, str((data or {}).get("errors") or r.text)[:200])
        return None
    commit = ((data.get("data") or {}).get("repository") or {}).get("object") or {}
    return {p: (commit.get("h{}".format(i)) or {}).get("nodes") or []
            for i, p in enumerate(paths)}


def _focused_bugs(commits):
    """Unique bug IDs from Bug-prefixed commits changing at most ``HISTORY_BULK_FILES`` files.

    Preserve input order; ignore commits with unknown file counts.
    """
    out = []
    for c in commits or ():
        headline = c.get("messageHeadline") or ""
        files = c.get("changedFilesIfAvailable")
        m = _BUG_HEADLINE.match(headline)
        if (m and files is not None and files <= HISTORY_BULK_FILES
                and not _REVERT_HEADLINE.match(headline) and int(m.group(1)) not in out):
            out.append(int(m.group(1)))
    return out


def _parent_dirs(path, rules):
    """Up to ``HISTORY_DIR_LEVELS`` enclosing directories, nearest first, never above the
    deepest directory rule that matches ``path``."""
    parts = path.split("/")
    bound = next((i for i in range(len(parts) - 1, 0, -1)
                  if "/".join(parts[:i]) + "/" in rules), 0)
    low = max(bound, len(parts) - HISTORY_DIR_LEVELS, 1)
    return ["/".join(parts[:i]) for i in range(len(parts) - 1, low - 1, -1)]


def history_component(product, paths, bug_components, active_components, rules=None):
    """Infer an active, non-General component from recent bugs in the same product.

    Each of the first ``STACK_WINDOW`` matching files splits one vote among its readable
    bugs. Use up to ``HISTORY_BUGS`` focused bugs per file, adding directory history below
    ``HISTORY_MIN_BUGS``. Require ``HISTORY_SHARE`` of votes, a strict lead and
    ``HISTORY_VOTES`` distinct supporting bugs.

    ``bug_components(ids)`` returns ``{id: (product, component)}``;
    ``active_components(product)`` returns active component names or ``None``.
    """
    rules = _load() if rules is None else rules
    files = []
    for p in paths or ():
        if (p and p not in files and not p.startswith(STACK_SKIP) and not is_test(p)
                and (component_for(p, rules) or ("",))[0] == product):
            files.append(p)
    files = files[:STACK_WINDOW]
    if not files:
        return None
    histories = _path_histories(files)
    if histories is None:
        return None
    bugs = {f: _focused_bugs(histories.get(f))[:HISTORY_BUGS] for f in files}
    thin = {f: _parent_dirs(f, rules) for f in files if len(bugs[f]) < HISTORY_MIN_BUGS}
    more = _path_histories(sorted({d for dirs in thin.values() for d in dirs})) or {}
    for f, dirs in thin.items():
        for d in dirs:
            if len(bugs[f]) >= HISTORY_MIN_BUGS:
                break
            for b in _focused_bugs(more.get(d)):
                if len(bugs[f]) >= HISTORY_BUGS:
                    break
                if b not in bugs[f]:
                    bugs[f].append(b)
    try:
        pcs = bug_components(sorted({b for ids in bugs.values() for b in ids})) or {}
    except Exception:
        logger.warning("bugcomponents: could not read the file history's bugs", exc_info=True)
        return None
    score, supporting, voters = collections.Counter(), collections.defaultdict(set), 0
    for f in files:
        mine = [b for b in bugs[f] if b in pcs and pcs[b][0] == product]
        if not mine:
            continue
        voters += 1
        for bug in mine:
            component = pcs[bug][1]
            score[component] += fractions.Fraction(1, len(mine))
            supporting[component].add(bug)
    ranked = score.most_common(2)
    if not ranked:
        return None
    component, best = ranked[0]
    if (component == "General" or best < HISTORY_SHARE * voters
            or (len(ranked) > 1 and ranked[1][1] == best) or len(supporting[component]) < HISTORY_VOTES):
        return None
    if component not in (active_components(product) or ()):
        return None
    logger.info("bugcomponents: file history names %s :: %s (%.2f of %d files, %d bugs)",
                product, component, float(best), voters, len(supporting[component]))
    return component
