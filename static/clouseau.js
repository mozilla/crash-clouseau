/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at http://mozilla.org/MPL/2.0/. */

/*jslint es6:true*/

"use strict";

const repoUrl = "https://hg.mozilla.org/mozilla-central";
const channel = "nightly";

let contextmenuTarget = null;

document.addEventListener("contextmenu", function(e) {
    contextmenuTarget = e.target;
});

function openChangeset(a) {
    const chgset = (a ? a.innerText : document.activeElement.innerText);
    const url = repoUrl + "/rev?node=" + chgset;
    window.open(url, "_blank");
}

function getFileInfo(a) {
    const pos = a.id.split("-").pop();
    const changeset = a.innerText;
    const filename = document.getElementById("filename-" + pos).innerText;
    const line = document.getElementById("line-" + pos).innerText;
    return {'filename': filename,
            'line': line,
            'changeset': changeset};
}

function openDiff(a) {
    const fi = getFileInfo(a ? a : document.activeElement);
    const url = repoUrl + "/diff/" + fi.changeset + "/" + fi.filename;
    window.open(url, "_blank");
}

function openAnnotateDiff(a, style="annotate") {
    const fi = getFileInfo(a ? a : document.activeElement);
    const url = "/diff.html"
              + "?filename=" + fi.filename
              + "&line=" + fi.line
              + "&node=" + NODE
              + "&changeset=" + fi.changeset
              + "&channel=" + channel
              + "&style=" + style;
    window.open(url, "_blank");
}

function openFileDiff(a) {
    openAnnotateDiff(a, "file");
}

function search() {
    const e = document.getElementById("score");
    const score = e.options[e.selectedIndex].value;
    location.href = "report.html?score=" + score;
}

function getChangesets(callback) {
    const e = contextmenuTarget.offsetParent;
    if (e.nodeName == "TD") {
        const links = Array.prototype.slice.call(e.getElementsByTagName("a"));
        links.forEach(a => {
            callback(a);
        });
    }
}

function openChangesetInTabs() {
    getChangesets(openChangeset);
}

function openDiffInTabs() {
    getChangesets(openDiff);
}

function openAnnotateDiffInTabs() {
    getChangesets(openAnnotateDiff);
}

function openFileDiffInTabs() {
    getChangesets(openFileDiff);
}

function getParams() {
    const params = ["buildid", "product", "score"].map(function(i) {
        const e = document.getElementById(i);
        return e.options[e.selectedIndex].value;
    });
    return params;
}

function setHref(date, channel, product) {
    location.href = "?date=" + date
                  + "&channel=" + channel
                  + "&product=" + product;
}

function update_reports() {
    const params = getParams();
    location.href = "reports.html?product=" + params[1]
                  + "&buildid=" + params[0]
                  + "&score=" + params[2];
}

function update_buildids() {
    const bids = document.getElementById("buildid");
    const products = document.getElementById("product");
    const prod = products.options[products.selectedIndex].value;
    const newBids = BUILDIDS[prod];
    bids.innerHTML = "";
    newBids.forEach(bid => {
        bids.options.add(new Option(bid, bid));
    });
}

function reportBug() {
    const a = document.activeElement;
    const changeset = a.innerText;
    location.href = "bug.html?changeset=" + changeset
                  + "&uuid=" + UUID;
}
