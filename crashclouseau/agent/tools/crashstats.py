# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Signature-POPULATION crash-stats tools (`mcp__crashstats__*`) for the spike investigator.

The blind second opinion's ``mcp__socorro__crash_stats`` answers one question about one
signature: how old is it and what are its top facets. A spike asks a different one -- WHAT
CHANGED between the reports before the spike and the reports in it -- and the two tools here are
the instruments that answered it by hand on the 2026-09 QuotaManager case: the annotation diff
(``facets`` split at the spike build: ``quota_manager_shutdown_timeout`` named the operation in
56/59 spike crashes against 29/59 before) and the OTHER threads of a hang dump (``report`` with a
thread index: the IO thread was idle in 8/10 dumps, which is what turned "shutdown timed out"
into "blocked on ``mQuotaMutex``").

Read-only, signature-scoped (``facets``) or uuid-scoped (``report``), UA-safe through
libmozdata. Field names come from a whitelist so the model cannot turn this into an arbitrary
SuperSearch; Socorro's own rejection of a field it will not facet is returned as text rather
than raised, because a tool must never raise into the agent loop. This module imports no agent
framework.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated

from pydantic import Field

from libmozdata import socorro
from crashclouseau import inspector, utils
from crashclouseau.vendor.agent_tools.registry import tool, tools_in

# Facetable term fields a spike investigation has wanted so far. Unknown names are refused with
# the list, so the model learns the vocabulary from the error instead of guessing.
TERM_FIELDS = (
    "platform", "platform_pretty_version", "platform_version", "cpu_arch", "cpu_info",
    "cpu_microcode_version", "process_type", "release_channel", "version", "build_id",
    "moz_crash_reason", "reason", "address", "adapter_vendor_id", "adapter_device_id",
    "adapter_driver_version", "adapter_subsys_id", "app_init_dlls", "shutdown_progress",
    "shutdown_reason", "startup_crash", "useragent_locale", "dom_fission_enabled",
    "ipc_channel_error", "ipc_message_name", "ipc_shutdown_state",
    "quota_manager_shutdown_timeout", "async_shutdown_timeout", "gmp_plugin",
    "graphics_critical_error", "signature", "proto_signature", "topmost_filenames",
    "accessibility", "accessibility_client", "safe_mode", "background_task_name",
    "install_time", "uptime", "system_memory_use_percentage", "available_physical_memory",
    "available_virtual_memory", "total_physical_memory", "install_age", "oom_allocation_size",
)
# The numeric ones, which read better bucketed (``interval``) than as exact values.
NUMERIC_FIELDS = frozenset({
    "uptime", "system_memory_use_percentage", "available_physical_memory",
    "available_virtual_memory", "total_physical_memory", "install_age", "oom_allocation_size",
    "install_time",
})
_FACETS_SIZE = 20
_MAX_DAYS = 364
# Processed-crash keys ``report`` prints; everything else in the payload is either huge
# (``json_dump``, printed separately and bounded) or protected and absent.
_REPORT_KEYS = (
    "product", "version", "build", "release_channel", "os_pretty_version", "os_name",
    "cpu_arch", "cpu_info", "cpu_microcode_version", "process_type", "report_type", "reason",
    "address", "moz_crash_reason", "signature", "date_processed", "uptime", "install_age",
    "startup_crash", "system_memory_use_percentage", "available_physical_memory",
    "total_physical_memory", "shutdown_progress", "shutdown_reason", "async_shutdown_timeout",
    "quota_manager_shutdown_timeout", "xpcom_spin_event_loop_stack", "ipc_channel_error",
    "ipc_message_name", "ipc_shutdown_state", "adapter_vendor_id", "adapter_device_id",
    "adapter_driver_version", "accessibility", "accessibility_client", "app_init_dlls",
    "dom_fission_enabled", "gmp_plugin", "graphics_critical_error",
)
_VALUE_CAP = 600
_MAX_THREADS_LISTED = 80
_MAX_FRAMES = 60


@dataclass
class CrashStatsCtx:
    """The spike's own product/channel, so every facet query is scoped the way the selector's
    numbers were (``utils.get_search_channel`` widens beta to beta+aurora)."""

    product: str = "Firefox"
    channel: str = "nightly"


def _search(params: dict) -> dict:
    got: dict = {}

    def handler(json_, data):
        data["result"] = json_

    socorro.SuperSearch(params=params, handler=handler, handlerdata=got).wait()
    return got.get("result") or {}


def _short(value, cap=_VALUE_CAP) -> str:
    text = str(value)
    return text if len(text) <= cap else text[:cap] + "... [truncated]"


