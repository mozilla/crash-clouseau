# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

"""Offline evaluation harness (#13): mine + freeze a corpus of clouseau-aliased
BMO bugs, re-run the crash-triage agent per case, and compute the gating metrics
(off-stack recall vs stack-only baseline, evidence precision, abstain calibration).
Re-runs drive #02's run_crash_triage per case under bounded concurrency (no Batch
API on the SDK path)."""
