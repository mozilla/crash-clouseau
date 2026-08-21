# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Does ``config._OTHER_APP_PRODUCTS`` still describe BMO and Socorro?

That map decides which open bugs belong to somebody ELSE's application
(``bugzilla_apply._split_by_application``, ``report_bug.resolve_product_component``), and until
2026-08-21 it carried a completeness CLAIM in a comment — "these are the only non-Firefox
products whose application reports crashes to Socorro at all" — that nothing checked and that
was false in both directions. This is that claim, executable:

* CHECK 1 — every BMO product the map NAMES exists on BMO and is ``is_active``. This is what
  catches the obvious wrong fix: a ``"Fenix": ["Fenix"]`` entry looks right and matches nothing,
  because BMO has no product called Fenix. It is only a search alias — on 2026-08-21
  ``/rest/product?names=Fenix`` returned ``{"products":[]}`` while ``?product=Fenix`` returned
  21,891 bugs, every one of them reading ``product: "Firefox for Android"``.
* CHECK 2 — every application reporting to Socorro that we do not triage
  (``config.get_products()``) is a KEY in the map, and every key still reports. Deliberately NO
  volume floor: the claim being audited is "reports at all", so one report is a violation, and
  the counts are printed so that a reader prices each one instead of a threshold nobody fit
  doing it for them.

IT FAILS TODAY, AND THAT IS THE POINT. 2026-08-21 over 30d: check 1 clean; check 2 names
``Fenix`` (458,043 reports), ``Focus`` (5,804) and ``ReferenceBrowser`` (125) as unmapped, and
``SeaMonkey`` as mapped-but-silent (0 reports in the whole ~180d retention). Those three are
deliberately unmapped and the evidence is above ``config._OTHER_APP_PRODUCTS`` and in
``spike/other_app_products/RESULTS.json``: mapping them moves the venue on 0 of 51 filings and 0
of the 300 loudest desktop-nightly signatures, and costs bug 1855806. This exists so that the
day that stops being true, somebody learns it from one command instead of from a crash reported
into another team's product.

WHERE IT DELIBERATELY DOES **NOT** RUN. Its two inputs are live BMO and live Socorro, so
anywhere it runs automatically it can fail a build for a reason unrelated to the change being
shipped:

* not in the unit suite — that suite is offline by policy
  (``test_orchestrator.test_gates_stay_network_free_and_the_callers_resolve``,
  ``test_compiled_out_gate.test_the_gate_itself_never_touches_the_network``). The LOGIC is unit
  tested instead, in tests/test_other_app_products.py, against captured 2026-08-21 snapshots —
  including the snapshot that makes it fail;
* not in ``bin/release.py`` — the Heroku release phase "deliberately does NOT run ingestion ...
  so the release phase stays fast and never blocks a deploy on the network", and BMO answers 502
  often enough to matter (2 of 302 lookups while this panel was being measured);
* not in ``bin/predeploy.py`` — that script's exit 1 gates ``&& git push heroku``, so a BMO
  outage would read as "triage runs in flight" and stop a deploy; it also refuses without
  ``DATABASE_URL``, which this needs none of;
* not in ``bin/schedule.py`` — nothing would act on the result, and BMO rate-limits an IP for
  ~45 minutes when pushed, a budget the filer needs more than this does.

So: by hand, when the map is edited and when plans/16-fenix-nightly-support.md lands. Run it
from the REPO ROOT — ``config._get_global`` opens the relative path ``./config/global.json``:

    uv run python bin/audit_products.py             # exit 1 = the map no longer matches reality
    uv run python bin/audit_products.py --days 180  # full retention; also names MozillaVPN
                                                    # (9 reports/180d, 0 bugs with a signature)
    uv run python bin/audit_products.py --force     # report, always exit 0
