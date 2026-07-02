"""Mine a *starting* corpus of candidate off-stack regressions from BMO (Phase-0).

Clouseau files bugs with ``blocked = "clouseau,{bugid}"`` (see
``crashclouseau.report_bug.improve``), so the bugs that **block the ``clouseau``
alias bug** are Clouseau's own human-confirmed finds. For each we pull
``regressed_by`` (the true regressor bug) and ``cf_crash_signature``, and scrape
the regressor bug's comments for the landed hg revision(s).

This produces an **uncurated** ``corpus.json``: it does NOT know whether a case is
off-stack, and it leaves ``uuid`` / ``build_window`` blank. Run it once, then
curate by hand (see spike/README.md) -- keep only cases whose regressor function
is absent from the crash's on-stack frames, and freeze a representative uuid.

Uses the public BMO REST API directly (stable, no auth for public bugs) rather
than libmozdata, to keep this throwaway miner simple; production would reuse
``libmozdata.bugzilla`` and lando git2hg. FIXME: security bugs hide their
signature, and regressor->node scraping is heuristic (autoland/central rev links
only; git/lando links are not resolved here).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path

import requests

log = logging.getLogger("spike.mine_corpus")

BZ = "https://bugzilla.mozilla.org/rest"
CLOUSEAU_ALIAS = "clouseau"
TIMEOUT = 60

# hg rev links in bug comments: autoland / mozilla-central / releases/*
_HG_REV_RE = re.compile(
    r"https?://hg\.mozilla\.org/(?:integration/autoland|mozilla-central|releases/[^/\s]+)/rev/([0-9a-f]{12,40})"
)
# first "[@ signature ]" entry in cf_crash_signature
_SIG_RE = re.compile(r"\[@\s*(.+?)\s*\]")


def _bz_headers() -> dict:
    """Bugzilla API-key header from mozdata.ini, for higher rate limits (optional)."""
    try:
        from libmozdata import config

        tok = config.get("Bugzilla", "token", "")
    except Exception:
        tok = ""
    return {"X-Bugzilla-API-Key": tok} if tok else {}


def _get(path: str, params: dict) -> dict:
    r = requests.get(f"{BZ}/{path}", params=params, headers=_bz_headers(), timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def clouseau_bug_id() -> int | None:
    try:
        bugs = _get(f"bug/{CLOUSEAU_ALIAS}", {"include_fields": "id"}).get("bugs", [])
        return bugs[0]["id"] if bugs else None
    except Exception as e:
        log.error("could not resolve the '%s' alias bug: %s", CLOUSEAU_ALIAS, e)
        return None


def blocking_bugs(clouseau_id: int) -> list[dict]:
    try:
        return _get(
            "bug",
            {
                "blocks": clouseau_id,
                "include_fields": "id,regressed_by,cf_crash_signature,status,resolution",
                "order": "bug_id DESC",
                "limit": 1000,
            },
        ).get("bugs", [])
    except Exception as e:
        log.error("could not list bugs blocking %s: %s", clouseau_id, e)
        return []


def landed_nodes(regressor_bug: int) -> list[str]:
    """Scrape hg revs from a regressor bug's comments (short 12-char form)."""
    try:
        data = _get(f"bug/{regressor_bug}/comment", {})
    except Exception as e:
        log.warning("could not fetch comments for bug %s: %s", regressor_bug, e)
        return []
    comments = (
        data.get("bugs", {}).get(str(regressor_bug), {}).get("comments", [])
    )
    seen, nodes = set(), []
    for c in comments:
        for m in _HG_REV_RE.finditer(c.get("text", "")):
            short = m.group(1)[:12]
            if short not in seen:
                seen.add(short)
                nodes.append(short)
    return nodes


def _first_signature(cf_crash_signature: str) -> str:
    m = _SIG_RE.search(cf_crash_signature or "")
    return m.group(1) if m else (cf_crash_signature or "").strip()


_GH_COMMIT_RE = re.compile(r"github\.com/([\w-]+/[\w-]+)/commit/([0-9a-f]{7,40})")


def _resolve_uuid(signature: str, since: str, product: str = "Firefox") -> str | None:
    """Freeze a representative crash uuid for a signature (token-aware SuperSearch)."""
    try:
        from libmozdata import socorro

        res: dict = {}
        socorro.SuperSearch(
            params={"signature": "=" + signature, "product": product,
                    "date": [">=" + since], "_columns": ["uuid"], "_results_number": 1},
            handler=lambda j, d: d.update(j), handlerdata=res,
        ).wait()
        hits = res.get("hits", [])
        return hits[0]["uuid"] if hits else None
    except Exception as e:
        log.warning("uuid resolve failed for %r: %s", (signature or "")[:40], e)
        return None


