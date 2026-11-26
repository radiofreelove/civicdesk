/* CivicDesk ID & Vital Records path finder (/id-records/path/).
   Supports marriage, divorce, court-ordered name changes, and sex/gender designation updates.
   Birth-certificate question is optional (blank or "skip" omits birth-certificate steps).
   Answers never leave the page and are never saved. */
(function () {
  "use strict";
  var form = document.getElementById("path-form");
  if (!form) return;
  var results = document.getElementById("path-results");
  var body = document.getElementById("results-body");
  var summary = document.getElementById("results-summary");
  var errorBox = document.getElementById("path-error");
  var heading = document.getElementById("results-h");
  var whoFieldset = document.getElementById("q-who");

  var WORDS = {
    live: {
      or: "you live in Oregon",
      wa: "you live in Washington",
      ut: "you live in Utah",
      other: "you live outside Oregon, Washington, and Utah"
    },
    change: {
      marriage: "a name change after marriage or domestic partnership",
      divorce: "a name restoration or change through divorce",
      name: "a court-ordered name change",
      marker: "a sex or gender designation update",
      both: "a name and sex or gender designation update"
    },
    who: {
      adult: "for yourself",
      parent: "for your child",
      minor: "for yourself, under 18"
    },
    born: {
      skip: "without birth-certificate steps",
      or: "with an Oregon birth certificate",
      wa: "with a Washington birth certificate",
      ut: "with a Utah birth certificate",
      "other-us": "with a birth certificate from another U.S. state or territory",
      outside: "born outside the United States"
    }
  };

  function value(name) {
    var el = form.querySelector('input[name="' + name + '"]:checked');
    return el ? el.value : null;
  }

  function syncWhoVisibility() {
    var ch = value("change");
    if (!whoFieldset) return;
    var adultOnly = ch === "marriage" || ch === "divorce";
    whoFieldset.hidden = adultOnly;
  }

  function keep(el, answers) {
    var change = el.getAttribute("data-change");
    var who = el.getAttribute("data-who");
    var court = el.getAttribute("data-court");
    var okChange = !change || change.split(" ").indexOf(answers.change) !== -1;
    var okWho = !who || who.split(" ").indexOf(answers.who) !== -1;
    var okCourt = !court || court.split(" ").indexOf(answers.court) !== -1;
    return okChange && okWho && okCourt;
  }

  function block(id, answers) {
    var tpl = document.getElementById(id);
    if (!tpl || !tpl.content) return null;
    var frag = tpl.content.cloneNode(true);
    frag.querySelectorAll("[data-change], [data-who], [data-court]").forEach(function (el) {
      if (!keep(el, answers)) el.parentNode.removeChild(el);
    });
    frag.querySelectorAll("ul").forEach(function (ul) {
      if (!ul.children.length) ul.parentNode.removeChild(ul);
    });
    return frag;
  }

  function clearResults() {
    results.hidden = true;
    errorBox.hidden = true;
    body.textContent = "";
    form.querySelectorAll('fieldset[aria-describedby="path-error"]').forEach(function (fs) {
      fs.removeAttribute("aria-describedby");
    });
  }

  function showErrors(missing) {
    form.querySelectorAll('fieldset[aria-describedby="path-error"]').forEach(function (fs) {
      fs.removeAttribute("aria-describedby");
    });
    errorBox.textContent = "";
    var title = document.createElement("p");
    title.className = "callout-title";
    var strong = document.createElement("strong");
    strong.textContent = "Please answer the required questions.";
    title.appendChild(strong);
    errorBox.appendChild(title);
    var list = document.createElement("ul");
    missing.forEach(function (q) {
      var fs = document.getElementById("q-" + q.name);
      if (fs) fs.setAttribute("aria-describedby", "path-error");
      var li = document.createElement("li");
      var a = document.createElement("a");
      a.href = "#q-" + q.name;
      a.textContent = q.label;
      a.addEventListener("click", function (event) {
        event.preventDefault();
        var first = form.querySelector('input[name="' + q.name + '"]');
        if (first) first.focus();
      });
      li.appendChild(a);
      list.appendChild(li);
    });
    errorBox.appendChild(list);
    var firstFs = document.getElementById("q-" + missing[0].name);
    if (firstFs && firstFs.parentNode) {
      firstFs.parentNode.insertBefore(errorBox, firstFs);
    }
    errorBox.hidden = false;
    errorBox.focus();
  }

  form.reset();
  syncWhoVisibility();
  clearResults();
  window.addEventListener("pageshow", function () {
    form.reset();
    syncWhoVisibility();
    clearResults();
  });

  form.addEventListener("change", function () {
    syncWhoVisibility();
    if (!results.hidden || !errorBox.hidden) clearResults();
  });

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var live = value("live");
    var change = value("change");
    var adultOnly = change === "marriage" || change === "divorce";
    var who = adultOnly ? (value("who") || "adult") : value("who");
    var born = value("born") || "skip";

    var missing = [];
    if (!live) missing.push({ name: "live", label: "Where do you live?" });
    if (!change) missing.push({ name: "change", label: "What do you want to update?" });
    if (!adultOnly && !who) missing.push({ name: "who", label: "Who is this for?" });

    if (missing.length) {
      showErrors(missing);
      return;
    }

    var answers = { live: live, change: change, who: who, born: born };
    errorBox.hidden = true;
    body.textContent = "";

    var noCourt =
      answers.change === "marriage" ||
      (answers.change === "marker" &&
        (answers.live === "or" || answers.live === "wa") &&
        (answers.born === "or" || answers.born === "wa" || answers.born === "skip"));
    answers.court = noCourt ? "no" : "yes";

    var order = [];
    if (answers.change === "marriage") {
      order.push("t-marriage-" + answers.live);
    } else if (answers.change === "divorce") {
      order.push("t-divorce-" + answers.live);
    } else if (!noCourt) {
      order.push("t-court-" + answers.live);
    }

    order.push("t-federal");
    order.push("t-id-" + answers.live);
    if (answers.born !== "skip") {
      order.push("t-birth-" + answers.born);
    }
    order.push("t-after");
    order.push("t-help-" + answers.live);

    var n = 0;
    order.forEach(function (id) {
      var frag = block(id, answers);
      if (!frag) return;
      var h = frag.querySelector("h3");
      var hasContent = frag.querySelector("li, p");
      if (!hasContent) return;
      n += 1;
      if (h) h.textContent = "Step " + n + ": " + h.textContent;
      body.appendChild(frag);
    });

    summary.textContent =
      "These steps are for " + WORDS.change[answers.change] + " " + WORDS.who[answers.who] +
      ", when " + WORDS.live[answers.live] + ", " + WORDS.born[answers.born] + "." +
      (answers.change === "marker" && noCourt ? " You do not need a court order for this path." : "");
    results.hidden = false;
    heading.focus();
  });

  form.addEventListener("reset", function () {
    window.setTimeout(syncWhoVisibility, 0);
    clearResults();
  });
})();
