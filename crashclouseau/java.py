# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import html
from libmozdata.hgmozilla import Mercurial
import re
import requests
import time
from . import models, tools


# must match 'at android.os.Parcel.readException(Parcel.java:1552)'
JAVA_PAT1 = re.compile(r"^at\ ([^\(]+)\(([^:]+):([0-9]*)\)$")
# must match $123 in MyClass$123 or MyClass$Inner
JAVA_PAT2 = re.compile(r"\$.*")
JAVA_PAT3 = re.compile(r"\([^:]+:[0-9]*\)$")
GITHUB_URL = "https://api.github.com/repos/mozilla/gecko-dev"


def parse_path(path):
    path = path.split(".")
    method = path[-1]
    # we remove the method and the class name
    path = path[:-2]
    # remove inner class stuff \$.*
    path = map(lambda x: JAVA_PAT2.sub("", x), path)
    path = "/".join(path)

    return path, method


def inspect_java_stacktrace(st, node, get_full_path=models.File.get_full_path):
    if not st:
        return [], set()

    lines = map(lambda x: x.strip(), st.split("\n"))
    lines = filter(lambda x: x.startswith("at "), lines)
    stack = []
    files = set()
    for n, line in enumerate(lines):
        m = JAVA_PAT1.match(line)
        d = {
            "original": line,
            "filename": "",
            "module": "",
            "changesets": [],
            "function": "",
            "node": "",
            "line": 0,
            "internal": False,
            "stackpos": n,
        }
        stack.append(d)
        if m:
            path, filename, linenumber = m.groups()
            if path.startswith("org.mozilla."):
                d["internal"] = True
                d["node"] = node
                d["line"] = int(linenumber)
                base_path, d["function"] = parse_path(path)
                filename = base_path + "/" + filename
                filename = get_full_path(filename)
                d["filename"] = filename
                files.add(filename)

    return stack, files


def reformat_java_stacktrace(
    st,
    channel,
    buildid,
    get_full_path=models.File.get_full_path,
    get_changeset=tools.get_changeset,
):
    if not st:
        return ""

    node = get_changeset(buildid, channel, "FennecAndroid")
    if not node:
        return html.escape(st)

    res = ""
    repo_url = Mercurial.get_repo_url(channel)
    lines = list(st.split("\n"))
    N = len(lines)
    for i in range(N):
        line = lines[i]
        m = JAVA_PAT1.match(line.strip())
        line = html.escape(line)
        added = False
        if m:
            path, filename, linenumber = m.groups()
            if path.startswith("org.mozilla."):
                base_path, _ = parse_path(path)
                repo_filename = base_path + "/" + filename
                repo_filename = get_full_path(repo_filename)
                r = '(<a href="{}/annotate/{}/{}#l{}">{}:{}</a>)'
                r = r.format(
                    repo_url, node, repo_filename, linenumber, filename, linenumber
                )
                res += JAVA_PAT3.sub(r, line)
                added = True
        if not added:
            res += line
        if i < N - 1:
            res += "\n"

    return res


def get_sha(path, filename, sleep=0.1, retry=10):
    url = "{}/contents/{}".format(GITHUB_URL, path)
    for _ in range(retry):
        r = requests.get(url)
        if r.status_code == 200:
            for data in r.json():
                if data["name"] == filename:
                    return data["sha"]
            raise Exception("Cannot get GitHub sha for {}/{}".format(path, filename))
        else:
            time.sleep(sleep)
    raise Exception("Too many attempts in java.get_sha (retry={})".format(retry))


def get_java_files(root, sha, sleep=0.1, retry=10):
    url = "{}/git/trees/{}?recursive=1".format(GITHUB_URL, sha)
    for _ in range(retry):
        r = requests.get(url)
        if r.status_code == 200:
            res = []
            for data in r.json()["tree"]:
                path = data["path"]
                if path.endswith(".java"):
                    res.append(root + "/" + path)
            return res
        else:
            time.sleep(sleep)
    raise Exception("Too many attempts in java.get_java_files (retry={})".format(retry))


def get_all_java_files(sleep=0.1, retry=10):
    # first we get the sha of directory mobile/android
    sha = get_sha("mobile", "android")

    # get the java files in the dir corresponding to the sha
    files = get_java_files("mobile/android", sha)

    return files


def populate_java_files():
    # We need to have all the java files to be able to build urls from java crash stack
    # We get them in using the GitHub API (since I didn't find out any good solution in using Mercurial one)
    files = get_all_java_files()
    models.File.populate(files)


def write_java_stack(uuid, path):
    import json
    from libmozdata import socorro

    data = socorro.ProcessedCrash.get_processed(uuid)
    data = data[uuid]
    channel = data["release_channel"]
    buildid = data["build"]

    java_st = data.get("java_stack_trace")
    jframes, files = inspect_java_stacktrace(java_st, "tip")
    reformatted = reformat_java_stacktrace(java_st, channel, buildid)

    res = {
        "stack": java_st,
        "frames": jframes,
        "files": list(sorted(files)),
        "uuid": uuid,
        "reformatted": reformatted,
        "channel": channel,
        "buildid": buildid,
    }
    with open(path, "w") as Out:
        json.dump(res, Out)


# write_java_stack('52b6dc27-6755-4ed5-8bfa-68d050180201', './tests/java/stack.1.json')
# write_java_stack('bdf532de-40ec-446d-bf55-5c4550180201', './tests/java/stack.2.json')
