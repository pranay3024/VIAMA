/*
 * Extract button for the schedules pages (admin / regional / teamleader).
 *
 * Copies the Section No + Cycle No columns for whichever Team / Day / State
 * filter is currently applied. Nothing is downloaded - the button is a
 * type="button" so it never submits the surrounding form.
 *
 * Clipboard access has three tiers because the app is often reached over plain
 * http on a LAN IP, where the async Clipboard API is unavailable:
 *
 *   1. navigator.clipboard  - requires a secure context
 *   2. textarea + execCommand("copy") - works on http
 *   3. window.prompt - last resort, the user copies by hand
 *
 * The endpoint URL comes from the button's data-extract-url, and the filter
 * values are read from the form itself, so the two can never disagree.
 */
(function () {

    var btn = document.getElementById("extract-btn");

    var note = document.getElementById("extract-note");

    var form = document.querySelector(".advanced-filter");

    if (!btn || !note || !form) {
        return;
    }

    function say(message, failed) {

        note.textContent = message;

        note.className = failed
            ? "extract-note extract-note-failed"
            : "extract-note extract-note-ok";

    }

    function rows(count) {

        return count === 1 ? "1 row" : count + " rows";

    }

    function manualCopy(text) {

        var area = document.createElement("textarea");

        area.value = text;
        area.setAttribute("readonly", "");
        area.style.position = "fixed";
        area.style.left = "-9999px";

        document.body.appendChild(area);
        area.select();

        var copied = false;

        try {
            copied = document.execCommand("copy");
        } catch (err) {
            copied = false;
        }

        document.body.removeChild(area);

        return copied;

    }

    function fallback(text, count) {

        if (manualCopy(text)) {
            say("Copied " + rows(count) + ".", false);
        } else {
            window.prompt("Copy these section numbers and cycles:", text);
            say("Copy the text above.", true);
        }

    }

    function copy(text, count) {

        if (!text) {
            say("Nothing scheduled for this filter.", true);
            return;
        }

        if (navigator.clipboard && window.isSecureContext) {

            navigator.clipboard.writeText(text).then(function () {
                say("Copied " + rows(count) + ".", false);
            }, function () {
                fallback(text, count);
            });

        } else {
            fallback(text, count);
        }

    }

    btn.addEventListener("click", function () {

        var params = new URLSearchParams(
            new FormData(form)
        ).toString();

        btn.disabled = true;
        say("Collecting...", false);

        fetch(btn.getAttribute("data-extract-url") + "?" + params, {
            headers: { "Accept": "application/json" }
        })
            .then(function (response) {

                if (!response.ok) {
                    throw new Error(response.status);
                }

                return response.json();

            })
            .then(function (data) {

                copy(data.text, data.count);

            })
            .catch(function () {

                say("Could not load the schedule. Try again.", true);

            })
            .then(function () {

                btn.disabled = false;

            });

    });

})();