def _facet_rows(result: dict, field: str, interval) -> tuple[int, list[tuple[str, int]]]:
    total = int(result.get("total") or 0)
    facets = result.get("facets") or {}
    if interval:
        rows = facets.get("histogram_{}".format(field)) or []
        return total, [(str(r.get("term")), int(r.get("count") or 0)) for r in rows]
    rows = facets.get(field) or []
    return total, [(str(r.get("term")), int(r.get("count") or 0)) for r in rows]


def _render(label: str, total: int, rows, field: str) -> list[str]:
    out = ["{}: {} crashes".format(label, total)]
    if not rows:
        out.append("  (no {} values)".format(field))
        return out
    for term, count in rows:
        share = " ({:.0f}%)".format(100.0 * count / total) if total else ""
        out.append("  {}: {}{}".format(_short(term, 160), count, share))
    return out


def _facet_params(ctx: CrashStatsCtx, signature: str, field: str, since: str, interval,
                  build_lo=None, build_hi=None) -> dict:
    params = {
        "signature": "=" + signature,
        "product": ctx.product,
        "date": ">=" + since,
        "_results_number": 0,
    }
    if ctx.channel:
        params["release_channel"] = utils.get_search_channel(ctx.channel)
    build = []
    if build_lo:
        build.append(">=" + str(build_lo))
    if build_hi:
        build.append("<" + str(build_hi))
    if build:
        params["build_id"] = build
    if interval:
        params["_histogram.{}".format(field)] = "product"
        params["_histogram_interval.{}".format(field)] = interval
    else:
        params["_facets"] = field
        params["_facets_size"] = _FACETS_SIZE
    return params


@tool
async def facets(
    ctx: CrashStatsCtx,
    signature: Annotated[str, Field(description="The exact crash signature.")],
    field: Annotated[str, Field(
        description="The crash-report field to break the population down by, e.g. "
                    "platform_pretty_version, process_type, version, build_id, "
                    "moz_crash_reason, shutdown_progress, quota_manager_shutdown_timeout, "
                    "async_shutdown_timeout, adapter_driver_version, cpu_info, uptime, "
                    "system_memory_use_percentage. Unknown names are refused with the list.")],
    days: Annotated[int, Field(
        description="Look-back window in days for the counts (default 14, max 364).")] = 14,
    split_at_build: Annotated[str, Field(
        description="A 14-digit buildid. When given, the breakdown is computed TWICE -- reports "
                    "on builds BEFORE it and reports on builds FROM it on -- so the two "
                    "distributions can be compared. This is the annotation-diff that finds what "
                    "changed in a spike: pass the first spiking build.")] = "",
    interval: Annotated[str, Field(
        description="For a NUMERIC field only (uptime, system_memory_use_percentage, "
                    "available_physical_memory, ...): the bucket width, e.g. 60 for uptime in "
                    "seconds, 10 for a percentage. Ignored for term fields.")] = "",
) -> str:
    """Break this signature's recent crash reports down by one field (top values with counts
    and shares), optionally SPLIT at a build so the reports before the spike and the reports in
    it can be compared side by side. Use it to find what the spiking population has in common
    that the earlier one did not: an OS version, a driver, a process type, a version, a
    shutdown phase, a memory state, an annotation value. Read-only; scoped to this signature on
    the crash's own product and channel."""
    field = (field or "").strip()
    if field not in TERM_FIELDS:
        return ("facets: {!r} is not a field this tool will facet. Known fields: {}".format(
            field, ", ".join(TERM_FIELDS)))
    days = max(1, min(int(days or 14), _MAX_DAYS))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    interval = (interval or "").strip() if field in NUMERIC_FIELDS else ""
    split = (split_at_build or "").strip()
    if split and not (split.isdigit() and len(split) == 14):
        return "facets: split_at_build must be a 14-digit buildid, got {!r}.".format(split)
    head = "crash-stats facets for [@ {}] by {} (product {}, channel {}, last {}d{})".format(
        signature, field, ctx.product, ctx.channel or "any", days,
        ", buckets of {}".format(interval) if interval else "")
    try:
        if split:
            before = await asyncio.to_thread(
                _search, _facet_params(ctx, signature, field, since, interval, build_hi=split))
            during = await asyncio.to_thread(
                _search, _facet_params(ctx, signature, field, since, interval, build_lo=split))
            lines = [head]
            total, rows = _facet_rows(before, field, interval)
            lines += _render("BEFORE build {} (older builds)".format(split), total, rows, field)
            total, rows = _facet_rows(during, field, interval)
            lines += _render("FROM build {} on".format(split), total, rows, field)
            return "\n".join(lines)
        result = await asyncio.to_thread(
            _search, _facet_params(ctx, signature, field, since, interval))
    except Exception as exc:  # noqa: BLE001 - a tool must not raise into the agent loop
        return "facets: lookup failed for {!r} by {} ({}: {}).".format(
            signature, field, type(exc).__name__, exc)
    if result.get("errors"):
        return "facets: Socorro refused the query: {}".format(_short(result["errors"], 400))
    total, rows = _facet_rows(result, field, interval)
    return "\n".join([head] + _render("all reports", total, rows, field))