"""
import argparse
import os
import sys
from datetime import date, timedelta

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from crashclouseau import config, net  # noqa: E402

_SUPERSEARCH = "https://crash-stats.mozilla.org/api/SuperSearch/"


def bmo_products():
    """``{BMO product name: is_active}`` for every product this client may see.

    ``type=accessible`` is what the configured Bugzilla token can see; unauthenticated the list
    is smaller (194 vs 200 on 2026-08-21), which can only make CHECK 1 stricter — a product we
    cannot see reads as missing — never looser.

    THE RETRY BOUND IS LOAD-BEARING. libmozdata mounts
    ``Retry(total=Connection.MAX_RETRIES, backoff_factor=1)`` on the session from the CLASS
    attribute, and it does that BEFORE ``__init__`` reads any per-instance ``max_retries=``
    kwarg — so that kwarg is inert, the default is 256 attempts with backoff, and an
    unreachable BMO makes ``.wait()`` hang for hours instead of raising. Measured 2026-08-21
    behind a dead proxy: unbounded was still running at 120s, bounded raises ``ProxyError`` in
    2s. Without it the refuse-never-bless branch in ``main`` is unreachable for the commonest
    failure there is, and a transient 502 is still absorbed (3 attempts, 0/2/4s apart)."""
    from libmozdata.bugzilla import BugzillaProduct
    from libmozdata.connection import Connection

    got = {}

    def handler(product, data):
        data[product["name"]] = bool(product.get("is_active"))

    was = Connection.MAX_RETRIES
    Connection.MAX_RETRIES = 2
    try:
        BugzillaProduct(product_types="accessible", include_fields=["name", "is_active"],
                        product_handler=handler, product_data=got).wait()
    finally:
        Connection.MAX_RETRIES = was
    return got


def socorro_products(days=30):
    """``{Socorro product: crash reports}`` over the last *days*, with NO product filter.

    Raw SuperSearch rather than ``libmozdata.socorro.SuperSearch`` for one reason: this query
    must carry no ``product`` term at all, and the entire point of it is to see the products the
    config does not name. ``net.get`` stamps the allowlisted ``crash-clouseau`` User-Agent."""
    today = date.today()
    params = [("date", ">={}".format(today - timedelta(days=days))),
              ("date", "<{}".format(today)),
              ("_results_number", 0), ("_facets", "product"), ("_facets_size", 500)]
    resp = net.get(_SUPERSEARCH, params=params, timeout=(10, 180))
    resp.raise_for_status()
    return {f["term"]: f["count"] for f in resp.json()["facets"]["product"]}


def audit(bmo, socorro, mapped=None, ours=None):
    """Both checks, PURE: ``{"unknown", "inactive", "unmapped", "silent"}``.

    Split from the fetchers so the offline unit suite can run it against captured snapshots.
    *bmo* is ``{product: is_active}``, *socorro* is ``{product: reports}``; *mapped* defaults to
    the shipped map and *ours* to ``config.get_products()``."""
    mapped = config._OTHER_APP_PRODUCTS if mapped is None else mapped
    ours = set(config.get_products() if ours is None else ours)
    unknown, inactive = [], []
    for app, products in mapped.items():
        for product in products:
            if product not in bmo:
                unknown.append((app, product))
            elif not bmo[product]:
                inactive.append((app, product))
    return {
        "unknown": sorted(unknown),
        "inactive": sorted(inactive),
        "unmapped": sorted((p, n) for p, n in socorro.items()
                           if p not in mapped and p not in ours),
        "silent": sorted(app for app in mapped if not socorro.get(app)),
    }


def is_clean(result):
    return not any(result[key] for key in ("unknown", "inactive", "unmapped", "silent"))


def report(result, socorro, days, mapped=None, ours=None):
    mapped = config._OTHER_APP_PRODUCTS if mapped is None else mapped
    ours = set(config.get_products() if ours is None else ours)
    print("audit: CHECK 1 — the {} BMO product name(s) the map names".format(
        sum(len(v) for v in mapped.values())))
    for app, product in result["unknown"]:
        print("  FAIL  {} -> {!r} is not a BMO product name".format(app, product))
    for app, product in result["inactive"]:
        print("  FAIL  {} -> {!r} exists on BMO but is not active".format(app, product))
    if not result["unknown"] and not result["inactive"]:
        print("  OK    every one of them exists on BMO and is active")
    print("audit: CHECK 2 — every application reporting to Socorro over {}d".format(days))
    for product, count in sorted(socorro.items(), key=lambda kv: -kv[1]):
        if product in ours:
            where = "ours (config.products)"
        elif product in mapped:
            where = "mapped"
        else:
            where = "NOT IN THE MAP"
        print("  {:>12,}  {:<20s} {}".format(count, product, where))
    for product, count in result["unmapped"]:
        print("  FAIL  {} reports to Socorro ({:,} in {}d) and is not a key in the map".format(
            product, count, days))
    for app in result["silent"]:
        print("  FAIL  {} is a key in the map and reported nothing in {}d".format(app, days))
    if is_clean(result):
        print("audit: the map still describes BMO and Socorro.")
    else:
        print("audit: the map no longer describes BMO and Socorro. Read the evidence block "
              "above config._OTHER_APP_PRODUCTS before adding anything — measured 2026-08-21, "
              "every candidate addition moved 0 of 51 filings and 0 of 300 nightly signatures, "
              "and cost bug 1855806 its venue.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=30,
                        help="Socorro window for CHECK 2 (default 30; 180 is full retention)")
    parser.add_argument("--force", action="store_true", help="report but always exit 0")
    args = parser.parse_args()

    try:
        bmo = bmo_products()
        socorro = socorro_products(args.days)
    except Exception as exc:
        # Refuse, never report clean. "Cannot reach BMO/Socorro" must not read like "the map is
        # fine" — same rule as bin/predeploy.py's unreadable-dossiers branch.
        print("audit: cannot reach BMO/Socorro ({}: {}) — refusing rather than blessing a map "
              "nobody checked.".format(type(exc).__name__, str(exc).splitlines()[0][:120]),
              file=sys.stderr)
        return 0 if args.force else 1
    if not bmo or not socorro:
        print("audit: BMO returned {} products and Socorro {} — refusing.".format(
            len(bmo), len(socorro)), file=sys.stderr)
        return 0 if args.force else 1

    result = audit(bmo, socorro)
    report(result, socorro, args.days)
    return 0 if (args.force or is_clean(result)) else 1


if __name__ == "__main__":
    sys.exit(main())
