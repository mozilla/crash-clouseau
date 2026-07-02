"""Fetch a processed crash and extract its crashing-thread frames (Phase-0 spike).

Fetches via libmozdata's ``ProcessedCrash`` -- the SAME source crash-clouseau
already uses (``crashclouseau.inspector.get_crash_data`` is just
``ProcessedCrash.get_processed(uuid)[uuid]``). We reuse the existing mechanism
rather than shell out to socorro-cli, which would only re-fetch the same
crash-stats JSON behind a Rust binary with no added capability. Frame extraction
mirrors ``inspector.inspect_stacktrace`` but WITHOUT the production build-node
guard or the per-frame lando ``git2hg`` lookups -- the spike only needs the
on-stack function names to seed searchfox, not the changeset mapping.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("spike.crash")

MAX_FRAMES = 50


@dataclass
class Frame:
    stackpos: int
    function: str
    filename: str
    line: int
    module: str


def fetch_processed(uuid: str) -> dict | None:
    """Processed-crash dict for ``uuid`` via libmozdata's ProcessedCrash.

    Same source as ``crashclouseau.inspector.get_crash_data`` -- the spike reads
    crashes exactly the way the app does, with no extra dependency."""
    try:
        from libmozdata import socorro
    except ImportError:
        log.error("libmozdata not importable; `pip install libmozdata>=0.2.12`")
        return None
    try:
        data = socorro.ProcessedCrash.get_processed(uuid)
        return data.get(uuid)
    except Exception as e:  # network / unknown-uuid
        log.warning("ProcessedCrash.get_processed failed for %s: %s", uuid, e)
        return None


def _crashing_thread_index(data: dict, dump: dict):
    ci = dump.get("crash_info") or {}
    for cand in (
        ci.get("crashing_thread"),
        data.get("crashing_thread"),
        dump.get("crashing_thread"),
    ):
        if cand is not None:
            return cand
    return None


def _path_from_uri(uri: str) -> str:
    # "hg:host:PATH:node" or "git:host:PATH:hash" -> PATH  (best-effort; no git2hg).
    if not uri:
        return ""
    if uri.startswith(("hg:", "git:")):
        parts = uri.split(":")
        if len(parts) >= 4:
            return parts[2]
    return uri


def crashing_frames(data: dict, max_frames: int = MAX_FRAMES) -> list[Frame]:
    """Frames of the crashing thread (function/file/line/module), capped."""
    dump = data.get("json_dump") or {}
    threads = dump.get("threads")
    if not threads:
        log.warning("crash data has no json_dump.threads")
        return []
    n = _crashing_thread_index(data, dump)
    if n is None or n >= len(threads):
        log.warning("no valid crashing_thread index (%r)", n)
        return []
    raw = threads[n].get("frames", [])[:max_frames]
    return [
        Frame(
            stackpos=i,
            function=f.get("function", "") or "",
            filename=_path_from_uri(f.get("file", "")),
            line=f.get("line", -1),
            module=f.get("module", "") or "",
        )
        for i, f in enumerate(raw)
    ]


def frame_functions(frames: list[Frame]) -> set[str]:
    return {f.function for f in frames if f.function}


def frame_files(frames: list[Frame]) -> set[str]:
    return {f.filename for f in frames if f.filename}


def brief(data: dict, frames: list[Frame], max_frames: int = 15) -> str:
    """A compact crash brief for the LLM explorer (signature + top frames)."""
    sig = data.get("signature", "?")
    ci = (data.get("json_dump") or {}).get("crash_info") or {}
    reason = ci.get("type") or data.get("reason") or "?"
    address = ci.get("address") or "?"
    mcr = data.get("moz_crash_reason") or ""
    lines = [
        f"signature: {sig}",
        f"crash type/reason: {reason}",
        f"faulting address: {address}",
    ]
    if mcr:
        lines.append(f"MOZ_CRASH: {mcr}")
    lines.append("top crashing-thread frames (stackpos: function [file:line]):")
    for f in frames[:max_frames]:
        loc = f"{f.filename}:{f.line}" if f.filename else (f.module or "?")
        lines.append(f"  {f.stackpos}: {f.function or '(no symbol)'} [{loc}]")
    return "\n".join(lines)


if __name__ == "__main__":  # usage: python -m spike.crash <uuid>
    import sys

    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m spike.crash <crash-uuid>")
    d = fetch_processed(sys.argv[1])
    if not d:
        raise SystemExit("could not fetch crash")
    fr = crashing_frames(d)
    print(brief(d, fr))
    print(f"\n{len(fr)} frames; {len(frame_functions(fr))} distinct frame functions")
