#!/usr/bin/env python
"""Rebuild and recompute the panel behind ``triage._render_spin_stack`` and
``triage._render_async_shutdown`` (the two per-field crash-fact renderers).

READ-ONLY on the repo: it fetches PUBLIC Socorro SuperSearch, runs the SHIPPED renderers over
what comes back, and prints every number quoted in those two docstrings, so a reviewer
recomputes them instead of trusting them.

    # 1. rebuild the raw panel (~14k reports, 10 weeks, Firefox nightly) -- a few minutes
    uv run python spike/crash_fact_renderers_panel.py --fetch --panel /tmp/cfr_panel
    # 2. recompute, and diff against the committed artifact
    uv run python spike/crash_fact_renderers_panel.py --panel /tmp/cfr_panel \\
        --check spike/CRASH_FACT_RENDERERS_PANEL.json

SuperSearch is a faithful stand-in for what ``inspector.get_crash_data`` hands ``_crash_facts``:
the values were compared against ``/api/ProcessedCrash/`` for the 3 longest and 2 median values
of each field, 10/10 byte-identical. ``phc_kind`` / ``phc_alloc_stack`` / ``phc_free_stack``
CANNOT be measured this way -- Socorro marks them ``view_pii`` and silently drops both the
column and the filter, so a query for them returns the whole nightly population.
"""
import argparse
import datetime
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))

BASE = "https://crash-stats.mozilla.org/api/SuperSearch/"
UA = {"User-Agent": "crash-clouseau"}
WINDOWS = [("2026-06-12", "2026-06-26"), ("2026-06-26", "2026-07-10"),
           ("2026-07-10", "2026-07-24"), ("2026-07-24", "2026-08-07"),
           ("2026-08-07", "2026-08-21")]
CONTROL_START, CONTROL_DAYS, CONTROL_PER_DAY = "2026-08-07", 14, 200
CONTROL_COLS = ["moz_crash_reason", "reason", "cpu_info", "adapter_driver_version",
                "platform_pretty_version", "address", "shutdown_progress", "shutdown_reason",
                "xpcom_spin_event_loop_stack", "async_shutdown_timeout", "ipc_shutdown_state",
                "last_error_value"]


def _get(params):
    url = BASE + "?" + urllib.parse.urlencode(params)
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=180))


def _pull(extra, cols, windows):
    out = []
    for a, b in windows:
        off = 0
        while True:
            params = [("product", "Firefox"), ("release_channel", "nightly"),
                      ("date", ">=" + a), ("date", "<" + b), ("_results_number", "1000"),
                      ("_results_offset", str(off)), ("_facets_size", "0")] + extra
            params += [("_columns", c) for c in cols]
            data = _get(params)
            out += data["hits"]
            print("  %s..%s %d/%d" % (a, b, len(out), data["total"]), file=sys.stderr)
            if off + 1000 >= data["total"] or not data["hits"]:
                break
            off += 1000
            time.sleep(0.5)
    return out


def fetch(panel):
    os.makedirs(panel, exist_ok=True)
    json.dump(_pull([("xpcom_spin_event_loop_stack", "!__null__")],
                    ["uuid", "signature", "xpcom_spin_event_loop_stack"], WINDOWS),
              open(os.path.join(panel, "spin.json"), "w"))
    json.dump(_pull([("async_shutdown_timeout", "!__null__")],
                    ["uuid", "signature", "async_shutdown_timeout"], WINDOWS),
              open(os.path.join(panel, "ast.json"), "w"))
    hits = []
    first = datetime.date(*[int(x) for x in CONTROL_START.split("-")])
    for i in range(CONTROL_DAYS):
        a, b = first + datetime.timedelta(days=i), first + datetime.timedelta(days=i + 1)
        params = [("product", "Firefox"), ("release_channel", "nightly"),
                  ("date", ">=" + a.isoformat()), ("date", "<" + b.isoformat()),
                  ("_results_number", str(CONTROL_PER_DAY)), ("_facets_size", "0")]
        params += [("_columns", c) for c in ["uuid"] + CONTROL_COLS]
        hits += _get(params)["hits"]
        print("  control %s %d" % (a, len(hits)), file=sys.stderr)
        time.sleep(0.4)
    json.dump(hits, open(os.path.join(panel, "control.json"), "w"))


