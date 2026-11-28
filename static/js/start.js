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
  var routesTpl = document.getElementById("start-routes");

  var ROUTES = {};
  if (routesTpl && routesTpl.content) {
    routesTpl.content.querySelectorAll("[data-route]").forEach(function (el) {
      var topic = el.getAttribute("data-topic");
      var state = el.getAttribute("data-state");
      if (!topic || !state) return;
      if (!ROUTES[topic]) ROUTES[topic] = {};
      var toolHref = el.getAttribute("data-tool-href");
      var toolTitle = el.getAttribute("data-tool-title");
      ROUTES[topic][state] = {
        primary: {
          title: el.getAttribute("data-primary-title") || "",
          href: el.getAttribute("data-primary-href") || ""
        },
        tool: toolHref && toolTitle ? { title: toolTitle, href: toolHref } : null,
        note: (el.textContent || "").trim()
      };
    });
  }

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
    mainBtn.href = match.primary.href;
    mainBtn.textContent = match.primary.title;
    actions.appendChild(mainBtn);

    if (match.tool) {
      var toolBtn = document.createElement("a");
      toolBtn.className = "btn-secondary";
      toolBtn.href = match.tool.href;
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
      runFilter();
    });
  }
})();
