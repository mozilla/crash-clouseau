"""Design D: family-keyed map + a product->family lookup, replacing the product-keyed dict.

Shows (a) why the product-keyed shape CANNOT express the Android family and (b) the
None-product behaviour change the extra `desktop: ["Firefox"]` entry introduces."""
import os as _os  # repo-relative paths: this script moved out of /tmp into the repo
_HERE = _os.path.dirname(_os.path.abspath(__file__)) + "/"
_REPO = _os.path.dirname(_os.path.dirname(_HERE.rstrip("/")))
import os, sys
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.chdir(_REPO); sys.path.insert(0, ".")
from crashclouseau import bugzilla_apply, config

_APP_FAMILY = {"Firefox": "desktop", "Fenix": "android", "Focus": "android",
               "ReferenceBrowser": "android", "Thunderbird": "thunderbird",
               "SeaMonkey": "seamonkey", "MozillaVPN": "vpn"}
_FAMILY_PRODUCTS = {
    "desktop": ["Firefox"],
    "android": ["Firefox for Android", "Focus"],
    "thunderbird": ["Thunderbird", "MailNews Core", "Calendar", "Chat Core"],
    "seamonkey": ["SeaMonkey"],
    "vpn": ["Mozilla VPN"],
}
def foreign_D(product, exempt_unknown_from_desktop=True):
    fam = _APP_FAMILY.get(product)
    out = set()
    for f, ps in _FAMILY_PRODUCTS.items():
        if f == fam:
            continue
        # An UNKNOWN crash product must not lose the desktop venue it would otherwise get:
        # this map's only inclusion-shaped entry is the one that can stop us filing at all.
        if fam is None and f == "desktop" and exempt_unknown_from_desktop:
            continue
        out |= set(ps)
    return frozenset(out)

def split(bugs, foreign):
    ours = [b for b in bugs if (b.get("product") or "") not in foreign]
    theirs = [b for b in bugs if (b.get("product") or "") in foreign]
    return ours, theirs
def b(i, p): return {"id": i, "product": p, "creation_time": "2026-01-01T00:00:00Z"}

CASES = [
    ("CE1 2057980 MailNews Core, Firefox crash",        b(2057980, "MailNews Core"), "Firefox", "excluded"),
    ("CE2 Core, Firefox crash",                          b(1, "Core"), "Firefox", "venue"),
    ("CE2 Firefox, Firefox crash",                       b(1, "Firefox"), "Firefox", "venue"),
    ("CE2 Toolkit, Firefox crash",                       b(1, "Toolkit"), "Firefox", "venue"),
    ("CE2 GeckoView, Firefox crash  <-- test:359",       b(1, "GeckoView"), "Firefox", "venue"),
    ("CE2 'Fenix' (a string BMO never emits)",           b(1, "Fenix"), "Firefox", "venue"),
    ("CE3 Application Services (shared)",                b(1, "Application Services"), "Firefox", "venue"),
    ("unknown crash product: MailNews Core still excluded", b(2057980, "MailNews Core"), None, "excluded"),
    ("unknown crash product: Firefox still a venue",     b(1, "Firefox"), None, "venue"),
    ("unknown crash product: Core still a venue",        b(1, "Core"), None, "venue"),
    ("FENIX-DAY Firefox::Installer excluded",            b(1681745, "Firefox"), "Fenix", "excluded"),
    ("FENIX-DAY Firefox for Android is a venue",         b(1, "Firefox for Android"), "Fenix", "venue"),
    ("FENIX-DAY GeckoView is a venue",                   b(1, "GeckoView"), "Fenix", "venue"),
    ("FENIX-DAY Core is a venue",                        b(1, "Core"), "Fenix", "venue"),
    ("FENIX-DAY Thunderbird excluded",                   b(1, "Thunderbird"), "Fenix", "excluded"),
    ("FOCUS-DAY Firefox for Android is a venue",         b(1, "Firefox for Android"), "Focus", "venue"),
    ("FOCUS-DAY Focus is a venue",                       b(1, "Focus"), "Focus", "venue"),
    ("FOCUS-DAY Firefox (desktop) excluded",             b(1, "Firefox"), "Focus", "excluded"),
]
print("%-56s %-9s %-10s %-10s" % ("case", "want", "D2(noGV)", "D no-exempt"))
bad = 0
for label, bug, product, want in CASES:
    g1 = "venue" if split([bug], foreign_D(product))[0] else "excluded"
    g2 = "venue" if split([bug], foreign_D(product, False))[0] else "excluded"
    m1 = "" if g1 == want else " WRONG"
    m2 = "" if g2 == want else " WRONG"
    bad += g1 != want
    print("%-56s %-9s %-10s %-10s" % (label[:56], want, g1+m1, g2+m2))
print("\nD2(noGV) failures:", bad)
print("\nWHY the product-keyed shape cannot express this: get_other_app_products drops only the")
print("ONE key equal to the crash product, so with Fenix and Focus both keyed to the Android")
print("product set, a Fenix crash still sees 'Firefox for Android' as foreign via the Focus key.")
