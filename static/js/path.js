/* CivicDesk ID & Vital Records path finder (/id-records/path/).
   Supports marriage, divorce, court-ordered name changes, and sex/gender designation updates.
   Answers sort the published steps instead of hiding the rest: matching steps come first, each with
   the rule that sorts it there ("Sorted by ..." from data-basis-label), and every other published
   pathway for the reader's state stays on the page in the closed "Other published pathways" drawer.
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
  var drawer = document.getElementById("other-pathways");
  var drawerTitle = document.getElementById("other-pathways-title");
  var drawerBody = document.getElementById("other-pathways-body");

  var STATE_NAMES = { or: "Oregon", wa: "Washington", ut: "Utah" };
  var FILTERED = "[data-change], [data-who], [data-court]";
  var ENTRY_PREFIXES = ["t-marriage-", "t-divorce-", "t-court-"];

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

  // Clone a result template and split it: the matching lines stay in the step, and alternate
  // pathway/filer lines (data-change or data-who) stay on the page in the Other published
  // pathways drawer. Pure wording variants (data-court only, as in t-after) are not alternate
  // pathways, so they are dropped rather than duplicated into the drawer.
  function split(id, answers) {
    var tpl = document.getElementById(id);
    if (!tpl || !tpl.content) return null;
    var frag = tpl.content.cloneNode(true);
    var other = [];
    frag.querySelectorAll(FILTERED).forEach(function (el) {
      if (!keep(el, answers)) {
        el.parentNode.removeChild(el);
        if (el.hasAttribute("data-change") || el.hasAttribute("data-who")) {
          other.push(el);
        }
      }
    });
    frag.querySelectorAll("ul").forEach(function (ul) {
      if (!ul.children.length) ul.parentNode.removeChild(ul);
    });
    return { frag: frag, other: other };
  }

  // "Sorted by" names the rules behind the lines actually shown. Lines that carry their own
  // data-basis (where name and sex-marker pathways follow different rules) win; otherwise the
  // block's data-basis applies. IDs and labels are parallel lists ("ID-A ID-B" / "A; B").
  function basisLabels(blockEl, lines) {
    var ids = [];
    var labels = [];
    function add(el) {
      var idAttr = el && el.getAttribute("data-basis");
      var labelAttr = el && el.getAttribute("data-basis-label");
      if (!idAttr || !labelAttr) return false;
      var labelList = labelAttr.split("; ");
      idAttr.split(" ").forEach(function (sid, i) {
        if (ids.indexOf(sid) === -1) {
          ids.push(sid);
          labels.push(labelList[i] || sid);
        }
      });
      return true;
    }
    var fromLines = false;
    (lines || []).forEach(function (el) {
      if (add(el)) fromLines = true;
    });
    if (!fromLines) add(blockEl);
    return labels;
  }

  function basisLine(blockEl, lines) {
    var labels = basisLabels(blockEl, lines);
    if (!labels.length) return null;
    var p = document.createElement("p");
    p.className = "basis-line";
    p.textContent = "Sorted by: " + labels.join("; ") + ".";
    return p;
  }

  function linesIn(node) {
    return node ? Array.prototype.slice.call(node.querySelectorAll(".step-body [data-basis]")) : [];
  }

  function drawerGroup(title, blockEl, nodes) {
    var group = document.createElement("div");
    group.className = "other-group";
    var h = document.createElement("h3");
    h.textContent = title;
    group.appendChild(h);
    var basisNodes = [];
    nodes.forEach(function (node) {
      if (node.hasAttribute("data-basis")) basisNodes.push(node);
      basisNodes = basisNodes.concat(Array.prototype.slice.call(node.querySelectorAll("[data-basis]")));
    });
    var line = basisLine(blockEl, basisNodes);
    if (line) group.appendChild(line);
    var list = null;
    nodes.forEach(function (node) {
      if (node.tagName === "LI") {
        if (!list) {
          list = document.createElement("ul");
          group.appendChild(list);
        }
        list.appendChild(node);
      } else {
        list = null;
        group.appendChild(node);
      }
    });
    return group;
  }

  // A whole entry pathway (marriage, divorce, or court) for the reader's state that the answers
  // did not put in the main list, shown unfiltered.
  function wholePathway(id) {
    var tpl = document.getElementById(id);
    if (!tpl || !tpl.content) return null;
    var frag = tpl.content.cloneNode(true);
    var blockEl = frag.querySelector(".result-block");
    var stepBody = frag.querySelector(".step-body");
    if (!stepBody) return null;
    var h = stepBody.querySelector("h3");
    var nodes = Array.prototype.slice.call(stepBody.children).filter(function (c) { return c !== h; });
    if (!nodes.length) return null;
    return drawerGroup(h ? h.textContent : "", blockEl, nodes);
  }

  function clearDrawer() {
    if (!drawer) return;
    drawer.open = false;
    drawer.hidden = true;
    if (drawerBody) drawerBody.textContent = "";
  }

  function clearResults() {
    results.hidden = true;
    errorBox.hidden = true;
    body.textContent = "";
    clearDrawer();
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
    clearDrawer();

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

    // Entry pathways for this state that the answers did not select come first in the drawer.
    var groups = [];
    ENTRY_PREFIXES.forEach(function (prefix) {
      var id = prefix + answers.live;
      if (order.indexOf(id) !== -1) return;
      var group = wholePathway(id);
      if (group) groups.push(group);
    });

    var n = 0;
    order.forEach(function (id) {
      var parts = split(id, answers);
      if (!parts) return;
      var frag = parts.frag;
      var blockEl = frag.querySelector(".result-block");
      var h = frag.querySelector("h3");
      var title = h ? h.textContent : "";
      if (parts.other.length) groups.push(drawerGroup(title, blockEl, parts.other));
      // Each block is a step-timeline <li>; only its body content counts.
      var hasContent = frag.querySelector(".step-body li, .step-body p");
      if (!hasContent) return;
      n += 1;
      if (h) {
        h.textContent = "Step " + n + ": " + title;
        var line = basisLine(blockEl, linesIn(frag));
        if (line) h.parentNode.insertBefore(line, h.nextSibling);
      }
      var node = frag.querySelector(".step-node");
      if (node) node.textContent = String(n);
      body.appendChild(frag);
    });

    if (drawer && drawerBody && groups.length) {
      groups.forEach(function (g) { drawerBody.appendChild(g); });
      if (drawerTitle) {
        drawerTitle.textContent = STATE_NAMES[answers.live]
          ? "Other published pathways in " + STATE_NAMES[answers.live]
          : "Other published pathways for people outside Oregon, Washington, and Utah";
      }
      drawer.hidden = false;
    }

    summary.textContent =
      "These steps are for " + WORDS.change[answers.change] + " " + WORDS.who[answers.who] +
      ", when " + WORDS.live[answers.live] + ", " + WORDS.born[answers.born] + "." +
      (answers.change === "marker" && noCourt ? " Oregon and Washington agencies do not require a court order for state ID or in-state birth-certificate sex-marker updates; a court order remains optional under state law." : "") +
      (groups.length ? " Other published pathways are listed below the steps." : "");
    results.hidden = false;
    heading.focus();
  });

  form.addEventListener("reset", function () {
    window.setTimeout(syncWhoVisibility, 0);
    clearResults();
  });
})();