def _load(panel):
    """Reads either this script's own layout or the original measurement's file names."""
    def rd(name, *legacy):
        path = os.path.join(panel, name)
        if os.path.exists(path):
            return json.load(open(path))
        rows = []
        for other in legacy:
            other = os.path.join(panel, other)
            if not os.path.exists(other):
                raise SystemExit("missing %s in %s -- run with --fetch" % (name, panel))
            rows += json.load(open(other))
        return rows
    return (rd("spin.json", "spin_8wk_prior.json", "spin_all.json"),
            rd("ast.json", "ast_8wk_prior.json", "asyncshutdown.json"),
            rd("control.json", "allfields.json"))


def pct(xs, p):
    xs = sorted(xs)
    k = (len(xs) - 1) * p / 100.0
    f, c = math.floor(k), math.ceil(k)
    return xs[int(k)] if f == c else xs[f] + (xs[c] - xs[f]) * (k - f)


def slen(value):
    return len(str(value).replace("\n", " ").strip())


def subsystem(entry):
    return re.sub(r"\s*#\d+\s*$", "", entry).strip()


def tail_preserving(value, limit=300):
    """THE OBVIOUS FIX THIS PANEL KILLS: keep the head AND the tail of the spin stack."""
    text = str(value).replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    head = (limit - 3) // 2
    return text[:head] + "..." + text[-(limit - 3 - head):]


def collapse(value):
    """The collapse half of `_render_spin_stack`, so the cap can be swept on its own."""
    text = str(value).replace("\n", " ").strip()
    counts, order = {}, []
    for entry in (part.strip() for part in text.split("|")):
        if entry in counts:
            counts[entry] += 1
        else:
            counts[entry] = 1
            order.append(entry)
    return "|".join(e + (" (x%d)" % counts[e] if counts[e] > 1 else "") for e in order)


def parses(raw):
    try:
        json.loads(raw)
        return True
    except Exception:
        return False


def files_of(raw):
    """Every source path the value carries: blocker `filename`s and `file:function:line`
    frames. This is what the 300-char head destroys and the only thing a regressor hunt
    can act on."""
    try:
        payload = json.loads(raw)
    except Exception:
        return set()
    files = set()
    for cond in (payload.get("conditions") or []):
        if not isinstance(cond, dict):
            continue
        if cond.get("filename"):
            files.add(cond["filename"])
        stack = cond.get("stack") or []
        if isinstance(stack, str):
            stack = [stack]
        for frame in stack:
            files.add(str(frame).rsplit(":", 2)[0])
    return files


def state_keys(raw, dicts_only=True):
    """The blocker `state`s a rendering has to preserve.

    `dicts_only` is the ORIGINAL denominator and it is a trap: `state` is a dict on 4,118 of the
    panel's conditions but a bare string on 925 and a list on 158, so scoring dicts only hides
    whatever a renderer does to the other 1,083. Both numbers are reported."""
    try:
        payload = json.loads(raw)
    except Exception:
        return []
    out = []
    for cond in (payload.get("conditions") or []):
        if not isinstance(cond, dict):
            continue
        state = cond.get("state")
        if isinstance(state, dict):
            out.append(json.dumps(state, separators=(",", ":")))
        elif not dicts_only and state not in (None, "", [], {}):
            out.append(state if isinstance(state, str)
                       else json.dumps(state, separators=(",", ":")))
    return out


def mis_ordered(render, spin):
    """Values whose rendering ENDS on an entry that was not the source's innermost.

    Not the same question as `innermost_lost`, which asks only whether the name appears
    ANYWHERE in the output -- a containment test cannot see a re-ordering, and a collapse that
    keys on first occurrence re-orders. On a field whose prompt label is "innermost last ...
    treat it as the primary lead", ending on the wrong entry is the failure mode. Only values
    the renderer did NOT truncate are counted, since a truncated tail is `innermost_lost`'s
    business."""
    bad = []
    for uuid, value in spin.items():
        text = render(value)
        if text.endswith("..."):
            continue
        entries = [e.strip() for e in str(value).replace("\n", " ").strip().split("|")]
        if re.sub(r" \(x\d+\)$", "", text.split("|")[-1]) != entries[-1]:
            bad.append(uuid)
    return bad


