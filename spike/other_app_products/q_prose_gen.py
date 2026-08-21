"""Item 4: can agent/tools/bugzilla.py:94 be GENERATED from config, and what does it cost?

Demonstrated against the SHIPPED objects, without editing the repo."""
import os as _os  # repo-relative paths: this script moved out of /tmp into the repo
_HERE = _os.path.dirname(_os.path.abspath(__file__)) + "/"
_REPO = _os.path.dirname(_os.path.dirname(_HERE.rstrip("/")))
import os, sys
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
os.chdir(_REPO); sys.path.insert(0, ".")
import dataclasses
from crashclouseau import config
from crashclouseau.agent.tools import bugzilla as bztools

td = next(t for t in bztools.TOOLS if t.name == "signature_bugs")
print("ToolDefinition is a dataclass:", dataclasses.is_dataclass(td),
      "| frozen:", td.__dataclass_params__.frozen)
print()
print("--- description TODAY (hand-written, 2nd copy of the list) ---")
print(td.description)

def describe_other_applications(product=None):
    """One clause naming every OTHER application and the BMO products that are its alone."""
    bits = []
    for app in sorted(config._OTHER_APP_PRODUCTS):
        if app == product:
            continue
        ps = config._OTHER_APP_PRODUCTS[app]
        bits.append(app if ps == [app]
                    else "{} (``{}``)".format(app, "``, ``".join(ps)))
    return ", ".join(bits[:-1]) + " and " + bits[-1] if len(bits) > 1 else (bits[0] if bits else "")

NEW = ("Each row names the bug's product::component, and you have to read it: every other "
       "application built on mozilla-central ({apps}) shares Gecko's crash signatures, so a "
       "matching bug in one of THEIR products is a different application's crash population "
       "with its own cause, however well the stack matches. It is context, not this crash's bug.")
td.description = td.description.split("Each row names")[0] + \
    NEW.replace("{apps}", describe_other_applications("Firefox"))
print()
print("--- description GENERATED from config._OTHER_APP_PRODUCTS ---")
print(td.description)
print()
print("cost: 1 helper in config (~6 lines), 1 placeholder + 3-line rewrite loop at the bottom")
print("      of agent/tools/bugzilla.py, 1 test asserting the rendered text names every product")
print("      in the map. No framework change: registry.ToolDefinition.description is a plain")
print("      mutable dataclass field read by the SDK adapter after tools_in() returns.")
