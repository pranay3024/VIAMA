/*
 * Progress + single-instance guard for the captain's survey-form PDF upload.
 *
 * Finishing a survey takes 5-10s on a good connection and much longer on a bad
 * one, because the POST does not end at the upload: the server then optimises
 * the PDF with pikepdf, pushes it to Drive and runs a Gemini pass over it. A
 * captain staring at a dead button has no idea whether it is working, so they
 * tap again - and every tap used to leave another copy in Drive and another
 * row in the survey's PDF history.
 *
 * So this does two things:
 *
 *   1. Only one upload at a time. `running` is the single gate; the button is
 *      disabled and every later submit is ignored while it is set. The server
 *      (routes/captain.py, pdf_versions.is_same_as_current) independently drops
 *      an identical resend, so a retry that slips past this still cannot
 *      produce a duplicate.
 *
 *   2. Real progress, and an honest label for the part we cannot measure.
 *      `upload.onprogress` gives true bytes-sent, so the bar and the "N seconds
 *      left" figure are measured, not guessed. Once the last byte is sent the
 *      server work starts and the browser cannot see it, so the bar switches to
 *      a moving stripe and the text counts elapsed seconds instead of claiming
 *      a remaining time it cannot know. Say what is happening rather than
 *      showing a bar frozen at 100%.
 *
 * Progressive enhancement: the form still posts normally with JavaScript off.
 * The route only returns JSON when it sees the X-Requested-With header this
 * script sets, so a plain submit keeps its ordinary redirect.
 */