def _frames_text(frames, max_frames) -> list[str]:
    out = []
    for i, fr in enumerate((frames or [])[:max_frames]):
        fn = fr.get("function") or fr.get("normalized") or "?"
        loc = ""
        uri = fr.get("file")
        if uri:
            path, _node = inspector.get_path_node(uri)
            loc = path or uri
            line = fr.get("line")
            if loc and line:
                loc = "{}:{}".format(loc, line)
        module = fr.get("module") or ""
        parts = ["#{}".format(i), _short(fn, 160)]
        if loc:
            parts.append(_short(loc, 160))
        if module:
            parts.append("[{}]".format(module))
        out.append("  " + "  ".join(parts))
    if frames and len(frames) > max_frames:
        out.append("  ... {} more frames".format(len(frames) - max_frames))
    return out


@tool
async def report(
    ctx: CrashStatsCtx,
    uuid: Annotated[str, Field(description="The crash report's uuid.")],
    thread: Annotated[int, Field(
        description="Index of the thread whose stack to print (default -1 = the thread the "
                    "signature describes: the crashing thread, or on a hang the hung main "
                    "thread rather than the watchdog). The thread list in the output gives the "
                    "indexes; on a hang, read the thread that owns the awaited work, e.g. the "
                    "QuotaManager IO thread or a thread pool worker.")] = -1,
    max_frames: Annotated[int, Field(description="Frames to print (default 40, max 60).")] = 40,
) -> str:
    """One processed crash report from crash-stats: the report-level annotations (OS, CPU,
    process type, reason, MOZ_CRASH reason, uptime, memory state, shutdown phase, shutdown-timeout
    annotations, ...), the list of threads in the minidump, and the stack of ONE thread -- by
    default the thread the signature describes, or the thread you ask for. Use it to read other
    threads of a hang (what was the awaited thread doing?), to compare reports from different
    machines, or to see annotations the summary did not carry. Read-only."""
    try:
        raw = await asyncio.to_thread(inspector.get_crash_data, uuid)
    except Exception as exc:  # noqa: BLE001
        return "report: could not fetch {} ({}: {}).".format(uuid, type(exc).__name__, exc)
    if not isinstance(raw, dict) or not raw:
        return "report: no processed crash for {}.".format(uuid)
    lines = ["crash report {}".format(uuid)]
    for key in _REPORT_KEYS:
        value = raw.get(key)
        if value in (None, "", [], {}):
            continue
        lines.append("{}: {}".format(key, _short(value)))
    dump = raw.get("json_dump") or {}
    info = dump.get("crash_info") or {}
    if info:
        for key in ("type", "address", "instruction", "crashing_thread"):
            if info.get(key) not in (None, ""):
                lines.append("crash_info.{}: {}".format(key, _short(info[key], 200)))
    threads = dump.get("threads") or []
    if not threads:
        lines.append("(no thread list in the minidump)")
        return "\n".join(lines)
    chosen = thread if isinstance(thread, int) and 0 <= thread < len(threads) else None
    if chosen is None:
        default = inspector.thread_for_analysis(raw)
        chosen = default if isinstance(default, int) and 0 <= default < len(threads) else 0
    lines.append("threads ({}):".format(len(threads)))
    for i, t in enumerate(threads[:_MAX_THREADS_LISTED]):
        name = t.get("thread_name") or ""
        mark = "  <- printed below" if i == chosen else ""
        lines.append("  {}: {} ({} frames){}".format(
            i, _short(name, 80) or "(unnamed)", len(t.get("frames") or []), mark))
    if len(threads) > _MAX_THREADS_LISTED:
        lines.append("  ... {} more threads".format(len(threads) - _MAX_THREADS_LISTED))
    t = threads[chosen]
    lines.append("thread {} ({}) stack:".format(chosen, t.get("thread_name") or "unnamed"))
    lines += _frames_text(t.get("frames"), max(1, min(int(max_frames or 40), _MAX_FRAMES)))
    return "\n".join(lines)


TOOLS = tools_in(__name__)
