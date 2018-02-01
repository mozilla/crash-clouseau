/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at http://mozilla.org/MPL/2.0/. */

"use strict";

async function fetchStack(channel, buildid, stack) {
    //const clouseau = "https://localhost:5001";
    const clouseau = "https://crash-clouseau.herokuapp.com";
    const url = clouseau + "/api/javast";
    const data = JSON.stringify({"channel": channel, "buildid": buildid, "stack": stack});
    const response = await fetch(url, {
        method: "POST",
        headers: {
            'Accept': 'application/json, text/plain, */*',
            'Content-Type': 'application/json'
        },
        body: data
    });
    return await response.json();
}


let channel = "";
let buildid = "";
const ths = Array.prototype.slice.call(document.getElementsByTagName("th"));
ths.some(th => {
    if (th.innerText === "Java Stack Trace") {
        if (buildid && channel) {
            const pre = th.nextElementSibling.getElementsByTagName("pre")[0];
            if (pre) {
                const stack = pre.innerText;
                fetchStack(channel, buildid, stack).then(data => {
                    pre.innerText = "";
                    pre.innerHTML = data["stack"];
                });
            }
            return true;
        }
    } else if (th.innerText === "Release Channel") {
        const td = th.nextElementSibling;
        if (td) {
            channel = td.innerText;
        }
    } else if (th.innerText === "Build ID") {
        const td = th.nextElementSibling;
        if (td) {
            buildid = td.innerText;
        }
    }

    return false;
});
