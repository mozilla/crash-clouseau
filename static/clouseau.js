/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at http://mozilla.org/MPL/2.0/. */

/*jslint es6:true*/

"use strict";

let loaded = false;
let currentTarget = null;


function getParams() {
    const params = ["buildid", "product", "channel"].map(function(i) {
        const e = document.getElementById(i);
        return e.options[e.selectedIndex].value;
    });
    return params;
}

function update_reports(noscore) {
    const params = getParams();
    location.href = "reports" + (noscore ? "_no_score" : "") + ".html?product=" + params[1]
                  + "&channel=" + params[2]
                  + "&buildid=" + params[0];
}

function update_channels(product, prevChannel, channels) {
    const newChannels = BUILDIDS[product];
    channels.innerHTML = "";
    for (let c in newChannels) {
        channels.options.add(new Option(c, c));
        if (c === prevChannel) {
            channels.value = c;
        }
    }
}

function update_buildids(product, channel, bids) {
    const newBids = BUILDIDS[product][channel];
    if (newBids) {
        bids.innerHTML = "";
        newBids.forEach(x => {
            const buildid = x[0];
            const version = x[1];
            bids.options.add(new Option(buildid + " (" + version + ")", buildid));
        });
    }
}

function update_selects(type) {
    const bids = document.getElementById("buildid");
    const products = document.getElementById("product");
    const channels = document.getElementById("channel");
    const prod = products.options[products.selectedIndex].value;
    let chan = channels.options[channels.selectedIndex].value;
    
    if (type === 'product') {
        update_channels(prod, chan, channels);
        chan = channels.options[channels.selectedIndex].value;
    }

    update_buildids(prod, chan, bids);
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

    document.getElementById("openChangeset").myurl = REPOURL + "/rev?node=" + changeset;
    document.getElementById("openDiff").myurl = REPOURL + "/diff/" + changeset + "/" + filename;
    const diffUrl = "/diff.html?filename=" + filename
                  + "&line=" + line
                  + "&node=" + NODE
                  + "&changeset=" + changeset
                  + "&channel=" + CHANNEL
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
              + "&channel=" + params[2];
    window.open(url, "_blank");
}

function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
}

// Evidence panel (#12): apply the human-confirmed subset of recorded Bugzilla
// actions. Nothing is posted until the explicit confirm() below is accepted.
function applyActions() {
    const boxes = document.querySelectorAll(".apply-cb:checked");
    const indices = Array.from(boxes).map(b => parseInt(b.value, 10));
    if (!indices.length) {
        window.alert("Select at least one recorded action to apply.");
        return;
    }
    if (!window.confirm(
        "This will POST to Bugzilla for the selected recorded action(s): "
        + "add the recorded comment and/or set the recorded needinfo flag on the "
        + "referenced bug(s). It cannot be undone from here. Continue?")) {
        return;
    }
    const btn = document.getElementById("applyActionsBtn");
    if (btn) { btn.disabled = true; }
    fetch("/api/evidence/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ uuid: UUID, indices: indices })
    }).then(r => r.json()).then(function (data) {
        renderApplyResults(data);
    }).catch(function (e) {
        window.alert("Apply failed: " + e);
        if (btn) { btn.disabled = false; }
    });
}

function renderApplyResults(data) {
    const el = document.getElementById("apply-results");
    if (!el) { return; }
    const results = (data && data.results) || [];
    const items = results.map(function (r) {
        let line;
        if (r.ok) {
            line = "✔ action #" + r.index + " (" + (r.type || "?") + ")";
            if (r.result_id) { line += " → " + r.result_id; }
            if (r.skipped) { line += " [" + r.skipped + "]"; }
            if (r.draft_url) { line += " — draft: " + r.draft_url; }
        } else {
            line = "✗ action #" + r.index + " (" + (r.type || "?") + "): "
                 + (r.error || "failed");
        }
        return "<li>" + escapeHtml(line) + "</li>";
    });
    el.innerHTML = items.length ? "<ul class=\"apply-results\">" + items.join("") + "</ul>"
                                : "<p>No actions applied.</p>";
    // Disable the now-applied checkboxes so a second click is a no-op locally too.
    document.querySelectorAll(".apply-cb:checked").forEach(function (b) {
        b.checked = false;
        b.disabled = true;
    });
}

// Preserved enter_bug draft (#12): open the human-filled new-bug draft. Files
// nothing until the human submits the Bugzilla form.
function draftBug(node) {
    if (!window.confirm(
        "Open a pre-filled NEW-bug draft for changeset " + node
        + "? Nothing is filed until you submit the Bugzilla form.")) {
        return;
    }
    location.href = "bug.html?changeset=" + node + "&uuid=" + UUID;
}
