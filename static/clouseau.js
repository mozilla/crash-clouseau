/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at http://mozilla.org/MPL/2.0/. */

/*jslint es6:true*/

"use strict";

const repoUrl = "https://hg.mozilla.org/mozilla-central";
const channel = "nightly";
let loaded = false;
let currentTarget = null;


function getParams() {
    const params = ["buildid", "product", "score"].map(function(i) {
        const e = document.getElementById(i);
        return e.options[e.selectedIndex].value;
    });
    return params;
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

function getOffset(el) {
    const rect = el.getBoundingClientRect(),
          scrollLeft = window.pageXOffset || document.documentElement.scrollLeft,
          scrollTop = window.pageYOffset || document.documentElement.scrollTop;
    return { top: rect.top + scrollTop + rect.height + 2, left: rect.left + scrollLeft }
}

function load(menu) {
    ["openChangeset", "openDiff", "openFileDiff", "openAnnotateDiff", "reportBug"].map(function(i) {
        document.getElementById(i).addEventListener("click", function (event) {
            menu.classList.remove("show");
            window.open(event.target.myurl, "_blank");
        }, false);
    });
    menu.addEventListener("mouseenter", function () {
        window.clearTimeout(menu.timer);
    });
    menu.addEventListener("mouseleave", function () {
        window.clearTimeout(menu.timer);
        menu.classList.remove("show");
    });
}

function showChangesetMenu(e) {
    const menu = document.getElementById("changeset-menu");
    const target = e.target;
    if (!loaded) {
        load(menu);
        loaded = true;
    }
    if ((target === currentTarget) && menu.classList.contains("show")) {
        currentTarget = null;
        menu.classList.remove("show");
        return false;
    }
    
    currentTarget = target;
    const info = target.id.split("-");
    const changeset = info[1];
    const pos = info[2];
    const filename = document.getElementById("filename-" + pos).innerText;
    const line = document.getElementById("line-" + pos).innerText;

    document.getElementById("openChangeset").myurl = repoUrl + "/rev?node=" + changeset;
    document.getElementById("openDiff").myurl = repoUrl + "/diff/" + changeset + "/" + filename;
    const diffUrl = "/diff.html?filename=" + filename
                  + "&line=" + line
                  + "&node=" + NODE
                  + "&changeset=" + changeset
                  + "&channel=" + channel
                  + "&style=";
    document.getElementById("openFileDiff").myurl = diffUrl + "file";
    document.getElementById("openAnnotateDiff").myurl = diffUrl + "annotate";
    document.getElementById("reportBug").myurl = "bug.html?changeset=" + changeset + "&uuid=" + UUID;

    const offset = getOffset(target);
    menu.style.left = offset.left + "px";
    menu.style.top = offset.top + "px";
    
    menu.timer = window.setTimeout(function () {
        menu.classList.remove("show");
    }, 3000);
    menu.classList.add("show");

    return false;
}

function openPushlog() {
    const params = getParams();
    const url = "/pushlog.html?buildid=" + params[0]
              + "&product=" + params[1]
              + "&channel=" + "nightly";
    window.open(url, "_blank");
}
