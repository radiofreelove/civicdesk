/* /start/ 2-question state-and-topic router.
   Answers never leave the page and are never saved. */
(function () {
  "use strict";
  var form = document.getElementById("start-form");
  if (!form) return;
  var resultBox = document.getElementById("start-result");
  var resultBody = document.getElementById("start-result-body");
  var resultHeading = document.getElementById("start-result-h");
  var errorBox = document.getElementById("start-error");
  var basePrefix = form.getAttribute("data-root") || "/";

  var ROUTES = {
    medicaid: {
      oregon: {
        primary: { title: "Open the Oregon Health Plan (OHP) 2027 guide", href: "medicaid/oregon/" },
        tool: { title: "Take the 1-minute OHP check", href: "medicaid/oregon/check/" },
        note: "Covers the Jan. 1, 2027 OHP work and activity rules, Oregon's hold harmless delay through June 30, 2027, the Dec. 31, 2026 past-bills cutoff, exemptions, and how to apply."
      },
      washington: {
        primary: { title: "Open the Washington Apple Health 2027 guide", href: "medicaid/washington/" },
        tool: { title: "Take the 1-minute Apple Health check", href: "medicaid/washington/check/" },
        note: "Covers the Jan. 1, 2027 Apple Health for Adults work rules, HCA's delayed enforcement through June 30, 2027, 6-month renewals, exemptions, and how to apply."
      },
      utah: {
        primary: { title: "Open the Utah Medicaid 2027 guide", href: "medicaid/utah/" },
        tool: { title: "Take the 1-minute Utah Medicaid check", href: "medicaid/utah/check/" },
        note: "Covers Utah Adult Expansion and Targeted Adult Medicaid 2027 community engagement rules, exclusions, Jan. 1, 2027 tribal proof deadline, and myCase / DWS steps."
      },
      federal: {
        primary: { title: "Open the 2027 Medicaid desk overview", href: "medicaid/" },
        tool: null,
        note: "CivicDesk currently covers detailed state Medicaid rules for Oregon, Washington, and Utah, plus federal rules and court updates that affect all states."
      }
    },
    "id-records": {
      oregon: {
        primary: { title: "Open the Oregon name, ID, and vital records guide", href: "id-records/oregon/" },
        tool: { title: "Use the personalized ID & records path finder", href: "id-records/path/" },
        note: "Covers marriage, divorce, and circuit court name changes, confidential sex-change filings, Oregon birth certificates (form OHA 2673), and Oregon DMV cards."
      },
      washington: {
        primary: { title: "Open the Washington name, ID, and vital records guide", href: "id-records/washington/" },
        tool: { title: "Use the personalized ID & records path finder", href: "id-records/path/" },
        note: "Covers marriage, divorce, public district court and sealed superior court name changes, Washington birth certificates, and Department of Licensing cards."
      },
      utah: {
        primary: { title: "Open the Utah name, ID, and vital records guide", href: "id-records/utah/" },
        tool: { title: "Use the personalized ID & records path finder", href: "id-records/path/" },
        note: "Covers marriage, divorce, and Utah district court name and sex-designation orders, Vital Records form 902, and Driver License Division updates."
      },
      federal: {
        primary: { title: "Open the Passports and Social Security guide", href: "id-records/federal/" },
        tool: { title: "Use the personalized ID & records path finder", href: "id-records/path/" },
        note: "Covers Social Security replacement cards after marriage, divorce, or court orders, passport name changes, and 2025-2026 federal sex-marker rules."
      }
    }
  };

  function getVal(name) {
    var checked = form.querySelector('input[name="' + name + '"]:checked');
    return checked ? checked.value : null;
  }

  function clearAll() {
    resultBox.hidden = true;
    errorBox.hidden = true;
    resultBody.textContent = "";
    form.querySelectorAll('fieldset[aria-describedby="start-error"]').forEach(function (fs) {
      fs.removeAttribute("aria-describedby");
    });
  }

  form.reset();
  clearAll();
  window.addEventListener("pageshow", function () {
    form.reset();
    clearAll();
  });
  form.addEventListener("change", function () {
    if (!resultBox.hidden || !errorBox.hidden) clearAll();
  });
  form.addEventListener("reset", clearAll);

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var state = getVal("state");
    var topic = getVal("topic");
    var missing = [];
    if (!state) missing.push({ name: "state", label: "1. Which state or level do you need?" });
    if (!topic) missing.push({ name: "topic", label: "2. What are you trying to do?" });

    if (missing.length) {
      form.querySelectorAll('fieldset[aria-describedby="start-error"]').forEach(function (fs) {
        fs.removeAttribute("aria-describedby");
      });
      errorBox.textContent = "";
      var p = document.createElement("p");
      p.className = "callout-title";
      var strong = document.createElement("strong");
      strong.textContent = "Please answer both questions.";
      p.appendChild(strong);
      errorBox.appendChild(p);
      var ul = document.createElement("ul");
      missing.forEach(function (m) {
        var fs = document.getElementById("q-" + m.name);
        if (fs) fs.setAttribute("aria-describedby", "start-error");
        var li = document.createElement("li");
        var a = document.createElement("a");
        a.href = "#q-" + m.name;
        a.textContent = m.label;
        a.addEventListener("click", function (ev) {
          ev.preventDefault();
          var first = form.querySelector('input[name="' + m.name + '"]');
          if (first) first.focus();
        });
        li.appendChild(a);
        ul.appendChild(li);
      });
      errorBox.appendChild(ul);
      var firstFs = document.getElementById("q-" + missing[0].name);
      if (firstFs && firstFs.parentNode) {
        firstFs.parentNode.insertBefore(errorBox, firstFs);
      }
      errorBox.hidden = false;
      errorBox.focus();
      return;
    }

    var match = ROUTES[topic] && ROUTES[topic][state];
    if (!match) return;
    resultBody.textContent = "";
    var note = document.createElement("p");
    note.textContent = match.note;
    resultBody.appendChild(note);

    var actions = document.createElement("div");
    actions.className = "btn-row";
    var mainBtn = document.createElement("a");
    mainBtn.className = "btn";
    mainBtn.href = basePrefix + match.primary.href;
    mainBtn.textContent = match.primary.title;
    actions.appendChild(mainBtn);

    if (match.tool) {
      var toolBtn = document.createElement("a");
      toolBtn.className = "btn-secondary";
      toolBtn.href = basePrefix + match.tool.href;
      toolBtn.textContent = match.tool.title;
      actions.appendChild(toolBtn);
    }
    resultBody.appendChild(actions);

    resultBox.hidden = false;
    resultHeading.focus();
  });

  var filterInput = document.getElementById("guide-filter");
  var filterStatus = document.getElementById("guide-filter-status");
  if (filterInput) {
    var groups = Array.prototype.slice.call(document.querySelectorAll("[data-guide-group]"));
    var items = Array.prototype.slice.call(document.querySelectorAll("[data-guide-item]"));
    var runFilter = function () {
      var raw = (filterInput.value || "").trim();
      var tokens = raw.toLowerCase().split(/\s+/).filter(Boolean);
      var shown = 0;
      items.forEach(function (item) {
        var hay = (item.getAttribute("data-search") || item.textContent || "").toLowerCase();
        var ok = tokens.every(function (t) { return hay.indexOf(t) !== -1; });
        item.hidden = !ok;
        if (ok) shown += 1;
      });
      groups.forEach(function (group) {
        var anyVisible = Array.prototype.some.call(
          group.querySelectorAll("[data-guide-item]"),
          function (li) { return !li.hidden; }
        );
        group.hidden = !anyVisible;
      });
      if (filterStatus) {
        if (!tokens.length) {
          filterStatus.textContent = "";
        } else if (shown === 0) {
          filterStatus.textContent = "No guides match \"" + raw + "\". Clear the filter to see every guide.";
        } else {
          filterStatus.textContent = "Showing " + shown + (shown === 1 ? " matching guide." : " matching guides.");
        }
      }
    };
    filterInput.addEventListener("input", runFilter);
    window.addEventListener("pageshow", function () {
      filterInput.value = "";
      runFilter();
    });
  }
})();
