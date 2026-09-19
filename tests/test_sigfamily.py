# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""The same crash under a new name (`sigfamily`), on the filings a human corrected.

Every fixture below is one of the 150 bugs the 2026-09-18 audit replayed (plans/24), with the
numbers crash-stats gave that night: CheckLogMessage -> PatchNtdll (2073210), the JS OOM abort's
`unknown` -> `large` (2071606/2071620), wgpu's MapCallback at frame 12 (2069647), the Windows
symbol gap onto `WaitOnAddress` (2070554), BitSet's other-channel spelling (2070376); and the
five shapes the prototype flagged wrongly -- AsyncShutdownTimeout blocker lists, the `EnterJit`
trampoline, a hang related to a non-hang, a quiet name an hour after the build, a bare
frame-set with no sibling at all (2061960's `nsFind`, FIXED, which must keep filing).

Run: DATABASE_URL=sqlite:// REDIS_URL=redis://localhost:6379/0 \\
     uv run python -m unittest tests.test_sigfamily
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import unittest  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from unittest import mock  # noqa: E402

from crashclouseau import config, sigage, sigfamily as sf  # noqa: E402


def _dt(day, hour=12):
    return datetime.strptime(day, "%Y-%m-%d").replace(hour=hour, tzinfo=timezone.utc)


def _daily(start, end, per_day, hour="093000"):
    """One build a day from *start* to *end* inclusive, *per_day* reports each."""
    out = []
    d = _dt(start)
    stop = _dt(end)
    while d <= stop:
        out.append((d.strftime("%Y%m%d") + hour, per_day))
        d += timedelta(days=1)
    return out


class FakeSocorro:
    """Answers `sigfamily._run`'s batched queries off canned facets.

    ``discovery`` maps ``("signature" | "proto_signature", anchor) -> {sig: count}`` for the
    ``~`` (contains) queries; ``builds`` maps ``sig -> [(buildid, count)]`` and ``days`` maps
    ``sig -> {day: count}`` for the per-signature build/date timelines. Unknown = empty."""

    def __init__(self, discovery=None, builds=None, days=None, fail=False):
        self.discovery = discovery or {}
        self.builds = builds or {}
        self.days = days or {}
        self.fail = fail
        self.calls = []

    def __call__(self, queries):
        self.calls.append([q.params for q in queries])
        if self.fail:
            raise RuntimeError("socorro is down")
        for q in queries:
            q.handler(self.answer(q.params), q.handlerdata)

    def answer(self, p):
        if p.get("_facets") == "signature":
            if isinstance(p.get("signature"), list):
                terms = {s[1:]: sum(n for _b, n in self.builds.get(s[1:], []))
                         for s in p["signature"] if s[1:] in self.builds}
            elif p.get("signature"):
                assert p["signature"].startswith("~"), p
                terms = self.discovery.get(("signature", p["signature"][1:]), {})
            else:
                assert p["proto_signature"].startswith("~"), p
                terms = self.discovery.get(("proto_signature", p["proto_signature"][1:]), {})
            return {"facets": {"signature": [{"term": t, "count": n} for t, n in terms.items()]}}
        if p.get("_facets") == "build_id":
            sig = p["signature"][1:]
            return {"facets": {
                "build_id": [{"term": int(b), "count": n} for b, n in self.builds.get(sig, [])],
                "histogram_date": [{"term": d + "T00:00:00+00:00", "count": n}
                                   for d, n in sorted(self.days.get(sig, {}).items())],
            }}
        raise AssertionError("unexpected query {!r}".format(p))

    def anchors_asked(self):
        out = []
        for batch in self.calls:
            for p in batch:
                if p.get("_facets") != "signature":
                    continue
                if isinstance(p.get("signature"), str):
                    out.append(("signature", p["signature"][1:]))
                elif p.get("proto_signature"):
                    out.append(("proto_signature", p["proto_signature"][1:]))
        return out


class _Base(unittest.TestCase):
    ever = {}

    def setUp(self):
        sf.clear_cache()
        self.addCleanup(sf.clear_cache)
        p = mock.patch.object(sigage, "first_seen_ever_facts",
                              side_effect=lambda sigs: {s: self.ever[s] for s in sigs
                                                        if s in self.ever})
        p.start()
        self.addCleanup(p.stop)

    def _lookup(self, fake, *args, **kw):
        with mock.patch.object(sf, "_run", fake):
            return sf.lookup(*args, **kw)


# ---------------------------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------------------------
class TestFrames(unittest.TestCase):
    def test_socorros_lists_are_matched_as_siggen_matches_them(self):
        # Start-anchored prefixes, no `$`: `Rtl` is a prefix entry and covers every Rtl* frame.
        self.assertTrue(sf.socorro_skips("std::panicking::begin_panic_handler"))
        self.assertTrue(sf.socorro_skips("RtlWaitOnAddress"))
        self.assertTrue(sf.socorro_skips("logging::LogMessage::~LogMessage"))
        self.assertFalse(sf.socorro_skips("WaitOnAddress"))
        self.assertFalse(sf.socorro_skips("sandbox::InterceptionManager::PatchNtdll"))
        # The class Chromium added later is NOT listed: that gap is bug 2073210.
        self.assertFalse(sf.socorro_skips(
            "logging::(anonymous namespace)::CheckLogMessage::~CheckLogMessage"))
        self.assertGreater(len(sf.skip_patterns()), 500)

    def test_specific_frames(self):
        self.assertEqual(sf.specific_frames(
            "shutdownhang | ntdll.dll | kernelbase.dll | mozilla::MaybeLeakRefPtr<T>::~MaybeLeakRefPtr"),
            ["mozilla::MaybeLeakRefPtr<T>::~MaybeLeakRefPtr"])
        # `Rtl*` is prefix-listed; the OS primitive `WaitOnAddress` is still specific for the
        # FAMILY (it is what identifies the catch-all predecessor).
        self.assertEqual(sf.specific_frames("shutdownhang | RtlWaitOnAddress | WaitOnAddress"),
                         ["WaitOnAddress"])
        for f in ("core::ptr::drop_in_place<T>", "<T as core::clone::uninit::CopySpec>::clone_one",
                  "<alloc::vec::Vec<T, A> as core::ops::drop::Drop>::drop",
                  "js::jit::EnterJit", "js::Interpret", "libxul.so (deleted)", "@0x0",
                  "<unknown in amdxx64.dll>", "libwayland-client.so.0", "OOM", "large",
                  "shutdownhang", "js::AutoEnterOOMUnsafeRegion::crash_impl", "MOZ_Crash",
                  "mozilla::detail::MutexImpl::mutexLock"):
            self.assertFalse(sf.specific_frame(f), f)
        for f in ("CanEnterBaselineJIT", "js::jit::CanBaselineInterpretScript",
                  "js::gc::AllocateTenuredCellInGC", "mozilla::webgpu::WebGPUParent::MapCallback",
                  "style_traits::owned_slice::impl$1::drop"):
            self.assertTrue(sf.specific_frame(f), f)

    def test_normalisation_makes_the_toolchains_agree(self):
        self.assertEqual(sf.normalize_frame("style_traits::owned_slice::impl$1::drop"),
                         "style_traits::owned_slice::drop")
        self.assertEqual(sf.normalize_frame(
            "<style_traits::owned_slice::OwnedSlice<T> as core::ops::drop::Drop>::drop"),
            "style_traits::owned_slice::drop")
        self.assertEqual(sf.normalize_frame("core::ptr::drop_in_place<T>"),
                         sf.normalize_frame("core::ptr::drop_in_place"))
        self.assertEqual(sf.normalize_frame("Foo::Shutdown::$_95::operator()"),
                         sf.normalize_frame("Foo::Shutdown::<lambda_0>::operator()"))
        self.assertEqual(sf.normalize_frame("mozilla::BitSet<T>::BitSet"), "mozilla::BitSet::BitSet")

    def test_unsymbolicated_is_no_symbol_at_all(self):
        for s in ("xul.dll | _PR_MD_UNLOCK | PR_Unlock | xul.dll",
                  "libxul.so (deleted) | libxul.so (deleted) | libnspr4.so (deleted)",
                  "libvulkan_radeon.so", "amdxx64.dll | RtlAllocateHeap", "@0xe2ba40f948",
                  "shutdownhang | libc.so.6 | libpthread.so.0"):
            self.assertTrue(sf.is_unsymbolicated(s), s)
        # Partly symbolicated, or a prefix-listed frame standing alone: still a symbol.
        for s in ("OOM | unknown | memcpy_repmovs_Intel | mozilla::dom::RTCEncodedFrameBase::RTCEncodedFrameBase",
                  "mozilla::detail::MutexImpl::mutexLock", "IPC::ParamTraits<T>::Read",
                  "shutdownhang | ntdll.dll | kernelbase.dll | mozilla::MaybeLeakRefPtr<T>::~MaybeLeakRefPtr",
                  "core::ptr::drop_in_place<T> | <alloc::vec::Vec<T, A> as core::ops::drop::Drop>::drop"):
            self.assertFalse(sf.is_unsymbolicated(s), s)
        self.assertFalse(sf.is_unsymbolicated(""))

    def test_generated_spellings(self):
        s = "OOM | large | js::AutoEnterOOMUnsafeRegion::crash_impl | js::gc::FreeSpan::allocate"
        self.assertEqual(sf.generated_spellings(s), {
            "OOM | unknown | js::AutoEnterOOMUnsafeRegion::crash_impl | js::gc::FreeSpan::allocate",
            "OOM | small | js::AutoEnterOOMUnsafeRegion::crash_impl | js::gc::FreeSpan::allocate",
            "js::AutoEnterOOMUnsafeRegion::crash_impl | js::gc::FreeSpan::allocate"})
        lam = "mozilla::dom::quota::QuotaManager::Shutdown::<T>::operator()"
        self.assertEqual(sf.generated_spellings(lam),
                         {"mozilla::dom::quota::QuotaManager::Shutdown::$::operator()"})
        self.assertEqual(sf.generated_spellings("nsFind::FindFromRangeBoundaries"), set())


# ---------------------------------------------------------------------------------------------
# Relations
# ---------------------------------------------------------------------------------------------
CHECK = "logging::(anonymous namespace)::CheckLogMessage::~CheckLogMessage"
PATCH = "sandbox::InterceptionManager::PatchNtdll"
CHECK_PROTO = (
    "logging::LogMessage::~LogMessage | {c} | {c} | logging::CheckNoreturnError::~CheckNoreturnError"
    " | {p} | sandbox::InterceptionManager::InitializeInterceptions"
    " | sandbox::PolicyBase::SetupAllInterceptions | sandbox::PolicyBase::ApplyToTarget"
    " | sandbox::BrokerServicesBase::FinishSpawnTargetImpl").format(c=CHECK, p=PATCH)

OOM_LARGE = ("OOM | large | js::AutoEnterOOMUnsafeRegion::crash_impl"
             " | js::AutoEnterOOMUnsafeRegion::crash_impl | js::AutoEnterOOMUnsafeRegion::crash"
             " | js::gc::AllocateTenuredCellInGC")
OOM_UNKNOWN = ("OOM | unknown | js::AutoEnterOOMUnsafeRegion::crash_impl"
               " | js::AutoEnterOOMUnsafeRegion::crash | js::gc::AllocateTenuredCellInGC")
OOM_PROTO = ("MOZ_Crash | js::AutoEnterOOMUnsafeRegion::crash_impl"
             " | js::AutoEnterOOMUnsafeRegion::crash_impl | js::AutoEnterOOMUnsafeRegion::crash"
             " | js::gc::AllocateTenuredCellInGC | js::gc::TenuringTracer::allocCell"
             " | js::gc::TenuringTracer::alloc | js::gc::TenuringTracer::promoteObjectSlow")

WGPU = "wgpu_bindings::server::wgpu_server_buffer_get_mapped_range"
MAPCB = "mozilla::webgpu::WebGPUParent::MapCallback"
WGPU_PROTO = " | ".join([
    "std::panicking::begin_panic_handler", "std::panicking::rust_panic_with_hook",
    "std::panicking::begin_panic_handler::{{closure}}", "std::sys::backtrace::__rust_end_short_backtrace",
    "rust_begin_unwind", "core::panicking::panic_fmt", "core::panicking::panic_display",
    "core::option::expect_failed", "core::option::Option<T>::expect", "core::result::unwrap_failed",
    "core::result::Result<T, E>::expect",
    WGPU, MAPCB, "mozilla::webgpu::WebGPUParent::RecvBufferMap::<T>::operator()",
    "mozilla::detail::RunnableFunction<T>::Run", "nsThread::ProcessNextEvent"])

HANG = "shutdownhang | ntdll.dll | kernelbase.dll | mozilla::MaybeLeakRefPtr<T>::~MaybeLeakRefPtr"
WAIT1 = "shutdownhang | RtlWaitOnAddress | WaitOnAddress"
WAIT2 = "shutdownhang | RtlpWaitOnAddress | RtlWaitOnAddress | WaitOnAddress"
WAIT3 = "shutdownhang | RtlpWaitOnAddressWithTimeout | RtlpWaitOnAddress | RtlWaitOnAddress | WaitOnAddress"
WAIT_BARE = "RtlWaitOnAddress | WaitOnAddress"
HANG_VARIANT = "shutdownhang | RtlWaitOnAddress | kernelbase.dll | mozilla::MaybeLeakRefPtr<T>::~MaybeLeakRefPtr"
HANG_PROTO = ("ZwWaitForAlertByThreadId | RtlWaitOnAddress | WaitOnAddress | mozilla::FutexImpl<T>::wait"
              " | mozilla::detail::ConditionVariableImpl::wait_for | mozilla::detail::ConditionVariableImpl::wait"
              " | mozilla::OffTheBooksCondVar::Wait | mozilla::TaskController::GetRunnableForMTTask"
              " | nsThread::ProcessNextEvent | NS_ProcessNextEvent | mozilla::SpinEventLoopUntil"
              " | mozilla::net::nsHttpConnectionMgr::Shutdown")

BITSET4 = ("mozilla::BitSet<T>::operator= | mozilla::BitSet<T>::BitSet | mozilla::BitSet<T>::BitSet"
           " | js::gc::BitSetIter<T>::BitSetIter")
BITSET3 = "mozilla::BitSet<T>::operator= | mozilla::BitSet<T>::BitSet | js::gc::BitSetIter<T>::BitSetIter"


class TestRelate(unittest.TestCase):
    def test_pushed_down_reads_the_whole_proto(self):
        self.assertEqual(sf.relate(CHECK, PATCH, CHECK_PROTO), "pushed-down")
        # wgpu: the old name is frame 12, past the 12-frame window the prototype read.
        self.assertEqual(sf.relate(WGPU, MAPCB, WGPU_PROTO), "pushed-down")
        self.assertIsNone(sf.relate(WGPU, MAPCB, None))
        self.assertEqual(sf.relate(HANG, WAIT1, HANG_PROTO), "pushed-down")

    def test_frame_variant(self):
        self.assertEqual(sf.relate(OOM_LARGE, OOM_UNKNOWN), "frame-variant")
        self.assertEqual(sf.relate(BITSET4, BITSET3), "frame-variant")
        self.assertEqual(sf.relate(HANG, HANG_VARIANT), "frame-variant")
        # The Windows and Linux spellings of one Rust drop.
        self.assertEqual(sf.relate(
            "core::ptr::drop_in_place | style_traits::owned_slice::impl$1::drop",
            "core::ptr::drop_in_place<T> | <style_traits::owned_slice::OwnedSlice<T> as core::ops::drop::Drop>::drop"),
            "frame-variant")
        # Shares a frame but is three edits away: a different crash through the same helper.
        self.assertIsNone(sf.relate("A::a | B::b | C::c | D::d", "E::e | F::f | C::c | G::g"))

    def test_spelling(self):
        self.assertEqual(sf.relate(
            "OOM | large | js::AutoEnterOOMUnsafeRegion::crash_impl | js::gc::FreeSpan::allocate",
            "OOM | unknown | js::AutoEnterOOMUnsafeRegion::crash_impl | js::gc::FreeSpan::allocate"),
            "spelling")

    def test_what_is_not_a_relation(self):
        # A hang and a non-hang are different crashes whatever frames they share (2072770's
        # `CanEnterBaselineJIT`, the plain 2024 crash, is not the shutdown hang's venue).
        self.assertIsNone(sf.relate("shutdownhang | js::jit::CanBaselineInterpretScript",
                                    "js::jit::CanBaselineInterpretScript"))
        self.assertIsNone(sf.relate(HANG, WAIT_BARE, HANG_PROTO))
        # The JIT trampoline identifies nothing.
        self.assertIsNone(sf.relate("mozilla::Foo::Bar | js::jit::EnterJit",
                                    "mozilla::Baz::Qux | js::jit::EnterJit"))
        # AsyncShutdownTimeout "frames" are blocker names.
        self.assertIsNone(sf.relate(
            "AsyncShutdownTimeout | profile-before-change | CookiePersistentStorage: cookies.sqlite closing",
            "AsyncShutdownTimeout | profile-before-change | CookiePersistentStorage: cookies.sqlite closing,ServiceWorkerRegistrar: Flushing data"))
        self.assertFalse(sf.family_eligible("AsyncShutdownTimeout | profile-before-change | X"))
        self.assertIsNone(sf.relate(CHECK, CHECK, CHECK_PROTO))
        # A candidate with no specific frame of its own relates to nothing.
        self.assertIsNone(sf.relate(HANG, "shutdownhang | ntdll.dll | kernelbase.dll", HANG_PROTO))

    def test_changed_frames_and_their_sentence(self):
        self.assertEqual(sf.changed_frames(CHECK, PATCH), {"added": [CHECK], "removed": [PATCH]})
        self.assertEqual(sf.describe_change(CHECK, PATCH, "pushed-down"),
                         "the new frame `{}` took over the name from `{}`".format(CHECK, PATCH))
        self.assertEqual(sf.describe_change(OOM_LARGE, OOM_UNKNOWN, "frame-variant"),
                         "the OOM size class `unknown` became `large`")
        self.assertEqual(sf.describe_change(
            "shutdownhang | js::jit::CanBaselineInterpretScript", "shutdownhang | CanEnterBaselineJIT",
            "pushed-down"),
            "the new frame `js::jit::CanBaselineInterpretScript` took over the name from `CanEnterBaselineJIT`")
        self.assertIn("took over the name from `RtlWaitOnAddress`, `WaitOnAddress`",
                      sf.describe_change(HANG, WAIT1, "pushed-down"))
        self.assertEqual(sf.describe_change(BITSET4, BITSET3, "frame-variant"),
                         "the two names spell the same frames differently")


# ---------------------------------------------------------------------------------------------
# The lookup, case by case
# ---------------------------------------------------------------------------------------------
class TestCheckLogMessage(_Base):
    """Bug 2073210: release, filed 2026-09-17; S first on 155.0 (build 20260812182057)."""
    ever = {CHECK: {"first_build": "20260812182057", "first_date": "2026-08-12"},
            PATCH: {"first_build": "20250310180126", "first_date": "2025-03-10"}}
    NORETURN = "logging::CheckNoreturnError::~CheckNoreturnError"
    OOM_CHECK = "OOM | unknown | " + CHECK

    def fake(self):
        return FakeSocorro(
            discovery={
                ("signature", CHECK): {CHECK: 5220, self.OOM_CHECK: 44},
                ("proto_signature", self.NORETURN): {self.NORETURN: 6, CHECK: 5220},
                ("proto_signature", PATCH): {PATCH: 7335, "OOM | large | " + PATCH: 2},
            },
            builds={
                CHECK: [("20260812182057", 3000), ("20260903215306", 2220)],
                PATCH: [("20260715120000", 5000), ("20260722120000", 374)],
                self.NORETURN: [("20260824154132", 3)],
                "OOM | large | " + PATCH: [("20260427013024", 2)],
            },
            days={PATCH: {"2026-07-25": 374, "2026-08-13": 100, "2026-08-20": 64}},
        )

    def test_the_predecessor_the_stack_still_carries(self):
        fake = self.fake()
        fam = self._lookup(fake, CHECK, CHECK_PROTO, "Firefox", "release", "20260903215306",
                           until=_dt("2026-09-17", 18))
        self.assertEqual(fam["lookup"], "ok")
        self.assertEqual(fam["s_first_build"], "20260812182057")
        self.assertFalse(fam["at_wall"])
        self.assertEqual([p["signature"] for p in fam["predecessors"]], [PATCH])
        p = fam["predecessors"][0]
        self.assertEqual((p["relation"], p["status"], p["before"], p["after"], p["after_dates"]),
                         ("pushed-down", "handoff", 374, 0, 164))
        self.assertGreater(p["expected_after"], 400)
        self.assertEqual(p["alignment"], "build")          # old builds kept reporting: a code change
        self.assertEqual(p["first_seen_ever"], "20250310180126")
        self.assertEqual(p["change"], "the new frame `{}` took over the name from `{}`".format(CHECK, PATCH))
        self.assertEqual(fam["family_first_seen_ever"], "20250310180126")
        self.assertEqual(fam["family_first_seen_ever_date"], "2025-03-10")
        self.assertEqual(fam["fan_in"], 1)
        self.assertEqual(fam["alignment"], "build")
        self.assertEqual(fam["changed_frames"], {"added": [CHECK], "removed": [PATCH]})
        statuses = {s["signature"]: s["status"] for s in fam["siblings"]}
        self.assertEqual(statuses, {self.NORETURN: "younger", self.OOM_CHECK: "other_channel"})
        # Predecessors first, then the siblings loudest first.
        self.assertEqual(sf.spellings(fam), [PATCH, self.OOM_CHECK, self.NORETURN])

    def test_the_anchors_skip_what_socorro_skips(self):
        fake = self.fake()
        self._lookup(fake, CHECK, CHECK_PROTO, "Firefox", "release", until=_dt("2026-09-17"))
        asked = fake.anchors_asked()
        self.assertEqual(asked[0], ("signature", CHECK))
        # `logging::LogMessage::~LogMessage` is on the irrelevant list: never an anchor.
        self.assertNotIn(("proto_signature", "logging::LogMessage::~LogMessage"), asked)
        self.assertEqual([a for k, a in asked if k == "proto_signature"],
                         [self.NORETURN, PATCH, "sandbox::InterceptionManager::InitializeInterceptions"])
        # All discovery in one round-trip, all timelines in a second.
        self.assertEqual(len(fake.calls), 2)
        timelines = [p["signature"][1:] for p in fake.calls[1]]
        self.assertEqual(timelines[0], CHECK)
        self.assertEqual(fake.calls[1][0]["release_channel"], "release")
        self.assertEqual(fake.calls[1][0]["_facets"], "build_id")
        # Discovery is product-wide, every channel.
        self.assertNotIn("release_channel", fake.calls[0][0])

    def test_two_distinct_predecessors_is_a_catch_all(self):
        other = "sandbox::TargetServicesBase::LowerToken"
        fake = self.fake()
        fake.discovery[("proto_signature", PATCH)][other] = 900
        fake.builds[other] = [("20260722120000", 90)]
        proto = CHECK_PROTO.replace(PATCH, PATCH + " | " + other)
        fam = self._lookup(fake, CHECK, proto, "Firefox", "release", until=_dt("2026-09-17"))
        self.assertEqual([p["signature"] for p in fam["predecessors"]], [PATCH, other])
        self.assertEqual(fam["fan_in"], 2)

    def test_the_answer_is_cached_per_day_and_a_failure_is_not(self):
        fake = self.fake()
        self._lookup(fake, CHECK, CHECK_PROTO, "Firefox", "release", until=_dt("2026-09-17"))
        self._lookup(fake, CHECK, CHECK_PROTO, "Firefox", "release", until=_dt("2026-09-17", 20))
        self.assertEqual(len(fake.calls), 2)
        self._lookup(fake, CHECK, CHECK_PROTO, "Firefox", "release", until=_dt("2026-09-18"))
        self.assertEqual(len(fake.calls), 4)
        down = FakeSocorro(fail=True)
        fam = self._lookup(down, CHECK, CHECK_PROTO, "Firefox", "beta", until=_dt("2026-09-17"))
        self.assertEqual(fam["lookup"], "failed")
        self.assertEqual((fam["predecessors"], fam["siblings"], fam["family_first_seen_ever"]),
                         ([], [], None))
        self._lookup(down, CHECK, CHECK_PROTO, "Firefox", "beta", until=_dt("2026-09-17"))
        self.assertEqual(len(down.calls), 2, "a failed lookup must be asked again")

    def test_the_kill_switch(self):
        fake = self.fake()
        with mock.patch.object(config, "get_agent_signature_family",
                               return_value={"enabled": False, "days": 182, "max_candidates": 8}):
            fam = self._lookup(fake, CHECK, CHECK_PROTO, "Firefox", "release")
        self.assertEqual(fam["lookup"], "disabled")
        self.assertEqual(fake.calls, [])
        with mock.patch.dict(os.environ, {"SIGNATURE_FAMILY_ENABLED": "0"}):
            self.assertFalse(config.get_agent_signature_family()["enabled"])
        self.assertTrue(config.get_agent_signature_family()["enabled"])


class TestJsOomAbort(_Base):
    """Bugs 2071606 / 2071620: nightly, the fix to OOM reporting moved `unknown` to `large` on
    build 20260911092915; the spike filer filed both the next day."""
    ever = {OOM_LARGE: {"first_build": "20260911092915", "first_date": "2026-09-11"},
            OOM_UNKNOWN: {"first_build": "20240430094738", "first_date": "2024-04-30"}}
    OLD_LARGE = ("OOM | large | js::AutoEnterOOMUnsafeRegion::crash_impl"
                 " | js::AutoEnterOOMUnsafeRegion::crash | js::gc::AllocateTenuredCellInGC")
    ALLOC_CELL = ("OOM | unknown | js::AutoEnterOOMUnsafeRegion::crash_impl"
                  " | js::gc::TenuringTracer::allocCell")

    def fake(self):
        return FakeSocorro(
            discovery={("signature", "js::gc::AllocateTenuredCellInGC"):
                       {OOM_LARGE: 55, OOM_UNKNOWN: 13379, self.OLD_LARGE: 11},
                       ("proto_signature", "js::gc::TenuringTracer::allocCell"): {self.ALLOC_CELL: 24}},
            builds={OOM_LARGE: [("20260911092915", 30), ("20260912091500", 25)],
                    OOM_UNKNOWN: _daily("2026-08-14", "2026-09-10", 37),
                    self.OLD_LARGE: [("20260901093000", 2)]},
            days={OOM_UNKNOWN: {"2026-09-11": 40, "2026-09-12": 17}},
        )

    def test_a_day_old_handoff_is_decisive_when_the_old_name_was_loud(self):
        fam = self._lookup(self.fake(), OOM_LARGE, OOM_PROTO, "Firefox", "nightly",
                           "20260911092915", until=_dt("2026-09-12", 12))
        self.assertEqual([p["signature"] for p in fam["predecessors"]], [OOM_UNKNOWN])
        p = fam["predecessors"][0]
        self.assertEqual((p["relation"], p["before"], p["after"]), ("frame-variant", 28 * 37, 0))
        self.assertAlmostEqual(p["expected_after"], 37 * 1.1, delta=1.0)
        # Nightly's old builds die within a day, so the alignment is not readable there.
        self.assertEqual(p["alignment"], "unknown")
        self.assertEqual(p["change"], "the OOM size class `unknown` became `large`")
        self.assertEqual(fam["family_first_seen_ever"], "20240430094738")
        statuses = {s["signature"]: s["status"] for s in fam["siblings"]}
        self.assertEqual(statuses, {self.OLD_LARGE: "older", self.ALLOC_CELL: "other_channel"})

    def test_the_spike_on_the_handoff_build_is_a_re_bucketing(self):
        fake = self.fake()
        with mock.patch.object(sf, "_run", fake):
            hit = sf.handoff_for_spike(OOM_LARGE, OOM_PROTO, "Firefox", "nightly",
                                       "20260911092915", until=_dt("2026-09-12", 12))
            self.assertEqual(hit["signature"], OOM_UNKNOWN)
            self.assertEqual(hit["s_first_build"], "20260911092915")
            # The same signature spiking on a LATER build-day is a real spike of an old crash.
            self.assertIsNone(sf.handoff_for_spike(OOM_LARGE, OOM_PROTO, "Firefox", "nightly",
                                                   "20260913093000", until=_dt("2026-09-12", 12)))
            self.assertIsNone(sf.handoff_for_spike(OOM_LARGE, OOM_PROTO, "Firefox", "nightly",
                                                   None, until=_dt("2026-09-12", 12)))

    def test_a_quiet_old_name_is_undecided_an_hour_after_the_build(self):
        fake = self.fake()
        fake.builds[OOM_UNKNOWN] = _daily("2026-08-14", "2026-09-10", 1)     # 28 reports
        fam = self._lookup(fake, OOM_LARGE, OOM_PROTO, "Firefox", "nightly",
                           until=_dt("2026-09-11", 10))
        self.assertEqual(fam["predecessors"], [])
        row = next(s for s in fam["siblings"] if s["signature"] == OOM_UNKNOWN)
        self.assertEqual(row["status"], "undecided")
        self.assertIsNone(fam["family_first_seen_ever"])


class TestWgpu(_Base):
    """Bug 2069647: the old name is frame 12 of the stack, past the prototype's window."""
    ever = {WGPU: {"first_build": "20260904093000", "first_date": "2026-09-04"},
            MAPCB: {"first_build": "20250601093000", "first_date": "2025-06-01"}}

    def test_frame_twelve_is_still_the_predecessor(self):
        fake = FakeSocorro(
            discovery={("proto_signature", MAPCB): {MAPCB: 800, WGPU: 21}},
            builds={WGPU: [("20260904093000", 12), ("20260905093000", 9)],
                    MAPCB: _daily("2026-08-07", "2026-09-03", 20)},
        )
        fam = self._lookup(fake, WGPU, WGPU_PROTO, "Firefox", "nightly", until=_dt("2026-09-05", 20))
        self.assertEqual([p["signature"] for p in fam["predecessors"]], [MAPCB])
        self.assertEqual(fam["predecessors"][0]["relation"], "pushed-down")
        self.assertEqual(fam["family_first_seen_ever"], "20250601093000")
        # The panic machinery never became an anchor.
        for kind, anchor in fake.anchors_asked():
            self.assertFalse(anchor.startswith(("std::", "core::", "rust_begin")), anchor)
        self.assertEqual(fake.anchors_asked()[1], ("proto_signature", MAPCB))


class TestSymbolGap(_Base):
    """Bug 2070554: a Windows update's symbol gap moved every `WaitOnAddress` shutdown hang onto
    a module-named signature, on every live release build at once."""
    ever = {HANG: {"first_build": "20260903215306", "first_date": "2026-09-08"},
            WAIT1: {"first_build": "20240526221752", "first_date": "2024-05-26"},
            WAIT3: {"first_build": "20240416043247", "first_date": "2024-04-16"}}

    def fake(self):
        return FakeSocorro(
            discovery={("proto_signature", "WaitOnAddress"):
                       {WAIT1: 95826, WAIT2: 70, WAIT3: 75072, WAIT_BARE: 65, HANG_VARIANT: 3}},
            builds={HANG: [("20260903215306", 79)],
                    WAIT1: [("20260812182057", 12932), ("20260903215306", 44)],
                    WAIT2: [("20260812182057", 7)],
                    WAIT3: [("20260812182057", 10166), ("20260903215306", 42)],
                    WAIT_BARE: [("20260812182057", 10)],
                    HANG_VARIANT: [("20260903215306", 3)]},
            days={WAIT1: {"2026-08-20": 12932, "2026-09-04": 150, "2026-09-06": 140},
                  WAIT3: {"2026-08-20": 10166, "2026-09-04": 150, "2026-09-06": 148}},
        )

    def test_date_aligned_handoff_with_one_predecessor_under_four_spellings(self):
        fam = self._lookup(self.fake(), HANG, HANG_PROTO, "Firefox", "release",
                           until=_dt("2026-09-09", 12))
        self.assertEqual([p["signature"] for p in fam["predecessors"]], [WAIT1, WAIT3])
        top = fam["predecessors"][0]
        self.assertEqual((top["relation"], top["before"], top["after"], top["after_dates"]),
                         ("pushed-down", 12932, 44, 290))
        # The old name stopped on the old builds too: a DATE event, no changeset can be its cause.
        self.assertEqual(top["alignment"], "date")
        self.assertEqual(fam["alignment"], "date")
        # Four spellings of one wait: one predecessor, not a catch-all.
        self.assertEqual(fam["fan_in"], 1)
        self.assertEqual(fam["family_first_seen_ever"], "20240416043247")
        statuses = {s["signature"]: s["status"] for s in fam["siblings"]}
        self.assertEqual(statuses, {WAIT2: "undecided", HANG_VARIANT: "younger"})
        # The bare (non-hang) spelling is a different crash and never entered the family.
        self.assertNotIn(WAIT_BARE, sf.spellings(fam))
        self.assertIn("took over the name from `RtlWaitOnAddress`, `WaitOnAddress`", top["change"])


class TestBitSet(_Base):
    """Bug 2070376: filed on one report while the 3-frame spelling had five -- on another
    channel, which is why the family is discovered product-wide."""

    def test_an_other_channel_spelling_is_a_sibling_not_a_predecessor(self):
        # `mozilla::BitSet<T>` is on Socorro's prefix list, so the iterator is the anchor.
        fake = FakeSocorro(
            discovery={("signature", "js::gc::BitSetIter<T>::BitSetIter"): {BITSET4: 1, BITSET3: 5}},
            builds={BITSET4: [("20260904092011", 1)]},
        )
        fam = self._lookup(fake, BITSET4, BITSET4 + " | js::gc::AllocSpace<T>::sweep", "Firefox",
                           "nightly", until=_dt("2026-09-05"))
        self.assertEqual(fam["predecessors"], [])
        self.assertEqual([(s["signature"], s["status"], s["total_all_channels"])
                          for s in fam["siblings"]], [(BITSET3, "other_channel", 5)])
        self.assertIsNone(fam["family_first_seen_ever"])


class TestNothingToFind(_Base):
    def test_a_name_with_no_sibling(self):
        # 2061960, `nsFind::FindFromRangeBoundaries`, FIXED on a 326-day-old name: nothing here
        # may move for it.
        fake = FakeSocorro(discovery={("signature", "nsFind::FindFromRangeBoundaries"):
                                      {"nsFind::FindFromRangeBoundaries": 40}})
        fam = self._lookup(fake, "nsFind::FindFromRangeBoundaries", None, "Firefox", "nightly")
        self.assertEqual(fam["lookup"], "no_candidates")
        self.assertEqual((fam["predecessors"], fam["siblings"]), ([], []))
        self.assertEqual(len(fake.calls), 1)

    def test_ineligible_and_empty(self):
        fake = FakeSocorro()
        fam = self._lookup(fake, "AsyncShutdownTimeout | profile-before-change | X", None)
        self.assertEqual(fam["lookup"], "ineligible")
        self.assertEqual(self._lookup(fake, "", None)["lookup"], "ineligible")
        self.assertEqual(fake.calls, [])

    def test_a_signature_older_than_the_index_claims_no_handoff(self):
        # S itself first reports at the retention wall: its first build is unknowable.
        until = _dt("2026-09-17")
        wall = (until - timedelta(days=182)).strftime("%Y%m%d") + "093000"
        fake = FakeSocorro(
            discovery={("signature", CHECK): {CHECK: 100, PATCH: 7000}},
            builds={CHECK: [(wall, 3), ("20260812182057", 97)],
                    PATCH: [(str(int(wall) - 1000000), 500)]},
        )
        fam = self._lookup(fake, CHECK, CHECK_PROTO, "Firefox", "release", until=until)
        self.assertTrue(fam["at_wall"])
        self.assertEqual(fam["predecessors"], [])

    def test_no_history_on_the_channel(self):
        fake = FakeSocorro(discovery={("signature", CHECK): {PATCH: 7000}})
        fam = self._lookup(fake, CHECK, CHECK_PROTO, "Firefox", "release")
        self.assertEqual(fam["lookup"], "no_history")
        self.assertEqual(fam["predecessors"], [])
        self.assertEqual(fam["siblings"][0]["status"], "unclassified")


class TestSeedRoundTrip(unittest.TestCase):
    def test_seed_facts_and_back(self):
        fam = {"predecessors": [{"signature": PATCH, "status": "handoff"}],
               "siblings": [{"signature": "X::y", "status": "older"}],
               "s_first_build": "20260812182057", "family_first_seen_ever": "20250310180126",
               "alignment": "build", "fan_in": 1, "lookup": "ok"}
        seed = sf.seed_facts(fam)
        self.assertEqual(seed["signature_handoff_build"], "20260812182057")
        self.assertEqual(seed["signature_family_lookup"], "ok")
        self.assertEqual(sf.spellings(sf.family_from_seed(seed)), [PATCH, "X::y"])
        self.assertIsNone(sf.seed_facts({"predecessors": [], "s_first_build": "2026"})["signature_handoff_build"])
        c = {"signature_family_lookup": "ok", "signature_predecessors": [PATCH],
             "signature_siblings_live": ["X::y"], "signature_handoff_build": "20260812182057"}
        self.assertEqual(sf.spellings(sf.family_from_corroborations(c)), [PATCH, "X::y"])
        self.assertIsNone(sf.family_from_corroborations({}))
        self.assertEqual(sf.spellings(None), [])


if __name__ == "__main__":
    unittest.main()