(function () {

    var form = document.getElementById("finishSurveyForm")
        || document.getElementById("reuploadForm");

    if (!form) {
        return;
    }

    var button = document.getElementById("nextBtn")
        || document.getElementById("uploadBtn");

    var panel = document.getElementById("pdfUploadPanel");

    var bar = document.getElementById("pdfUploadBar");

    var fill = document.getElementById("pdfUploadFill");

    var caption = document.getElementById("pdfUploadCaption");

    var idleLabel = button ? button.innerHTML : "";

    // The one gate. Set before the request starts, cleared only when it fails.
    var running = false;

    var timer = null;

    if (!window.XMLHttpRequest || !window.FormData) {
        return;
    }

    // Tells the template's own submit handler to leave the button alone; it
    // otherwise disables it as a no-JS fallback.
    window.pdfUploadProgressReady = true;

    function show() {

        if (panel) {
            panel.hidden = false;
        }

    }

    function setBar(percent) {

        if (fill) {
            fill.style.width = percent + "%";
        }

    }

    function stripe(on) {

        if (bar) {
            bar.classList.toggle("is-indeterminate", on);
        }

    }

    function say(text) {

        if (caption) {
            caption.textContent = text;
        }

    }

    function stopTimer() {

        if (timer) {
            clearInterval(timer);
            timer = null;
        }

    }

    function lock(label) {

        if (button) {
            button.disabled = true;
            button.classList.add("is-uploading");
            button.innerHTML = label || idleLabel;
        }

    }

    function release() {

        if (button) {

            button.classList.remove("is-uploading");

            // The page owns the button's label and disabled state - on
            // recording.html that is driven by the checklist and photo, so hand
            // control back rather than restoring a label captured at load.
            if (typeof window.surveyFormRefresh === "function") {
                window.surveyFormRefresh();
            } else {
                button.disabled = false;
                button.innerHTML = idleLabel;
            }

        }

    }

    function fail(message) {

        stopTimer();
        stripe(false);
        running = false;
        release();

        show();

        say(message);
        setBar(100);

        if (panel) {
            panel.classList.add("is-error");
        }

    }

    /*
     * Seconds left, from bytes sent so far against bytes still to go.
     *
     * `lengthComputable` is false on some mobile browsers, in which case
     * there is no total to work with and the caller shows a plain "Uploading"
     * instead of a number we would have had to invent.
     */
    function secondsLeft(loaded, total, startedAt) {

        if (!total || loaded < 1) {
            return null;
        }

        var elapsed = (Date.now() - startedAt) / 1000;

        if (elapsed < 1) {
            return null;
        }

        var perSecond = loaded / elapsed;

        if (perSecond < 1) {
            return null;
        }

        var seconds = Math.ceil((total - loaded) / perSecond);

        return seconds > 0 ? seconds : 0;

    }

    function humanSize(bytes) {

        if (bytes >= 1048576) {
            return Math.round(bytes / 1048576) + " MB";
        }

        return Math.round(bytes / 1024) + " KB";

    }

    /*
     * The bytes are on the server; now Drive and Gemini run. No signal exists
     * for that, so count up and name the step rather than faking progress.
     */
    function startProcessing(startedAt) {

        stripe(true);
        setBar(100);

        var seconds = Math.floor((Date.now() - startedAt) / 1000);

        function tick() {

            seconds += 1;

            if (seconds === 1) {
                say("Uploaded. Saving and reading the form...");
            } else if (seconds > 8 && seconds % 5 === 0) {
                say("Uploaded. Still reading the form (" + seconds + "s)...");
            } else {
                say("Uploaded. Saving and reading the form (" + seconds + "s)...");
            }

        }

        tick();

        timer = setInterval(tick, 1000);

    }

    function finish(payload) {

        stopTimer();
        stripe(false);
        running = false;

        if (panel) {
            panel.classList.add("is-done");
        }

        if (button) {
            button.disabled = true;
            button.innerHTML = "✔ DONE";
        }

        say(payload.message || "Saved.");

        var target = payload.redirect
            || document.body.getAttribute("data-redirect-fallback")
            || "/captain-home";

        setTimeout(function () {
            window.location.href = target;
        }, 600);

    }

    form.addEventListener("submit", function (event) {

        // Already in flight - swallow the extra tap rather than queueing it.
        if (running) {
            event.preventDefault();
            return;
        }

        // Leave the other guards on the page in charge of saying no.
        if (typeof window.surveyFormBlocked === "function"
            && window.surveyFormBlocked()) {
            return;
        }

        var file = document.getElementById("survey_pdf");

        if (!file || !file.files || !file.files.length) {
            return;
        }

        event.preventDefault();

        running = true;

        lock("⏳ UPLOADING...");

        show();
        stripe(false);

        if (panel) {
            panel.classList.remove("is-error", "is-done");
        }

        var total = file.files[0].size;

        say("Uploading " + humanSize(total) + "...");

        setBar(2);

        var startedAt = Date.now();

        var request = new XMLHttpRequest();

        request.open("POST", form.getAttribute("action") || window.location.href, true);

        // Tells routes/captain.py to answer with JSON instead of a redirect.
        request.setRequestHeader("X-Requested-With", "XMLHttpRequest");

        request.upload.addEventListener("progress", function (event) {

            if (!event.lengthComputable || !event.total) {
                say("Uploading " + humanSize(total) + "...");
                return;
            }

            var percent = Math.round((event.loaded / event.total) * 100);

            setBar(Math.max(percent, 2));

            var left = secondsLeft(event.loaded, event.total, startedAt);

            if (left === null) {
                say("Uploading " + percent + "%");
            } else if (left === 0) {
                say("Uploading " + percent + "% - almost there");
            } else {
                say("Uploading " + percent + "% - about "
                    + left + (left === 1 ? " second" : " seconds") + " left");
            }

        });

        // Fires once the body is on the server and the server work begins.
        request.upload.addEventListener("load", function () {
            startProcessing(startedAt);
        });

        request.addEventListener("load", function () {

            var payload = null;

            try {
                payload = JSON.parse(request.responseText);
            } catch (err) {
                payload = null;
            }

            // Only the route's explicit JSON acknowledgement means the PDF
            // was saved. HTML can be a login page or a server error response.
            if (!payload || typeof payload !== "object"
                || request.status < 200 || request.status >= 300) {
                fail(payload && payload.message
                    || "The server did not confirm the PDF upload (HTTP "
                        + request.status
                        + "). Check Pending Uploads before retrying.");
                return;
            }

            if (payload.ok) {
                finish(payload);
                return;
            }

            fail(payload.message || "Could not save the PDF. Please try again.");

        });

        request.addEventListener("error", function () {
            fail("Upload failed. Check your connection and try again.");
        });

        request.addEventListener("abort", function () {
            fail("Upload cancelled.");
        });

        request.addEventListener("timeout", function () {
            fail("Upload timed out. Please try again.");
        });

        request.send(new FormData(form));

    });

})();