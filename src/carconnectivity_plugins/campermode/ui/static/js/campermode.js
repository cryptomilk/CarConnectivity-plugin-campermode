// CamperMode plugin — dashboard polling and control logic
(function () {
    "use strict";

    function formatDuration(minutes) {
        const h = Math.floor(minutes / 60);
        const m = minutes % 60;
        if (h > 0 && m > 0) return h + "h " + m + "min";
        if (h > 0) return h + "h";
        return minutes + "min";
    }

    function formatSeconds(seconds) {
        if (seconds <= 0) return "0s";
        const h = Math.floor(seconds / 3600);
        const m = Math.floor((seconds % 3600) / 60);
        const s = seconds % 60;
        if (h > 0 && m > 0) return h + "h " + m + "min";
        if (h > 0) return h + "h";
        if (m > 0 && s > 0) return m + "min " + s + "s";
        if (m > 0) return m + "min";
        return s + "s";
    }

    // Set to true the moment the user interacts with any start-form control.
    // syncControls skips the update while this is true so polling never
    // clobbers unsaved edits.
    let userEdited = false;
    let saveTimer = null;
    let editGeneration = 0;

    function getControlValues() {
        const batterySlider = document.getElementById("min_battery_level");
        const endlessCheck = document.getElementById("endless");
        const durationSlider = document.getElementById("total_duration_minutes");
        const intervalRadio = document.querySelector(
            'input[name="minutes_between_cycles"]:checked'
        );
        return {
            min_battery_level: batterySlider ? parseInt(batterySlider.value, 10) : 0,
            minutes_between_cycles: intervalRadio ? parseInt(intervalRadio.value, 10) : 0,
            total_duration_minutes: durationSlider ? parseInt(durationSlider.value, 10) : 0,
            endless: endlessCheck ? endlessCheck.checked : false,
        };
    }

    function showSaveIndicator(success) {
        const el = document.getElementById("save-indicator");
        if (!el) return;
        el.textContent = success ? "Settings saved" : "Save failed";
        el.classList.remove(
            "text-success",
            "text-danger",
            "invisible"
        );
        el.classList.add(success ? "text-success" : "text-danger");
        setTimeout(function () {
            el.classList.add("invisible");
        }, 2000);
    }

    function doSave() {
        const csrfInput = document.querySelector('input[name="csrf_token"]');
        const csrfToken = csrfInput ? csrfInput.value : "";
        const myGeneration = editGeneration;
        fetch("/api/settings", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": csrfToken,
            },
            body: JSON.stringify(getControlValues()),
        })
            .then(function (resp) {
                if (resp.ok && editGeneration === myGeneration) {
                    userEdited = false;
                    showSaveIndicator(true);
                } else if (!resp.ok) {
                    showSaveIndicator(false);
                }
            })
            .catch(function () {
                showSaveIndicator(false);
            });
    }

    function scheduleSave() {
        clearTimeout(saveTimer);
        saveTimer = setTimeout(doSave, 2000);
    }

    function initControls() {
        const batterySlider = document.getElementById("min_battery_level");
        const batteryLabel = document.getElementById("battery-label");
        if (batterySlider && batteryLabel) {
            batterySlider.addEventListener("input", function () {
                batteryLabel.textContent = this.value;
                editGeneration++;
                userEdited = true;
                scheduleSave();
            });
        }

        const durationSlider = document.getElementById("total_duration_minutes");
        const durationLabel = document.getElementById("duration-label");
        const endlessCheck = document.getElementById("endless");

        if (durationSlider && durationLabel) {
            durationSlider.addEventListener("input", function () {
                durationLabel.textContent = this.value;
                editGeneration++;
                userEdited = true;
                scheduleSave();
            });
        }
        if (endlessCheck && durationSlider) {
            endlessCheck.addEventListener("change", function () {
                durationSlider.disabled = this.checked;
                editGeneration++;
                userEdited = true;
                scheduleSave();
            });
        }

        document
            .querySelectorAll('input[name="minutes_between_cycles"]')
            .forEach(function (el) {
                el.addEventListener("change", function () {
                    userEdited = true;
                    scheduleSave();
                });
            });
    }

    function syncControls(settings) {
        if (userEdited) return;
        const batterySlider = document.getElementById("min_battery_level");
        const batteryLabel = document.getElementById("battery-label");
        if (batterySlider && batteryLabel) {
            batterySlider.value = settings.min_battery_level;
            batteryLabel.textContent = settings.min_battery_level;
        }

        const radio = document.getElementById(
            "interval_" + settings.minutes_between_cycles
        );
        if (radio) radio.checked = true;

        const durationSlider = document.getElementById("total_duration_minutes");
        const durationLabel = document.getElementById("duration-label");
        const endlessCheck = document.getElementById("endless");
        if (durationSlider && durationLabel && endlessCheck) {
            endlessCheck.checked = settings.endless;
            durationSlider.disabled = settings.endless;
            if (!settings.endless) {
                durationSlider.value = settings.total_duration_minutes;
                durationLabel.textContent = settings.total_duration_minutes;
            }
        }
    }

    function updateUI(data) {
        const state = data.state;
        const settings = data.settings;
        const phase = state.current_phase;

        // Toggle start/stop sections
        const startSection = document.getElementById("start-section");
        const stopSection = document.getElementById("stop-section");
        if (startSection) startSection.classList.toggle("d-none", state.active);
        if (stopSection) stopSection.classList.toggle("d-none", !state.active);

        // Status card border
        const card = document.getElementById("status-card");
        if (card) {
            card.classList.remove("border-danger", "border-warning");
            if (phase === "heating") card.classList.add("border-danger");
            else if (phase === "paused") card.classList.add("border-warning");
        }

        // Phase label
        const phaseEl = document.getElementById("status-phase");
        if (phaseEl) {
            let html;
            if (phase === "heating") {
                html =
                    '<span class="text-danger">' +
                    '<i class="bi bi-thermometer-high me-1"></i>Heating</span>';
            } else if (phase === "paused") {
                html =
                    '<span class="text-warning">' +
                    '<i class="bi bi-pause-circle me-1"></i>Paused</span>';
            } else {
                html =
                    '<span class="text-secondary">' +
                    '<i class="bi bi-moon-stars me-1"></i>Idle</span>';
            }
            phaseEl.innerHTML = html;
        }

        // Status details visibility
        const detailsEl = document.getElementById("status-details");
        if (detailsEl) detailsEl.classList.toggle("d-none", !state.active);

        const cycleEl = document.getElementById("status-cycle");
        if (cycleEl) cycleEl.textContent = state.cycle_number;

        const phaseRemEl = document.getElementById("status-phase-remaining");
        if (phaseRemEl) {
            phaseRemEl.textContent = formatSeconds(state.phase_remaining_seconds);
        }

        const totalRemEl = document.getElementById("status-total-remaining");
        if (totalRemEl) {
            totalRemEl.textContent = settings.endless
                ? "\u221e"
                : formatSeconds(state.total_remaining_seconds);
        }

        // Battery
        const batteryEl = document.getElementById("status-battery");
        if (batteryEl) {
            batteryEl.textContent =
                state.current_battery_level !== null
                    ? state.current_battery_level + "%"
                    : "\u2014";
        }

        // Stopped reason
        const stoppedDiv = document.getElementById("status-stopped-reason");
        const stoppedText = document.getElementById("status-stopped-text");
        if (stoppedDiv && stoppedText) {
            const hasReason =
                state.stopped_reason !== null && state.stopped_reason !== "";
            stoppedDiv.classList.toggle(
                "d-none",
                !hasReason || state.active
            );
            stoppedText.textContent = state.stopped_reason || "";
        }

        // Warning banner
        const warnEl = document.getElementById("warning-banner");
        if (warnEl) warnEl.classList.toggle("d-none", !data.show_warning);

        // Sync controls only when idle (avoid clobbering unsaved user changes)
        if (phase === "idle") syncControls(settings);
    }

    function pollStatus() {
        fetch("/api/status")
            .then(function (resp) {
                if (!resp.ok) return null;
                return resp.json();
            })
            .then(function (data) {
                if (data) updateUI(data);
            })
            .catch(function () {
                // Network error — silent, will retry on next poll
            });
    }

    document.addEventListener("DOMContentLoaded", function () {
        if (!document.getElementById("status-card")) return;
        initControls();
        pollStatus();
        setInterval(pollStatus, 5000);

        const startForm = document.querySelector(
            'form[action*="start"]'
        );
        if (startForm) {
            startForm.addEventListener("submit", function () {
                clearTimeout(saveTimer);
            });
        }
    });
}());
