# crash-clouseau
>  Tool to help to find patches which are potentially responsible of a crash

[![Build Status](https://api.travis-ci.org/mozilla/crash-clouseau.svg?branch=master)](https://travis-ci.org/mozilla/crash-clouseau)
[![codecov.io](https://img.shields.io/codecov/c/github/mozilla/crash-clouseau/master.svg)](https://codecov.io/github/mozilla/crash-clouseau?branch=master)

## See it in action

https://clouseau.moz.tools/reports.html

Results on Firefox code are tracked in a meta bug: https://bugzilla.mozilla.org/show_bug.cgi?id=1396527

## Setup

Dependencies are managed with [uv](https://docs.astral.sh/uv/). Install them with:
```sh
uv sync
```

## Running tests

```sh
uv run coverage run --source=crashclouseau -m unittest discover tests/
```

## UI Documentation

See [HOWTO](/HOWTO.md).

## Bugs

https://github.com/mozilla/crash-clouseau/issues/new

## Number of bugs reported with the tool

https://bugzilla.mozilla.org/rest/bug?count_only=1&blocks=1396527

## Contact

Email: release-mgmt@mozilla.com or calixte@mozilla.com