def landed_git_nodes(regressor_bug: int) -> list[str]:
    """Git commit hashes for the regressor's landing (github firefox links)."""
    try:
        data = _get(f"bug/{regressor_bug}/comment", {})
    except Exception as e:
        log.warning("comments fetch failed for %s: %s", regressor_bug, e)
        return []
    comments = data.get("bugs", {}).get(str(regressor_bug), {}).get("comments", [])
    seen, nodes = set(), []
    for c in comments:
        for repo, h in _GH_COMMIT_RE.findall(c.get("text", "")):
            if repo.endswith("/firefox") and h not in seen:
                seen.add(h)
                nodes.append(h)
    return nodes


# C/C++ AND in-tree Rust -- searchfox has call-graph data for both.
_NATIVE_EXT = (".cpp", ".cc", ".cxx", ".c", ".h", ".hh", ".hpp", ".mm", ".rs")


def _fetch_patch(repo: str, h: str) -> str | None:
    """A commit's git-format patch from GitHub (message header + diff in one GET)."""
    try:
        r = requests.get(f"https://github.com/{repo}/commit/{h}.patch",
                         timeout=60, headers={"User-Agent": "clouseau-spike"})
    except requests.RequestException:
        return None
    return r.text if r.status_code == 200 else None


def _is_test_only(files: list[str]) -> bool:
    if not files:
        return True

    def is_test(f: str) -> bool:
        base = f.rsplit("/", 1)[-1]
        return ("/test" in f or base.startswith(("test_", "browser_"))
                or f.endswith((".ini", ".toml", ".list")))

    return all(is_test(f) for f in files)


def select_regressor_commits(regressor_bug: int, max_candidates: int = 6):
    """Pick the regressor bug's *real* landing commit(s). Returns (nodes, is_cpp).

    A regressor bug's comments cite many commits (fix, tests, follow-ups, backouts,
    other bugs). Keep only commits whose message says ``Bug <regressor_bug>``, that
    aren't backouts, and aren't test-only -- this is the fix for the naive
    "first commit" picking wrong/test/JS commits.
    """
    try:
        data = _get(f"bug/{regressor_bug}/comment", {})
    except Exception as e:
        log.warning("comments fetch failed for %s: %s", regressor_bug, e)
        return [], False
    comments = data.get("bugs", {}).get(str(regressor_bug), {}).get("comments", [])
    seen: list = []
    for c in comments:
        for repo, h in _GH_COMMIT_RE.findall(c.get("text", "")):
            if repo.endswith("/firefox") and (repo, h) not in seen:
                seen.append((repo, h))
    chosen, is_cpp = [], False
    for repo, h in seen[:max_candidates]:
        patch = _fetch_patch(repo, h)
        if not patch:
            continue
        head = patch.split("diff --git", 1)[0]
        if f"Bug {regressor_bug}" not in head:          # not this bug's landing
            continue
        if re.search(r"[Bb]ack(ed)?[ -]?out", head):    # skip backouts
            continue
        files = re.findall(r"^\+\+\+\s+(?:b/)?(?!/dev/null)(\S+)", patch, re.MULTILINE)
        if _is_test_only(files):                         # skip test-only commits
            continue
        chosen.append(h)
        if any(f.endswith(_NATIVE_EXT) for f in files):  # C/C++ or in-tree Rust
            is_cpp = True
    return chosen, is_cpp


