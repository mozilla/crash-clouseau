/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at http://mozilla.org/MPL/2.0/. */

/*jslint es6:true*/

"use strict";


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

// Tasks view: re-run triage for one crash. A running task is cancelled first
// (server-side) so we don't pay for two runs. Analysis only -- posts nothing to
// Bugzilla.
function retriggerTask(uuid, btn) {
    if (!window.confirm(
        "Retrigger triage for " + uuid + "?\n"
        + "If it is still running, that run will be cancelled first.")) {
        return;
    }
    btn.disabled = true;
    btn.textContent = "…";
    fetch("/api/tasks/retrigger", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ uuid: uuid })
    }).then(function (r) {
        if (!r.ok) { throw new Error("HTTP " + r.status); }
        return r.json();
    }).then(function () {
        location.reload();
    }).catch(function (e) {
        window.alert("Retrigger failed: " + e);
        btn.disabled = false;
        btn.textContent = "retrigger";
    });
}
