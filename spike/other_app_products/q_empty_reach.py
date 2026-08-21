"""Could an 'EMPTY: no frame data available' crash ever reach the filer? Ask Socorro whether
those reports have a stack at all, and check the shipped unsymbolicated gate."""
import os as _os  # repo-relative paths: this script moved out of /tmp into the repo
_HERE = _os.path.dirname(_os.path.abspath(__file__)) + "/"
_REPO = _os.path.dirname(_os.path.dirname(_HERE.rstrip("/")))
import json, os, sys, urllib.request, urllib.parse
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.chdir(_REPO); sys.path.insert(0, ".")
from crashclouseau import bugzilla_apply
for s in ("EMPTY: no frame data available; MissingThreadList",
          "EMPTY: no frame data available; OK",
          "arena_run_reg_dalloc | arena_t::DallocSmall | arena_dalloc | idalloc",
          "MOZ_Crash", "mozilla::ThreadEventTarget::Dispatch",
          "@0xe2ba40f948"):
    print("%-55s unsymbolicated=%s specific=%s" % (
        s[:55], bugzilla_apply._is_unsymbolicated(s), bugzilla_apply._is_specific_signature(s)))