def mine_recent_crash_regressions(
    since: str = "2025-10-01",
    limit: int = 40,
    socorro_product: str = "Firefox",
    bmo_products: tuple = ("Core", "Firefox", "Toolkit"),
) -> list[dict]:
    """Recent Firefox crash regressions with a retrievable crash + git regressor.

    Ground truth for the Phase-0 recall spike that actually EXISTS (unlike the
    aged-out clouseau corpus): bugs with cf_crash_signature + regressed_by, recent
    enough that Socorro still serves the crash. Freezes a uuid at harvest and takes
    the regressor's github landing commit.

    NOTE: most *native* crashes are filed under BMO product **Core** (Firefox the
    BMO product is mostly frontend), so the bug query spans Core+Firefox+Toolkit;
    the Socorro side stays the shipping app (Firefox).
    """
    params = {
        "f1": "cf_crash_signature", "o1": "isnotempty",
        "f2": "regressed_by", "o2": "isnotempty",
        "f3": "creation_ts", "o3": "greaterthan", "v3": since,
        "product": list(bmo_products),
        "include_fields": "id,cf_crash_signature,regressed_by,creation_time,product",
        "order": "bug_id DESC", "limit": 1000,
    }
    try:
        bugs = _get("bug", params).get("bugs", [])
    except Exception as e:
        log.error("recent crash-regression query failed: %s", e)
        return []
    log.info("%d recent crash-regression bugs since %s (BMO products: %s)",
             len(bugs), since, ",".join(bmo_products))
    out: list[dict] = []
    for b in bugs:
        if len(out) >= limit:
            break
        regs = b.get("regressed_by") or []
        if not regs:
            continue
        sig = _first_signature(b.get("cf_crash_signature", ""))
        if not re.match(r"^[\w:]", sig or ""):  # skip junk/HTML signatures
            continue
        since_b = (b.get("creation_time") or since)[:10]
        uuid = _resolve_uuid(sig, since_b, socorro_product)
        if not uuid:  # crash no longer retrievable -> unusable
            continue
        nodes, is_cpp = select_regressor_commits(regs[0])
        if not nodes or not is_cpp:  # need this bug's C/C++ landing commit(s)
            continue
        out.append({
            "clouseau_bug": b["id"],  # the crash bug (key kept for run_spike)
            "bmo_product": b.get("product"),
            "regressor_bug": regs[0],
            "regressor_nodes": nodes[:3],
            "vcs": "git",
            "cpp": True,  # native: C/C++ or in-tree Rust
            "signature": sig,
            "channel": "nightly",
            "uuid": uuid,
            "build_window": {"start": "", "end": ""},
            "off_stack": None,
            "notes": "AUTO-MINED recent native (C/C++/Rust) crash-regression; uuid frozen, "
                     "regressor = this-bug native landing commit. CURATE: verify off_stack.",
        })
        log.info("  [%d/%d] crash bug %s | regr %s | %s", len(out), limit, b["id"], regs[0], nodes[0])
    return out


def build_corpus(limit: int | None = None) -> list[dict]:
    cid = clouseau_bug_id()
    if not cid:
        return []
    log.info("clouseau alias bug = %s", cid)
    bugs = blocking_bugs(cid)
    log.info("%d bugs block it", len(bugs))
    # Most recent first, and only bugs that carry a regressor: older bugs predate
    # the regressed_by field, and a case with no regressor is unusable here.
    bugs = sorted(bugs, key=lambda b: b.get("id", 0), reverse=True)
    bugs = [b for b in bugs if b.get("regressed_by")]
    log.info("%d have regressed_by (usable)", len(bugs))
    if limit:
        bugs = bugs[:limit]

    out = []
    for b in bugs:
        regs = b.get("regressed_by") or []
        regressor_bug = regs[0] if regs else None
        out.append(
            {
                "clouseau_bug": b["id"],
                "regressor_bug": regressor_bug,
                "regressor_nodes": landed_nodes(regressor_bug) if regressor_bug else [],
                "signature": _first_signature(b.get("cf_crash_signature", "")),
                "channel": "nightly",
                "uuid": "",
                "build_window": {"start": "", "end": ""},
                "off_stack": None,
                "notes": (
                    "AUTO-MINED, UNCURATED: confirm off_stack (regressor function "
                    "absent from the crash's on-stack frames), add a representative "
                    "nightly uuid, and set build_window. Drop if regressed_by is "
                    "empty or the signature is hidden (security bug)."
                ),
            }
        )
    return out


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Mine a starting Phase-0 corpus from BMO")
    ap.add_argument("--source", choices=["recent", "clouseau"], default="recent",
                    help="'recent' = recent crash-regressions (runnable); "
                         "'clouseau' = the aged-out clouseau-alias corpus")
    ap.add_argument("--since", default="2025-10-01", help="recent: earliest creation date")
    ap.add_argument("--out", default="spike/corpus.mined.json")
    ap.add_argument("--limit", type=int, default=None, help="max cases")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.source == "recent":
        corpus = mine_recent_crash_regressions(since=args.since, limit=args.limit or 40)
    else:
        corpus = build_corpus(args.limit)
    Path(args.out).write_text(json.dumps(corpus, indent=2))
    with_nodes = sum(1 for c in corpus if c["regressor_nodes"])
    log.info(
        "wrote %d cases to %s (%d with nodes). CURATE: verify off_stack, then save "
        "as spike/corpus.json.", len(corpus), args.out, with_nodes,
    )


if __name__ == "__main__":
    main()
