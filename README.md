# crash-clouseau
>  Tool to help to find patches which are potentially responsible of a crash

[![Build Status](https://api.travis-ci.org/mozilla/crash-clouseau.svg?branch=master)](https://travis-ci.org/mozilla/crash-clouseau)
[![codecov.io](https://img.shields.io/codecov/c/github/mozilla/crash-clouseau/master.svg)](https://codecov.io/github/mozilla/crash-clouseau?branch=master)

## See it in action

https://crash-clouseau.herokuapp.com/reports.html

Results on Firefox code are tracked in a meta bug: https://bugzilla.mozilla.org/show_bug.cgi?id=1396527

## Setup

Install the prerequisites via `pip`:
```sh
sudo pip install -r requirements.txt
```

## Running tests

Install test prerequisites via `pip`:
```sh
sudo pip install -r test-requirements.txt
```

Run tests:
```sh
coverage run --source=crashclouseau -m unittest discover tests/
```

## UI Documentation

See [HOWTO](/HOWTO.md).

## Bugs

https://github.com/mozilla/crash-clouseau/issues/new

## Contact

Email: release-mgmt@mozilla.com or calixte@mozilla.com
