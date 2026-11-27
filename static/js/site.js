/* CivicDesk client enhancements. Every page works without JavaScript.
   Zero network requests. Leave quickly (weather.gov + triple-Shift) is strictly
   scoped to pages that render a [data-quick-exit] control (/id-records/* and
   domestic-safety exemption callouts). */
(function () {
  "use strict";
  var root = document.documentElement;
  var KEYS = {
    remember: "civicdesk:remember",
    prefs: "civicdesk:prefs",
    checks: "civicdesk:checks"
  };

  function store(action, key, value) {
    try {
      if (action === "get") return window.localStorage.getItem(key);
      if (action === "set") window.localStorage.setItem(key, value);
      if (action === "remove") window.localStorage.removeItem(key);
    } catch (e) {
      return null;
    }
    return null;
  }
  function remembering() { return store("get", KEYS.remember) === "on"; }

  function announce(text) {
    var el = document.getElementById("announcer");
    if (!el) return;
    el.textContent = "";
    window.setTimeout(function () { el.textContent = text; }, 50);
  }

  /* ---------- Dated notices (e.g. Dec 31, 2026 retroactive bills cutoff) ---------- */
  function localTodayIso() {
    var override = root.getAttribute("data-today");
    if (override && /^\d{4}-\d{2}-\d{2}$/.test(override)) return override;
    var now = new Date();
    var y = String(now.getFullYear());
    var m = String(now.getMonth() + 1).padStart(2, "0");
    var d = String(now.getDate()).padStart(2, "0");
    return y + "-" + m + "-" + d;
  }
  var todayIso = localTodayIso();
  document.querySelectorAll("[data-show-until]").forEach(function (el) {
    var until = el.getAttribute("data-show-until");
    if (until && todayIso > until) {
      el.hidden = true;
    }
  });

  /* ---------- Reading settings ---------- */
  var DEFAULTS = { theme: "auto", size: "m", spacing: "normal", font: "system", brief: "off" };

  function current() {
    var out = {};
    Object.keys(DEFAULTS).forEach(function (k) {
      out[k] = root.getAttribute("data-" + k) || DEFAULTS[k];
    });
    return out;
  }
  function isDefaultPrefs(prefs) {
    return Object.keys(DEFAULTS).every(function (k) { return (prefs[k] || DEFAULTS[k]) === DEFAULTS[k]; });
  }
  function apply(prefs) {
    Object.keys(prefs).forEach(function (k) { root.setAttribute("data-" + k, prefs[k]); });
  }
  function savePrefs(prefs) {
    if (isDefaultPrefs(prefs)) {
      store("remove", KEYS.prefs);
    } else {
      store("set", KEYS.prefs, JSON.stringify(prefs));
    }
  }

  var dialog = document.getElementById("settings");
  var opener = document.getElementById("settings-open");
  var closer = document.getElementById("settings-close");
  var closerX = document.getElementById("settings-close-x");

  function syncForm() {
    if (!dialog) return;
    var p = current();
    dialog.querySelectorAll('input[name="size"]').forEach(function (el) { el.checked = el.value === p.size; });
    dialog.querySelectorAll('input[name="theme"]').forEach(function (el) { el.checked = el.value === p.theme; });
    var spacing = dialog.querySelector('input[name="spacing"]');
    if (spacing) spacing.checked = p.spacing === "wide";
    var font = dialog.querySelector('input[name="font"]');
    if (font) font.checked = p.font === "hyper";
    var brief = dialog.querySelector('input[name="brief"]');
    if (brief) brief.checked = p.brief === "on";
    var remember = dialog.querySelector('input[name="remember"]');
    if (remember) remember.checked = remembering();
  }

  /* ---------- Short version toggle strip ---------- */
  var briefToggle = document.getElementById("brief-toggle");
  function setBrief(on, message, persist) {
    var p = current();
    p.brief = on ? "on" : "off";
    apply(p);
    if (persist !== false || store("get", KEYS.prefs) !== null) {
      savePrefs(p);
    }
    syncForm();
    announce(message || (on
      ? "Short version. Extra sections on this page are now hidden."
      : "Full version. All sections on this page are showing."));
  }
  if (briefToggle) {
    briefToggle.addEventListener("click", function () {
      var before = briefToggle.getBoundingClientRect().top;
      setBrief(current().brief !== "on", null, true);
      var moved = briefToggle.getBoundingClientRect().top - before;
      if (moved) window.scrollBy(0, moved);
    });
  }
  syncForm();

  /* ---------- Checklists ---------- */
  var checks = {};
  if (remembering()) {
    try { checks = JSON.parse(store("get", KEYS.checks) || "{}") || {}; } catch (e) { checks = {}; }
  }
  function saveChecks() {
    if (!remembering()) return;
    if (Object.keys(checks).length > 0) {
      store("set", KEYS.checks, JSON.stringify(checks));
    } else {
      store("remove", KEYS.checks);
    }
  }
  function markStep(box) {
    if (!box.closest(".step-head")) return;
    var step = box.closest(".step");
    if (step) step.classList.toggle("is-done", box.checked);
  }
  var checkBoxes = Array.prototype.slice.call(document.querySelectorAll("input[data-check]"));
  var checklistHintEl = null;
  function ensureChecklistHint() {
    if (checklistHintEl || !checkBoxes.length) return checklistHintEl;
    var firstContainer = checkBoxes[0].closest(".steps, .checklist");
    if (!firstContainer || !firstContainer.parentNode) return null;
    checklistHintEl = document.createElement("p");
    checklistHintEl.className = "checklist-persistence-hint js-only";
    checklistHintEl.setAttribute("role", "status");
    checklistHintEl.hidden = true;
    firstContainer.parentNode.insertBefore(checklistHintEl, firstContainer);
    return checklistHintEl;
  }
  function syncChecklistHint(interacted) {
    var anyChecked = checkBoxes.some(function (b) { return b.checked; });
    if (!anyChecked || remembering()) {
      if (checklistHintEl) {
        checklistHintEl.hidden = true;
        checklistHintEl.textContent = "";
      }
      return;
    }
    if (!interacted && !checklistHintEl) return;
    var hint = ensureChecklistHint();
    if (hint) {
      hint.textContent = "Checkmarks stay on this page only while it is open. Turn on \"Remember my checklist on this device\" in Reading settings to save them here.";
      hint.hidden = false;
    }
  }
  checkBoxes.forEach(function (box) {
    var key = box.getAttribute("data-check");
    if (!key) return;
    if (checks[key]) box.checked = true;
    markStep(box);
    box.addEventListener("change", function () {
      if (box.checked) { checks[key] = true; } else { delete checks[key]; }
      markStep(box);
      saveChecks();
      syncChecklistHint(true);
    });
  });

  if (dialog && opener && typeof dialog.showModal === "function") {
    opener.addEventListener("click", function () {
      var resetStatus = document.getElementById("reset-status");
      var forgetStatus = document.getElementById("forget-status");
      if (resetStatus) resetStatus.textContent = "";
      if (forgetStatus) forgetStatus.textContent = "";
      syncForm();
      dialog.showModal();
    });
    if (closer) closer.addEventListener("click", function () { dialog.close(); });
    if (closerX) closerX.addEventListener("click", function () { dialog.close(); });
    dialog.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        event.preventDefault();
        dialog.close();
      }
    });
    dialog.addEventListener("close", function () { opener.focus(); });
    dialog.addEventListener("click", function (event) {
      if (event.target === dialog) dialog.close();
    });
    dialog.addEventListener("change", function (event) {
      var el = event.target;
      var p = current();
      if (el.name === "size" || el.name === "theme") p[el.name] = el.value;
      if (el.name === "spacing") p.spacing = el.checked ? "wide" : "normal";
      if (el.name === "font") p.font = el.checked ? "hyper" : "system";
      if (el.name === "brief") p.brief = el.checked ? "on" : "off";
      if (el.name === "remember") {
        if (el.checked) {
          store("set", KEYS.remember, "on");
          saveChecks();
          syncChecklistHint(false);
          announce("Your checklist will be remembered on this device.");
        } else {
          store("remove", KEYS.remember);
          store("remove", KEYS.checks);
          syncChecklistHint(true);
          announce("Your checklist will not be saved on this device.");
        }
        return;
      }
      apply(p);
      savePrefs(p);
      if (el.name === "brief") {
        syncForm();
        announce(el.checked
          ? "Short version. Extra sections on this page are now hidden."
          : "Full version. All sections on this page are showing.");
      }
    });
    var resetBtn = document.getElementById("reset-settings");
    if (resetBtn) {
      resetBtn.addEventListener("click", function () {
        store("remove", KEYS.prefs);
        apply(DEFAULTS);
        syncForm();
        var status = document.getElementById("reset-status");
        if (status) status.textContent = "Reading settings are back to standard. Your checklist was not changed.";
        announce("Reading settings are back to standard. Your checklist was not changed.");
      });
    }
    var forget = document.getElementById("forget-all");
    if (forget) {
      forget.addEventListener("click", function () {
        [KEYS.remember, KEYS.prefs, KEYS.checks].forEach(function (k) { store("remove", k); });
        apply(DEFAULTS);
        checks = {};
        checkBoxes.forEach(function (box) {
          box.checked = false;
          markStep(box);
        });
        syncChecklistHint(false);
        syncForm();
        var resetStatus = document.getElementById("reset-status");
        if (resetStatus) resetStatus.textContent = "";
        var status = document.getElementById("forget-status");
        if (status) status.textContent = "Done. Nothing from this site is saved on this device.";
      });
    }
  } else if (opener) {
    opener.hidden = true;
  }

  /* ---------- Scoped Leave quickly ([data-quick-exit] + triple-Shift) ---------- */
  var exitLinks = document.querySelectorAll("[data-quick-exit]");
  if (exitLinks.length > 0) {
    var defaultExitUrl = exitLinks[0].href || "https://www.weather.gov/";
    function leave(event) {
      if (event) event.preventDefault();
      var targetUrl = (event && event.currentTarget && event.currentTarget.href) || defaultExitUrl;
      try {
        document.body.innerHTML = "";
        document.title = "Weather";
      } catch (e) { /* keep going */ }
      window.location.replace(targetUrl);
    }
    exitLinks.forEach(function (linkEl) {
      linkEl.addEventListener("click", leave);
    });
    var shiftPresses = [];
    function isTypingField(el) {
      if (!el) return false;
      if (el.isContentEditable) return true;
      var tag = el.tagName;
      if (tag === "TEXTAREA" || tag === "SELECT") return true;
      if (tag === "INPUT") {
        var t = (el.type || "text").toLowerCase();
        return ["checkbox", "radio", "button", "submit", "reset"].indexOf(t) === -1;
      }
      return false;
    }
    document.addEventListener("keydown", function (event) {
      if (event.ctrlKey || event.altKey || event.metaKey) {
        shiftPresses = [];
        return;
      }
      if (isTypingField(event.target)) {
        shiftPresses = [];
        return;
      }
      if (event.key !== "Shift") {
        shiftPresses = [];
        return;
      }
      if (event.repeat) return;
      var now = Date.now();
      shiftPresses = shiftPresses.filter(function (t) { return now - t < 1500; });
      shiftPresses.push(now);
      if (shiftPresses.length >= 3) leave();
    });
  }

  /* ---------- Copy buttons ---------- */
  document.querySelectorAll("button[data-copy]").forEach(function (btn) {
    var originalText = btn.textContent;
    var copyTimer = null;
    btn.addEventListener("click", function () {
      var text = btn.getAttribute("data-copy");
      var done = function () {
        btn.textContent = "Copied";
        announce("Copied: " + text);
        if (copyTimer) window.clearTimeout(copyTimer);
        copyTimer = window.setTimeout(function () {
          btn.textContent = originalText;
          copyTimer = null;
        }, 2500);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, function () {
          announce("Copy did not work. Select the text instead.");
        });
      } else {
        announce("Copy is not available in this browser. Select the text instead.");
      }
    });
  });

  /* ---------- Switcher menus ---------- */
  var switchers = Array.prototype.slice.call(document.querySelectorAll("[data-state-switcher]"));
  function clampSwitcherMenu(switcher) {
    if (!switcher || !switcher.open) {
      if (switcher) {
        switcher.classList.remove("is-align-left");
        switcher.classList.remove("is-align-right");
      }
      return;
    }
    var menu = switcher.querySelector(".state-switcher-menu");
    if (!menu) return;
    switcher.classList.remove("is-align-left");
    switcher.classList.remove("is-align-right");
    if (window.getComputedStyle(menu).position !== "absolute") return;
    var rect = menu.getBoundingClientRect();
    if (rect.right > window.innerWidth - 8 && window.innerWidth - rect.width >= 8) {
      switcher.classList.add("is-align-right");
      rect = menu.getBoundingClientRect();
    }
    if (rect.left < 8) {
      switcher.classList.remove("is-align-right");
      switcher.classList.add("is-align-left");
    }
  }
  if (switchers.length) {
    document.addEventListener("click", function (event) {
      switchers.forEach(function (switcher) {
        if (switcher.open && !switcher.contains(event.target)) {
          switcher.open = false;
        }
      });
    });
    document.addEventListener("keydown", function (event) {
      if (event.key !== "Escape") return;
      switchers.forEach(function (switcher) {
        if (switcher.open) {
          event.preventDefault();
          switcher.open = false;
          var switcherSummary = switcher.querySelector("summary");
          if (switcherSummary) switcherSummary.focus();
        }
      });
    });
    window.addEventListener("resize", function () {
      switchers.forEach(clampSwitcherMenu);
    });
    switchers.forEach(function (switcher) {
      switcher.addEventListener("toggle", function () {
        clampSwitcherMenu(switcher);
      });
      switcher.addEventListener("focusout", function (event) {
        if (!switcher.open) return;
        var next = event.relatedTarget;
        if (next && !switcher.contains(next)) {
          switcher.open = false;
        } else if (!next) {
          window.setTimeout(function () {
            if (switcher.open && document.activeElement && !switcher.contains(document.activeElement)) {
              switcher.open = false;
            }
          }, 0);
        }
      });
    });
  }

  /* ---------- Print buttons and details expansion ---------- */
  document.querySelectorAll("[data-print]").forEach(function (btn) {
    btn.hidden = false;
    btn.addEventListener("click", function () { window.print(); });
  });
  var reopened = [];
  // The path finder's closed "Other published pathways" drawer stays closed so printouts
  // stay a clean checklist; a drawer the reader opened prints as shown.
  window.addEventListener("beforeprint", function () {
    document.querySelectorAll("details:not([open]):not(.state-switcher):not(.other-pathways)").forEach(function (d) {
      d.open = true;
      reopened.push(d);
    });
  });
  window.addEventListener("afterprint", function () {
    reopened.forEach(function (d) { d.open = false; });
    reopened = [];
  });

  /* ---------- Reveal hidden Short-version target on hash navigation ---------- */
  var HIDDEN_IN_BRIEF = ".deeper, .brief-extra, .more, [data-deeper]";
  function openTarget() {
    if (!window.location.hash) return;
    var id = window.location.hash.slice(1);
    try { id = decodeURIComponent(id); } catch (e) { /* use raw */ }
    var target = document.getElementById(id);
    if (!target) return;
    var briefActive = current().brief === "on" || current().brief === "true";
    var wasHidden = briefActive && target.closest(HIDDEN_IN_BRIEF);
    if (wasHidden) {
      setBrief(false, "Full version. The link goes to a section the short version hides, so all sections are showing again.", false);
    }
    var d = target.closest("details");
    if (d) d.open = true;
    if (wasHidden) target.scrollIntoView();
  }
  // Guide / Sources tabs (progressive enhancement). Without JS the two links in
  // [data-page-tabs] are plain jump links and both panels show in reading order.
  var tabNav = document.querySelector("[data-page-tabs]");
  var guidePanel = document.getElementById("guide");
  var sourcesPanel = document.querySelector(".page-sources");
  var tabs = null;
  function selectTab(which, moveFocus) {
    if (!tabs) return;
    tabs.forEach(function (tab) {
      var on = tab.getAttribute("data-tab") === which;
      tab.setAttribute("aria-selected", on ? "true" : "false");
      tab.setAttribute("tabindex", on ? "0" : "-1");
      if (on && moveFocus) tab.focus();
    });
    guidePanel.hidden = which !== "guide";
    sourcesPanel.hidden = which !== "sources";
  }
  function setHash(hash) {
    if (window.history && typeof window.history.replaceState === "function") {
      window.history.replaceState(null, "", hash || window.location.pathname + window.location.search);
    }
  }
  function showSourceTarget(target) {
    selectTab("sources", false);
    if (target.getAttribute("tabindex") === null) target.setAttribute("tabindex", "-1");
    target.scrollIntoView();
    target.focus({ preventScroll: true });
  }
  function syncTabToHash() {
    if (!tabs) return;
    var id = window.location.hash.slice(1);
    try { id = decodeURIComponent(id); } catch (e) { /* use raw */ }
    if (id === "sources" || id.slice(-8) === "/sources") {
      selectTab("sources", false);
      return;
    }
    var target = id ? document.getElementById(id) : null;
    if (!target) return;
    if (sourcesPanel.contains(target)) showSourceTarget(target);
    else if (guidePanel.contains(target) && target !== guidePanel) selectTab("guide", false);
  }
  if (tabNav && guidePanel && sourcesPanel) {
    var guideTab = document.getElementById("tab-guide");
    var sourcesTab = document.getElementById("tab-sources");
    if (!sourcesPanel.id) sourcesPanel.id = "page-sources";
    tabs = [guideTab, sourcesTab];
    guideTab.setAttribute("data-tab", "guide");
    sourcesTab.setAttribute("data-tab", "sources");
    tabNav.setAttribute("role", "tablist");
    guideTab.setAttribute("aria-controls", "guide");
    sourcesTab.setAttribute("aria-controls", sourcesPanel.id);
    guidePanel.setAttribute("role", "tabpanel");
    guidePanel.setAttribute("aria-labelledby", "tab-guide");
    sourcesPanel.setAttribute("role", "tabpanel");
    sourcesPanel.setAttribute("aria-labelledby", "tab-sources");
    tabs.forEach(function (tab) {
      tab.setAttribute("role", "tab");
      tab.addEventListener("click", function (event) {
        event.preventDefault();
        var which = tab.getAttribute("data-tab");
        selectTab(which, true);
        setHash(which === "sources" ? "#sources" : "");
      });
      tab.addEventListener("keydown", function (event) {
        var i = tabs.indexOf(tab);
        var next = null;
        if (event.key === "ArrowRight" || event.key === "ArrowDown") next = tabs[(i + 1) % tabs.length];
        else if (event.key === "ArrowLeft" || event.key === "ArrowUp") next = tabs[(i + tabs.length - 1) % tabs.length];
        else if (event.key === "Home") next = tabs[0];
        else if (event.key === "End") next = tabs[tabs.length - 1];
        if (!next) return;
        event.preventDefault();
        var which = next.getAttribute("data-tab");
        selectTab(which, true);
        setHash(which === "sources" ? "#sources" : "");
      });
    });
    document.querySelectorAll(".section-sources").forEach(function (link) {
      link.addEventListener("click", function (event) {
        var target = document.getElementById(link.getAttribute("href").slice(1));
        if (!target) return;
        event.preventDefault();
        setHash(link.getAttribute("href"));
        showSourceTarget(target);
      });
    });
    selectTab("guide", false);
    syncTabToHash();
    window.addEventListener("hashchange", syncTabToHash);
  }

  window.addEventListener("hashchange", openTarget);
  document.addEventListener("click", function (event) {
    var link = event.target.closest ? event.target.closest('a[href^="#"]') : null;
    if (link && link.getAttribute("href") === window.location.hash) openTarget();
  });
  openTarget();
})();
