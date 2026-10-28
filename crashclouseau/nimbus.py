# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Find live Nimbus recipes published before a signature's first report."""
import json
import re
from datetime import datetime, timedelta, timezone
from itertools import zip_longest

from libmozdata import socorro

from crashclouseau import config, hgedge, net
from crashclouseau.logger import logger

EXPERIMENTER_URL = "https://experimenter.services.mozilla.com/api/v6/experiments/"
SUMMARY_URL = "https://experimenter.services.mozilla.com/nimbus/{}/summary"
APPLICATIONS = {"Firefox": "firefox-desktop", "Fenix": "fenix"}
MANIFEST_PATH = "toolkit/components/nimbus/FeatureManifest.yaml"
_CACHE_PREFIX = "clouseau:nimbus:v2:"
_MANIFEST_TTL_S = 86400
# Limit retries and cache failures; manifest enrichment is optional.
_MANIFEST_TRIES = 2
_MANIFEST_MISS_TTL_S = 600
_VALUE_CHARS = 120
_BRANCHES = 3

_CHANNEL_EQ = re.compile(r"browserSettings\.update\.channel\s*==\s*[\"'](\w+)[\"']")
_CHANNEL_IN = re.compile(r"browserSettings\.update\.channel\s+in\s*\[([^\]]*)\]")
_QUOTED = re.compile(r"[\"'](\w+)[\"']")
_VERSION = re.compile(
    r"\b(?:app_)?version\|versionCompare\(\s*[\"']([^\"']+)[\"']\s*\)\s*(>=|<=|==|!=|>|<)\s*0")