def spin_section(triage, spin):
    lens = [slen(v) for v in spin.values()]
    over = {u: v for u, v in spin.items() if slen(v) > 300}
    at_cap = [v for v in spin.values() if len(v) == 10000]
    distinct_at_cap = sorted(len(set(e.strip() for e in v.split("|"))) for v in at_cap)
    section = {
        "reports": len(spin),
        "distinct_values": len(set(spin.values())),
        "raw_len": {"p50": pct(lens, 50), "p90": pct(lens, 90), "p99": pct(lens, 99),
                    "max": max(lens)},
        "over_300": len(over),
        "over_300_distinct_values": len(set(over.values())),
        "literal_INNERMOST_in_the_worklist_item": sum(1 for v in spin.values()
                                                      if "INNERMOST" in v),
        "non_adjacent_repeat": sum(
            1 for v in spin.values()
            for ents in [[e.strip() for e in v.split("|")]]
            if any(ents[i] in ents[:i] and ents[i] != ents[i - 1]
                   for i in range(1, len(ents)))),
        "at_socorro_10000_char_annotation_cap": {
            "n": len(at_cap),
            "distinct_entries_median": distinct_at_cap[len(distinct_at_cap) // 2],
            "distinct_entries_max": distinct_at_cap[-1]},
    }
    base = [len(triage._short_value(v)) for v in spin.values()]
    variants = []
    for name, render in [
            ("TODAY head-300", lambda v: triage._short_value(v)),
            ("KILLED tail-preserving 300", tail_preserving),
            ("collapse then 300", lambda v: triage._short_value(collapse(v), 300)),
            ("SHIPPED collapse then %d" % triage._SPIN_STACK_LIMIT, triage._render_spin_stack),
            ("collapse then 500", lambda v: triage._short_value(collapse(v), 500)),
            ("collapse then 600", lambda v: triage._short_value(collapse(v), 600)),
            ("collapse then 800", lambda v: triage._short_value(collapse(v), 800))]:
        innermost = anysub = newsub = 0
        named = {}
        for uuid, value in over.items():
            text = render(value)
            entries = [e.strip() for e in value.split("|") if e.strip()]
            subs = list(dict.fromkeys(subsystem(e) for e in entries))
            innermost += subsystem(entries[-1]) not in text
            miss = [s for s in subs if s not in text]
            anysub += bool(miss)
            new = [s for s in miss if s.split(",")[0].strip() not in text]
            if new:
                newsub += 1
                named[uuid] = new
        sizes = [len(render(v)) for v in spin.values()]
        delta = [a - b for a, b in zip(sizes, base)]
        variants.append({"variant": name, "innermost_lost": innermost,
                         "any_subsystem_lost": anysub, "new_subsystem_lost": newsub,
                         "mis_ordered_innermost": len(mis_ordered(render, spin)),
                         "added_bytes": {"p50": pct(delta, 50), "p90": pct(delta, 90),
                                         "max": max(delta),
                                         "mean": round(sum(delta) / len(delta), 2)}})
        if name.startswith("TODAY"):
            section["new_subsystems_lost_today"] = named
    section["variants"] = variants
    return section


def ast_score(triage, ast, base, render):
    kept = total = zero = trunc = skept = stotal = akept = atotal = 0
    for raw in ast.values():
        text = render(raw)
        if slen(raw) > 300:
            files = files_of(raw)
            hit = sum(1 for f in files if f in text)
            total += len(files)
            kept += hit
            trunc += 1
            zero += bool(files) and not hit
        flat = text.replace(" ", "")
        keys = state_keys(raw)
        if keys:
            stotal += len(keys)
            skept += sum(1 for k in keys if k.replace(" ", "") in flat)
        allkeys = state_keys(raw, dicts_only=False)
        if allkeys:
            atotal += len(allkeys)
            akept += sum(1 for k in allkeys if k.replace(" ", "") in flat)
    sizes = [len(render(v)) for v in ast.values()]
    delta = [a - b for a, b in zip(sizes, base)]
    return {"truncated_today": trunc, "files_kept": kept, "files_total": total,
            "files_kept_pct": round(100.0 * kept / total, 1),
            "reports_keeping_zero_file": zero,
            "state_keys_kept": skept, "state_keys_total": stotal,
            "state_keys_pct": round(100.0 * skept / stotal, 1),
            "all_state_keys_kept": akept, "all_state_keys_total": atotal,
            "all_state_keys_pct": round(100.0 * akept / atotal, 1),
            "added_bytes": {"p50": pct(delta, 50), "p90": pct(delta, 90), "max": max(delta),
                            "mean": round(sum(delta) / len(delta), 2)}}


def with_constant(triage, name, value):
    """Rebind one shipped constant for the duration of one render, so every constant in the
    module can be swept against the SHIPPED function rather than against a copy of it."""
    def render(raw):
        keep = getattr(triage, name)
        setattr(triage, name, value)
        try:
            return triage._render_async_shutdown(raw)
        finally:
            setattr(triage, name, keep)
    return render


def with_state_limit(triage, limit):
    return with_constant(triage, "_ASYNC_SHUTDOWN_STATE_LIMIT", limit)


def head_loss(triage, rows):
    """What today's head-300 keeps of a TRUNCATED value, on the same 10-week panel: condition
    names (which the signature carries anyway), blocker source files and frames."""
    got = dict(reports=0, names=0, names_head=0, names_in_signature=0, files=0, files_head=0,
               frames=0, frames_head=0)
    for row in rows:
        raw = str(row["async_shutdown_timeout"])
        if slen(raw) <= 300 or not parses(raw):
            continue
        payload = json.loads(raw)
        head = triage._short_value(raw)
        signature = row.get("signature") or ""
        conds = [c for c in (payload.get("conditions") or []) if isinstance(c, dict)]
        names = [c["name"] for c in conds if c.get("name")]
        frames = []
        for cond in conds:
            stack = cond.get("stack") or []
            if isinstance(stack, str):
                stack = [stack]
            frames += [str(f) for f in stack]
        files = files_of(raw)
        got["reports"] += 1
        got["names"] += len(names)
        got["names_head"] += sum(1 for n in names if n in head)
        got["names_in_signature"] += sum(1 for n in names if n in signature)
        got["files"] += len(files)
        got["files_head"] += sum(1 for f in files if f in head)
        got["frames"] += len(frames)
        got["frames_head"] += sum(1 for f in frames if f in head)
    return got


def ast_section(triage, ast):
    lens = [slen(v) for v in ast.values()]
    base = [len(triage._short_value(v)) for v in ast.values()]
    section = {
        "values": len(ast),
        "raw_len": {"p50": pct(lens, 50), "p90": pct(lens, 90), "p99": pct(lens, 99),
                    "max": max(lens)},
        "over_300": sum(1 for x in lens if x > 300),
        "parse_failures": sorted(u for u, v in ast.items() if not parses(v)),
        "variants": [],
    }
    for name, render in [
            ("TODAY head-300", lambda v: triage._short_value(v)),
            ("KILLED global cap 1000", lambda v: triage._short_value(v, 1000)),
            ("KILLED global cap 2000", lambda v: triage._short_value(v, 2000)),
            ("KILLED parse but drop state", with_state_limit(triage, 0)),
            ("SHIPPED parse, %d blockers / %d frames / state<=%d, cap %d"
             % (triage._ASYNC_SHUTDOWN_BLOCKERS, triage._ASYNC_SHUTDOWN_FRAMES,
                triage._ASYNC_SHUTDOWN_STATE_LIMIT, triage._ASYNC_SHUTDOWN_LIMIT),
             triage._render_async_shutdown)]:
        row = ast_score(triage, ast, base, render)
        row["variant"] = name
        section["variants"].append(row)
    # Every shipped constant gets a sweep: an unswept threshold is a guess, and three of these
    # four were unswept in the first cut of this panel.
    for key, name, values in (
            ("state_limit_sweep", "_ASYNC_SHUTDOWN_STATE_LIMIT", (0, 100, 120, 140, 160, 200)),
            ("outer_cap_sweep", "_ASYNC_SHUTDOWN_LIMIT",
             (500, 600, 700, 800, 850, 900, 1000, 1200, 10 ** 6)),
            ("blockers_sweep", "_ASYNC_SHUTDOWN_BLOCKERS", (1, 2, 3, 4, 5, 10, 10 ** 6)),
            ("frames_sweep", "_ASYNC_SHUTDOWN_FRAMES", (3, 4, 5, 6, 8, 12, 10 ** 6))):
        section[key] = []
        for value in values:
            row = ast_score(triage, ast, base, with_constant(triage, name, value))
            section[key].append(
                {name.lower().replace("_async_shutdown_", ""): value,
                 "files_kept": row["files_kept"], "files_kept_pct": row["files_kept_pct"],
                 "state_keys_pct": row["state_keys_pct"],
                 "all_state_keys_pct": row["all_state_keys_pct"],
                 "added_bytes_mean": row["added_bytes"]["mean"],
                 "added_bytes_p90": row["added_bytes"]["p90"],
                 "added_bytes_max": row["added_bytes"]["max"]})
    return section


def control_section(control):
    rows = []
    for col in CONTROL_COLS:
        vals = [str(h[col]) for h in control if h.get(col) not in (None, "")]
        if not vals:
            continue
        rows.append({"field": col, "present": len(vals), "max": max(slen(v) for v in vals),
                     "over_300": sum(1 for v in vals if slen(v) > 300)})
    return {"reports": len(control), "per_day": CONTROL_PER_DAY,
            "window": "%s +%dd" % (CONTROL_START, CONTROL_DAYS), "fields": rows}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--panel", default="/tmp/cfr_panel")
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--out")
    parser.add_argument("--check")
    args = parser.parse_args()
    if args.fetch:
        fetch(args.panel)

    os.environ.setdefault("DATABASE_URL", "sqlite://")
    os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
    from crashclouseau.agent import triage

    spin_rows, ast_rows, control = _load(args.panel)
    spin = {h["uuid"]: str(h["xpcom_spin_event_loop_stack"]) for h in spin_rows}
    ast = {h["uuid"]: str(h["async_shutdown_timeout"]) for h in ast_rows}
    out = {
        "what": ("the panel behind triage._render_spin_stack and "
                 "triage._render_async_shutdown"),
        "reproduce": ("uv run python spike/crash_fact_renderers_panel.py --fetch "
                      "--panel /tmp/cfr_panel"),
        "panel": {"source": "public Socorro SuperSearch, product=Firefox, channel=nightly",
                  "window": "%s..%s" % (WINDOWS[0][0], WINDOWS[-1][1]),
                  "not_measurable": ["phc_kind (view_pii)", "phc_alloc_stack (view_pii)",
                                     "phc_free_stack (view_pii)"]},
        "shipped_constants": {"_SPIN_STACK_LIMIT": triage._SPIN_STACK_LIMIT,
                              "_ASYNC_SHUTDOWN_BLOCKERS": triage._ASYNC_SHUTDOWN_BLOCKERS,
                              "_ASYNC_SHUTDOWN_FRAMES": triage._ASYNC_SHUTDOWN_FRAMES,
                              "_ASYNC_SHUTDOWN_STATE_LIMIT":
                                  triage._ASYNC_SHUTDOWN_STATE_LIMIT,
                              "_ASYNC_SHUTDOWN_LIMIT": triage._ASYNC_SHUTDOWN_LIMIT},
        "xpcom_spin_event_loop_stack": spin_section(triage, spin),
        "async_shutdown_timeout": ast_section(triage, ast),
        "what_the_head_300_keeps_of_a_truncated_async_value": head_loss(triage, ast_rows),
        "control_sample": control_section(control),
    }
    text = json.dumps(out, indent=1, sort_keys=True)
    if args.out:
        open(args.out, "w").write(text + "\n")
    print(text)
    if args.check:
        want = json.load(open(args.check))
        same = want == out
        print("CHECK vs %s: %s" % (args.check, "MATCH" if same else "DIFFER"), file=sys.stderr)
        return 0 if same else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
