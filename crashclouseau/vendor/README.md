# Vendored hackbot substrate

Import-rewritten subset of **mozilla/bugbug** (`libs/hackbot-runtime`,
`libs/agent-tools`), commit `26cf71eeb8f1f04154812abc6c0ee679a71baafa`
(vendored 2026-07-03 for Clouseau plan #02).

## Why vendored
hackbot is not published to PyPI as installable libs; a pinned copy is the
substrate for Clouseau's read-only crash-triage agent (#02). Re-sync
deliberately against a newer bugbug commit (re-run the copy + import rewrite).

## Keep set (import-closed for read-only triage + recorded needinfo)
- `hackbot_runtime/`: `results`, `errors`, `uploader`, `artifacts`, `claude`,
  `actions/{__init__, recorder, bugzilla, claude_sdk}` + a trimmed `__init__`.
- `agent_tools/`: `registry`, `claude_sdk` + a trimmed `__init__`.

## Dropped (unneeded; would drag google-auth / pydantic-settings / SystemExit)
`runtime` (a `NoReturn` `SystemExit` entry point), `anthropic_wif` (GCP WIF),
`context`, `config`, `providers`, `source`, `changes`. Clouseau runs its own
`run_crash_triage` coroutine (`crashclouseau/agent/triage.py`) instead of
`hackbot_runtime.run` / `run_async`, and builds `ActionsRecorder` directly.

## Import rewrite (the only change to upstream code)
`hackbot_runtime.*` -> `crashclouseau.vendor.hackbot_runtime.*`
`agent_tools.*`     -> `crashclouseau.vendor.agent_tools.*`
(sed, anchored to line start.)

## Deps
`pydantic>=2.6`, `claude-agent-sdk>=0.2` (imported only by `claude.py` and the
two `claude_sdk.py` modules), `requests` (by `uploader.py`; already a Clouseau
dep). NOT needed: `pydantic-settings`, `google-auth`, `anthropic`.