_TARGETING_TOKENS = re.compile(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|&&|\|\||[()\[\]]''')

_FEATURE = re.compile(r"^([A-Za-z0-9][\w-]*):\s*(?:#.*)?$")
_OWNER = re.compile(r"^\s+owner:\s*(.+?)\s*$")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PREF = re.compile(r"^\s+(?:pref|fallbackPref):\s*[\"']?([\w.\-]+)")
_VARIABLES = re.compile(r"^\s+variables:\s*(?:#.*)?$")
_KEY = re.compile(r"^\s+([A-Za-z0-9_][\w.\-]*):\s*(?:#.*)?$")

_SPLIT = re.compile(r"[^A-Za-z0-9]+")
_WORDS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+")
_MIN_TERM = 4
# A term this long is also matched inside a longer one ("autofill" in "formautofill").
_MIN_SUBTERM = 6
# Exclude structural words and prefixes such as `auto` in AutoLock.
_STOP = frozenset("""
    enabled enable disabled disable feature features rollout rollouts experiment experiments
    nimbus browser extensions extension toolkit components component mozilla firefox desktop
    nightly beta release pref prefs value values config default user true false test tests
    rust panic panicking hook core alloc mozglue static wrappers wrapper crash abort assert
    assertion fail failed thread threads pool runnable event events loop message process
    handler call calls once closure lambda detail impl inner outer base init start main lib
    src mod run next make with from into this that generated scaffolding cpp rs h mm auto
""".split())

NO_ONSET = {"status": "disabled", "first_report": None, "lookback_hours": None,
            "application": None, "recipes": [], "others": []}


def gap_phrase(minutes):
    """``28 minutes``, ``5 hours``, ``3 days``."""
    if minutes < 120:
        return "{} minute{}".format(minutes, "" if minutes == 1 else "s")
    hours = minutes // 60
    if hours < 48:
        return "{} hours".format(hours)
    return "{} days".format(hours // 24)


def _parse_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def mandatory_terms(expr):
    """Extract ``&&`` terms, skipping ``||`` groups and negations."""
    expr = (expr or "").strip()
    if not expr:
        return []
    parts, depth, start = [], 0, 0
    wrapped = expr.startswith("(")
    # Quoted strings are single tokens, so their operators and brackets are ignored.
    for match in _TARGETING_TOKENS.finditer(expr):
        token = match.group()
        if token in ("(", "["):
            depth += 1
        elif token in (")", "]"):
            depth -= 1
            if depth == 0 and match.end() < len(expr):
                wrapped = False
        elif depth == 0 and token == "||":
            return []
        elif depth == 0 and token == "&&":
            parts.append(expr[start:match.start()])
            start = match.end()
    if wrapped:
        return mandatory_terms(expr[1:-1])
    if parts:
        parts.append(expr[start:])
        return [t for part in parts for t in mandatory_terms(part)]
    return [] if expr.startswith("!") else [expr]


def channels_of(record):
    """Return recognized channel requirements, or None when unknown."""
    if record.get("channel"):
        return [record["channel"]]
    found = set()
    for term in mandatory_terms(record.get("targeting")):
        found.update(_CHANNEL_EQ.findall(term))
        for group in _CHANNEL_IN.findall(term):
            found.update(_QUOTED.findall(group))
    return sorted(found) or None


def version_bounds(targeting):
    """``[[op, version], ...]`` for each mandatory ``versionCompare(...) <op> 0``."""
    return [[op, spec] for term in mandatory_terms(targeting)
            for spec, op in _VERSION.findall(term)]


_INT_MAX = 2 ** 31 - 1
_LEADING_INT = re.compile(r"\s*[+-]?\d+")
_NUM_START = re.compile(r"[0-9+\-]")


def _strtol(text):
    m = _LEADING_INT.match(text)
    if not m:
        return 0, text
    return max(-_INT_MAX - 1, min(_INT_MAX, int(m.group()))), text[m.end():]


def _version_part(part):
    """Split a version component into ``(numA, strB, numC, extraD)``."""
    if not part:
        return 0, None, 0, None
    if part == "*":
        return _INT_MAX, None, 0, None
    a, rest = _strtol(part)
    if not rest:
        return a, None, 0, None
    if rest[0] == "+":
        return a + 1, "pre", 0, None
    m = _NUM_START.search(rest)
    if not m:
        return a, rest, 0, None
    c, extra = _strtol(rest[m.start():])
    return a, rest[:m.start()], c, extra or None


def _cmp(x, y):
    return (x > y) - (x < y)


def _cmp_str(x, y):
    # Any string sorts before no string: 161.0a1 < 161.0.
    if x is None:
        return 0 if y is None else 1
    if y is None:
        return -1
    return _cmp(x, y)


def version_cmp(version, spec):
    """Compare numeric components and prerelease suffixes; None for missing versions."""
    if not version or not spec:
        return None
    for have, want in zip_longest(str(version).split("."), str(spec).split(".")):
        p, q = _version_part(have), _version_part(want)
        for compare, x, y in zip((_cmp, _cmp_str, _cmp, _cmp_str), p, q):
            r = compare(x, y)
            if r:
                return r
    return 0


_OPS = {">=": lambda c: c >= 0, "<=": lambda c: c <= 0, ">": lambda c: c > 0,
        "<": lambda c: c < 0, "==": lambda c: c == 0, "!=": lambda c: c != 0}


def _crash_channels(channel):
    label = str(channel or "").lower()
    if label.startswith("esr"):
        return {"esr"}
    if label == "beta":
        return {"beta", "aurora"}
    return {label}


def targets(recipe, channel, version):
    """Reject incompatible channel/version requirements; retain unknowns."""
    chans = recipe.get("channels")
    if chans and not (_crash_channels(channel) & set(chans)):
        return False
    for op, spec in recipe.get("versions") or []:
        cmp = version_cmp(version, spec)
        if cmp is not None and not _OPS[op](cmp):
            return False
    return True


def compact(record):
    """The fields of an Experimenter v6 record this module reads."""
    values, variables = {}, {}
    for branch in record.get("branches") or []:
        for feature in branch.get("features") or []:
            fid = feature.get("featureId")
            if fid:
                value = feature.get("value")
                variables.setdefault(fid, set()).update(
                    value.keys() if isinstance(value, dict) else ())
                values.setdefault(fid, []).append(
                    [branch.get("slug") or "", json.dumps(feature.get("value") or {},
                                                          sort_keys=True)[:_VALUE_CHARS]])
    bucket = record.get("bucketConfig") or {}
    total = bucket.get("total") or 0
    share = round(bucket.get("count", 0) / total, 4) if total else None
    return {
        "slug": record.get("slug") or "",
        "rollout": bool(record.get("isRollout")),
        "features": list(record.get("featureIds") or []),
        "published": record.get("publishedDate"),
        "channels": channels_of(record),
        "versions": version_bounds(record.get("targeting")),
        "share": share,
        "values": {fid: rows[:_BRANCHES] for fid, rows in values.items()},
        "variables": {fid: sorted(names) for fid, names in variables.items()},
    }


def _redis():
    from crashclouseau import worker

    return worker.conn


def _cache_get(key):
    try:
        raw = _redis().get(_CACHE_PREFIX + key)
    except Exception as exc:  # pragma: no cover - Redis down: fetch instead
        logger.info("nimbus: cache read failed for %s: %s", key, exc)
        return None
    return json.loads(raw) if raw else None


def _cache_set(key, value, ttl):
    try:
        _redis().set(_CACHE_PREFIX + key, json.dumps(value), ex=int(ttl))
    except Exception as exc:  # pragma: no cover - Redis down: no cache
        logger.info("nimbus: cache write failed for %s: %s", key, exc)


def live_recipes(application, cache_seconds):
    """Compacted live recipes for an Experimenter application, or None when unavailable."""
    key = "recipes:" + application
    hit = _cache_get(key)
    if hit is not None:
        return hit
    try:
        r = net.get(EXPERIMENTER_URL, params={"application": application, "status": "Live"})
        r.raise_for_status()
        recipes = [compact(rec) for rec in r.json()]
    except Exception as exc:
        logger.warning("nimbus: Experimenter fetch failed for %s: %s", application, exc)
        return None
    _cache_set(key, recipes, cache_seconds)
    return recipes


def parse_manifest(text):
    """``{feature_id: {"owners", "variables": {name: [prefs]}}}`` from FeatureManifest.yaml, read
    line by line. Variables sit at the indent of the first line under ``variables:``."""
    out = {}
    feature = block = var_indent = var = None
    for line in (text or "").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _FEATURE.match(line)
        if m:
            feature = out.setdefault(m.group(1), {"owners": [], "variables": {}})
            block = var_indent = var = None
            continue
        if feature is None:
            continue
        indent = len(line) - len(line.lstrip())
        if block is not None and indent <= block:
            block = var_indent = var = None
        if block is None:
            if _VARIABLES.match(line):
                block = indent
                continue
            m = _OWNER.match(line)
            if m and not feature["owners"]:
                # Some features list several: "owner: a@mozilla.com, b@mozilla.com".
                feature["owners"] = _EMAIL.findall(m.group(1))
            continue
        if var_indent is None:
            var_indent = indent
        if indent == var_indent:
            m = _KEY.match(line)
            var = m.group(1) if m else None
            if var:
                feature["variables"].setdefault(var, [])
            continue
        m = _PREF.match(line)
        if var and m and m.group(1) not in feature["variables"][var]:
            feature["variables"][var].append(m.group(1))
    return out


def recipe_prefs(recipe, fid, entry):
    """The prefs of the manifest variables ``recipe`` configures for feature ``fid``."""
    mapping = (entry or {}).get("variables") or {}
    prefs = []
    for name in (recipe.get("variables") or {}).get(fid) or []:
        prefs += [p for p in mapping.get(name) or [] if p not in prefs]
    return prefs


def manifest_features(node, channel):
    """The desktop feature manifest as of ``node``, parsed, or ``{}`` when unavailable."""
    if not node:
        return {}
    key = "manifest:" + node
    hit = _cache_get(key)
    if hit is not None:
        return hit
    text = hgedge.raw_file(MANIFEST_PATH, node, channel, retries=_MANIFEST_TRIES)
    if not text:
        _cache_set(key, {}, _MANIFEST_MISS_TTL_S)
        return {}
    features = parse_manifest(text)
    _cache_set(key, features, _MANIFEST_TTL_S)
    return features


def first_report(signature, product, days):
    """The date of the signature's first report in the last ``days`` days, any channel."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    params = {"signature": "=" + signature, "product": product, "date": ">=" + since,
              "_columns": ["date"], "_sort": "date", "_results_number": 1}
    got = {}

    def handler(json_, data):
        data["result"] = json_

    socorro.SuperSearch(params=params, handler=handler, handlerdata=got).wait()
    hits = (got.get("result") or {}).get("hits") or []
    return _parse_dt(hits[0].get("date")) if hits else None


def terms(text):
    """Lower-case words of an identifier, path or pref name, minus structural words."""
    out = set()
    for chunk in _SPLIT.split(text or ""):
        if not chunk:
            continue
        out.add(chunk.lower())
        out.update(w.lower() for w in _WORDS.findall(chunk))
    return {t for t in out if len(t) >= _MIN_TERM and not t.isdigit() and t not in _STOP}


def stack_terms(frames, limit):
    """Terms from the first ``limit`` frames with source revisions."""
    out = set()
    for frame in [f for f in frames or [] if f.get("node")][:limit]:
        out |= terms(frame.get("filename"))
        out |= terms(frame.get("function"))
    return out


def shared_terms(recipe_terms, stack):
    """Terms a recipe and a stack share, exactly or as a long word inside another."""
    shared = recipe_terms & stack
    for a, b in ((recipe_terms, stack), (stack, recipe_terms)):
        shared |= {t for t in a if len(t) >= _MIN_SUBTERM and any(t in u for u in b)}
    return _shortest(shared)


def _shortest(shared):
    # Keep the shortest spelling: "address" rather than "address" and "addresses".
    return sorted(t for t in shared if not any(o != t and o in t for o in shared))


def _feature_facts(recipe, fid, entry, stack):
    """A configured feature's prefs, owners and overlap with the crash stack."""
    prefs = recipe_prefs(recipe, fid, entry)
    return {
        "id": fid, "owners": entry.get("owners") or [], "prefs": prefs,
        "values": (recipe.get("values") or {}).get(fid) or [],
        "shared": shared_terms(terms(" ".join([fid, *prefs])), stack),
    }


def onset_facts(signature, product, channel, version, node, frames, first_ever=None,
                now=None):
    """Find live recipes published before the first report and separate stack matches.

    Reject known recurrences using ``first_ever``.
    """
    cfg = config.get_agent_nimbus_onset()
    out = dict(NO_ONSET, recipes=[], others=[], lookback_hours=cfg["lookback_hours"])
    if not cfg["enabled"]:
        return out
    application = APPLICATIONS.get(product or "Firefox")
    out["application"] = application
    if not application or not signature:
        return dict(out, status="unsupported")
    now = now or datetime.now(timezone.utc)
    first = None
    for attempt in range(2):
        try:
            first = first_report(signature, product or "Firefox", cfg["onset_days"])
            break
        except Exception as exc:
            # Retry once after a lookup error.
            if attempt:
                logger.warning("nimbus: first-report lookup failed for %r: %s", signature, exc)
                return dict(out, status="failed")
    if first is None:
        return dict(out, status="failed")
    out["first_report"] = first.isoformat()
    if first < now - timedelta(days=cfg["onset_days"] - 1):
        return dict(out, status="old")
    # A bounded search can miss earlier reports of a recurring signature.
    ever = _parse_dt(first_ever)
    if ever and ever.date() < first.date():
        return dict(out, status="old")
    recipes = live_recipes(application, cfg["cache_seconds"])
    if recipes is None:
        return dict(out, status="failed")
    since = first - timedelta(hours=cfg["lookback_hours"])
    window = []
    for recipe in recipes:
        published = _parse_dt(recipe.get("published"))
        if published and since <= published <= first and targets(recipe, channel, version):
            window.append((first - published, recipe))
    if not window:
        return dict(out, status="none")
    window.sort(key=lambda w: w[0])
    manifest = manifest_features(node, channel) if application == "firefox-desktop" else {}
    stack = stack_terms(frames, cfg["stack_frames"])
    matched, others = [], []
    for gap, recipe in window:
        # Keep matches per feature for owner routing.
        features = [_feature_facts(recipe, fid, manifest.get(fid) or {}, stack)
                    for fid in recipe.get("features") or []]
        shared = _shortest({t for f in features for t in f["shared"]})
        if not shared:
            others.append(recipe["slug"])
            continue
        matched.append({"slug": recipe["slug"], "rollout": recipe.get("rollout"),
                        "share": recipe.get("share"), "published": recipe.get("published"),
                        "minutes_before": int(gap.total_seconds() // 60),
                        "features": features, "shared": shared})
    cap = cfg["max_recipes"]
    return dict(out, status="ok", recipes=matched[:cap], others=others[:cap])
