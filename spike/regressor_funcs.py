"""Ground-truth extractor: the 'changed functions' of a regressor changeset.

Given a regressor hg node, fetch its raw unified diff (via libmozdata's
``RawRevision``) and pull out (a) the touched file paths and (b) best-effort
enclosing-function name hints from the ``@@ ... @@ <context>`` hunk headers. Those
form the **recall target**: did the searchfox neighborhood reach any of them?

Caveats (see plans/00-phase0-callgraph-spike.md):

* The ``@@`` section heading (git ``xfuncname``) is a heuristic -- decent for
  C/C++, weaker for Rust/JS, and occasionally names an *outer* function. FIXME:
  verify per language on real changesets (sub-plan #14 measures this).
* Post hg->git migration the diff may be git-formatted; both ``+++ b/<path>`` and
  a bare ``+++ <path>`` are handled.
* ``resolve=True`` canonicalises each hint through ``searchfox_cli.define`` --
  network, and subject to revision drift.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import requests

log = logging.getLogger("spike.regressor_funcs")

GITHUB_REPO = "mozilla-firefox/firefox"
_NATIVE_EXT = (".cpp", ".cc", ".cxx", ".c", ".h", ".hh", ".hpp", ".mm", ".rs")

# "+++ b/dom/media/AudioStream.cpp"  ->  dom/media/AudioStream.cpp  (skip /dev/null)
_FILE_RE = re.compile(r"^\+\+\+\s+(?:b/)?(?!/dev/null)(\S+)", re.MULTILINE)
# "@@ -12,7 +12,9 @@ <context>"  ->  captures the trailing <context> (may be empty)
_HUNK_RE = re.compile(r"^@@\s+-\d+(?:,\d+)?\s+\+\d+(?:,\d+)?\s+@@\s*(.*)$", re.MULTILINE)
# In a context tail, the identifier immediately preceding '(' is the function name.
_CTX_FUNC_RE = re.compile(r"([A-Za-z_]\w*(?:::~?[A-Za-z_]\w*)*)\s*\(")
# A bare qualified name (fallback when there is no '(').
_QUALIFIED_RE = re.compile(r"[A-Za-z_]\w*(?:::~?[A-Za-z_]\w*)+")


@dataclass
class ChangedFuncs:
    """The recall target for one regressor node."""

    node: str
    files: set[str] = field(default_factory=set)
    func_hints: set[str] = field(default_factory=set)  # from @@ headers (best-effort)
    resolved: set[str] = field(default_factory=set)  # canonicalised via searchfox

    def targets(self) -> set[str]:
        """The function-name set to test neighborhood membership against."""
        return self.resolved or self.func_hints

    def as_dict(self) -> dict:
        return {
            "node": self.node,
            "files": sorted(self.files),
            "func_hints": sorted(self.func_hints),
            "resolved": sorted(self.resolved),
        }


def fetch_diff(node: str, channel: str = "nightly") -> str | None:
    """Raw unified diff of ``node`` from hg.mozilla.org, or None on failure."""
    try:
        from libmozdata.hgmozilla import RawRevision
    except ImportError:
        log.error("libmozdata not importable; `pip install libmozdata>=0.2.12`")
        return None
    try:
        # FIXME: confirm signature/return against the installed >=0.2.12 release.
        return RawRevision.get_revision(channel, node)
    except Exception as e:  # network / 404 / node-not-on-channel
        log.warning("failed to fetch diff for %s (%s): %s", node, channel, e)
        return None


def fetch_diff_github(node: str, repo: str = GITHUB_REPO) -> str | None:
    """Unified diff of a git commit from GitHub (post hg->git regressors).

    GitHub serves ``/commit/<hash>.diff`` as a git-format unified diff, which
    ``functions_from_diff`` parses the same as an hg one.
    """
    url = f"https://github.com/{repo}/commit/{node}.diff"
    try:
        r = requests.get(url, timeout=60, headers={"User-Agent": "clouseau-spike"})
    except requests.RequestException as e:
        log.warning("github diff fetch failed for %s: %s", node, e)
        return None
    if r.status_code != 200:
        log.warning("github diff %s -> HTTP %s", node, r.status_code)
        return None
    return r.text


def _func_from_context(ctx: str) -> str | None:
    ctx = ctx.strip()
    if not ctx:
        return None
    m = _CTX_FUNC_RE.search(ctx)
    if m:
        return m.group(1)
    # No '(' in the (often truncated) section heading. It usually reads
    # "<return-type> <Class::method>", so the function is the LAST qualified
    # identifier -- taking the first would wrongly grab the return type
    # (real case: "ipc::IPCResult WebGPUParent::RecvFoo" -> WebGPUParent::RecvFoo).
    quals = _QUALIFIED_RE.findall(ctx)
    return quals[-1] if quals else None


def functions_from_diff(diff_text: str, cpp_only: bool = False) -> tuple[set[str], set[str]]:
    """(touched files, enclosing-function hints) parsed from a unified diff.

    With ``cpp_only`` only hunks in C/C++ files contribute function hints -- avoids
    polluting the target set with Python/build/doc funcs from a mixed changeset.
    """
    files: set[str] = set()
    hints: set[str] = set()
    current: str | None = None
    for line in diff_text.splitlines():
        fm = _FILE_RE.match(line)
        if fm:
            current = fm.group(1)
            files.add(current)
            continue
        hm = _HUNK_RE.match(line)
        if hm and current:
            if cpp_only and not current.endswith(_NATIVE_EXT):
                continue
            name = _func_from_context(hm.group(1))
            if name:
                hints.add(name)
    return files, hints


def changed_functions(
    node: str,
    channel: str = "nightly",
    vcs: str = "hg",
    resolve: bool = False,
    repo: str = "mozilla-central",
    github_repo: str = GITHUB_REPO,
    cpp_only: bool = False,
) -> ChangedFuncs:
    """Extract the changed files + function hints for a regressor ``node``.

    ``vcs="git"`` fetches the commit diff from GitHub (post hg->git regressors);
    ``vcs="hg"`` uses hg.mozilla.org. With ``resolve=True`` each hint is
    canonicalised through ``searchfox --define`` (network; drift-prone).
    """
    result = ChangedFuncs(node=node)
    diff = fetch_diff_github(node, github_repo) if vcs == "git" else fetch_diff(node, channel)
    if not diff:
        return result
    result.files, result.func_hints = functions_from_diff(diff, cpp_only=cpp_only)

    if resolve and result.func_hints:
        from . import searchfox_cli as sf

        for hint in result.func_hints:
            r = sf.define(hint, repo=repo)
            if r.ok and r.symbols:
                result.resolved.update(r.symbols)
    return result


if __name__ == "__main__":  # usage: python -m spike.regressor_funcs <hg-node> [channel]
    import json
    import sys

    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m spike.regressor_funcs <hg-node> [channel]")
    node = sys.argv[1]
    channel = sys.argv[2] if len(sys.argv) > 2 else "nightly"
    cf = changed_functions(node, channel=channel)
    print(json.dumps(cf.as_dict(), indent=2))
