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
// (server-side) so we don't pay for two runs.
//
// IT CAN POST TO BUGZILLA. This comment used to say "Analysis only -- posts nothing to
// Bugzilla", contradicting the route's own docstring: a re-run is an ordinary run and reaches
// `_maybe_autofile`. The endpoint now requires the viewer token, which this fetch supplies as
// the `clouseau_view` cookie (set once by any `?token=` visit) -- so a 403 here means this
// browser has never presented the token, not that the run failed.
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

let refreshing = false;

function retriggerInFlight() {
    return document.querySelector(".retrigger-btn:disabled") !== null;
}

function refreshPage() {
    if (refreshing || retriggerInFlight()) {
        return;
    }
    refreshing = true;
    fetch(location.href, { cache: "no-store", redirect: "manual" }).then(function (r) {
        // An expired sign-in redirects; reload so the browser follows it.
        if (r.type === "opaqueredirect") { location.reload(); throw new Error("signed out"); }
        if (!r.ok) { throw new Error("HTTP " + r.status); }
        return r.text();
    }).then(function (text) {
        const doc = new DOMParser().parseFromString(text, "text/html");
        // Preserve button state if a retrigger started during the fetch.
        // Only accept responses with the current page title.
        if (doc.title === document.title && !retriggerInFlight()) {
            morphChildren(document.body, doc.body);
        }
    }).catch(function (e) {
        console.warn("refresh failed:", e);
    }).finally(function () {
        refreshing = false;
    });
}

// Skip hidden tabs; check for an overdue refresh when shown.
function autoRefresh(ms) {
    let last = Date.now();
    function tick() {
        if (document.hidden || Date.now() - last < ms - 1000) {
            return;
        }
        last = Date.now();
        refreshPage();
    }
    setInterval(tick, ms);
    document.addEventListener("visibilitychange", tick);
}

function morphNode(cur, next) {
    if (cur.nodeType !== next.nodeType || cur.nodeName !== next.nodeName) {
        cur.replaceWith(document.importNode(next, true));
        return;
    }
    if (cur.nodeType !== Node.ELEMENT_NODE) {
        if (cur.nodeValue !== next.nodeValue) { cur.nodeValue = next.nodeValue; }
        return;
    }
    Array.from(cur.attributes).forEach(function (a) {
        // Preserve expanded details during refresh.
        if (a.name === "open" && cur.nodeName === "DETAILS") { return; }
        if (!next.hasAttribute(a.name)) { cur.removeAttribute(a.name); }
    });
    Array.from(next.attributes).forEach(function (a) {
        if (cur.getAttribute(a.name) !== a.value) { cur.setAttribute(a.name, a.value); }
    });
    morphChildren(cur, next);
}

// Match keyed children by identity; match unkeyed children at the cursor.
function morphChildren(cur, next) {
    const keyed = new Map();
    Array.from(cur.children).forEach(function (c) {
        if (c.hasAttribute("data-key")) { keyed.set(c.getAttribute("data-key"), c); }
    });
    let ref = cur.firstChild;
    Array.from(next.childNodes).forEach(function (n) {
        const key = n.nodeType === Node.ELEMENT_NODE ? n.getAttribute("data-key") : null;
        let match = key === null ? null : keyed.get(key);
        if (match) {
            keyed.delete(key);
            if (match !== ref) { cur.insertBefore(match, ref); }
        } else if (key === null && ref
                   && !(ref.nodeType === Node.ELEMENT_NODE && ref.hasAttribute("data-key"))) {
            match = ref;
        } else {
            cur.insertBefore(document.importNode(n, true), ref);
            return;
        }
        ref = match.nextSibling;
        morphNode(match, n);
    });
    while (ref) {
        const after = ref.nextSibling;
        ref.remove();
        ref = after;
    }
}
