# crash-clouseau
>  Tool to help to find patches which are potentially responsible of a crash

[![Build Status](https://api.travis-ci.org/mozilla/crash-clouseau.svg?branch=master)](https://travis-ci.org/mozilla/crash-clouseau)
[![codecov.io](https://img.shields.io/codecov/c/github/mozilla/crash-clouseau/master.svg)](https://codecov.io/github/mozilla/crash-clouseau?branch=master)

## See it in action

https://clouseau.moz.tools/reports.html

Results on Firefox code are tracked in a meta bug: https://bugzilla.mozilla.org/show_bug.cgi?id=1396527

## Setup

Dependencies are managed with [uv](https://docs.astral.sh/uv/). Install uv, then:
```sh
uv sync
```
This creates a local `.venv` (Python from `.python-version`) with the app
dependencies plus the dev group (coverage, flake8, honcho). Run project commands
through uv, e.g. `uv run python -m crashclouseau.worker`.

On Heroku the same `pyproject.toml` + `uv.lock` are installed by the buildpack
(`uv sync --locked --no-default-groups`, so the dev group is skipped).

## Running tests

```sh
uv run coverage run --source=crashclouseau -m unittest discover tests/
```

That runs on sqlite and **silently skips ~60 tests** — the Postgres-only ones, which are
exactly the tables most of the recent work touches (the `filed_bug` JSONB the autofiler
writes, the dossier/verdict round-trips, the heartbeat and reaper, the untriaged sweep's
candidate selection, the selection log, and the beta window/merge-push fixtures). They skip
rather than fail, so a green run means less than it looks like. To run the whole suite:

```sh
docker run -d --rm --name clouseau_test_pg \
  -e POSTGRES_USER=clouseau -e POSTGRES_PASSWORD=passwd -e POSTGRES_DB=clouseau_test \
  -p 55432:5432 postgres
docker run -d --rm --name clouseau_test_redis -p 6379:6379 redis

DATABASE_URL=postgresql://clouseau:passwd@localhost:55432/clouseau_test \
  REDIS_URL=redis://localhost:6379/0 \
  uv run python -m unittest discover tests/
```

Both env vars matter. `crashclouseau/__init__.py` builds a Flask app at import time, so
without `DATABASE_URL` three modules fail at *import* (`sqlalchemy.exc.ArgumentError:
Could not parse SQLAlchemy URL`) and the ~135 tests in them never load at all.

## UI Documentation

See [HOWTO](/HOWTO.md).

## Bugs

https://github.com/mozilla/crash-clouseau/issues/new

## Number of bugs reported with the tool

https://bugzilla.mozilla.org/rest/bug?count_only=1&blocks=1396527

## Contact

Email: release-mgmt@mozilla.com or calixte@mozilla.com
