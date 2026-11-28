/* Utah Medicaid 2027 1-minute check data for static/js/checker.js. */
window.CivicDeskCheck = (function () {
  "use strict";
  // Each reason's kind follows the headings of Utah's H.R. 1 exclusions and exceptions table
  // (MED-U05): "exclusion" (excluded individuals), "exception" (mandatory exceptions), and
  // "hardship" (optional short-term hardships). basis lists the registry sources for the item;
  // drug or alcohol treatment is not on MED-U05, so it carries its own tag and rests on MED-U01,
  // MED-U02, and the federal rule (MED-S27). tests/test_playwright_browser.py checks kinds, tags,
  // and basis IDs.
  var REQUESTED = "You must ask the state for it.";

  var KINDS = {
    exclusion: { one: "an exclusion", many: "exclusions", tag: "Utah's table: excluded individuals." },
    exception: { one: "a mandatory exception", many: "mandatory exceptions", tag: "Utah's table: mandatory exceptions." },
    hardship: { one: "an optional short-term hardship", many: "optional short-term hardships", tag: "Utah's table: optional short-term hardships." }
  };
  var KIND_ORDER = ["exclusion", "exception", "hardship"];

  var REASONS = {
    pregnant: {
      label: "You are pregnant, or you get Medicaid for the months after a birth.",
      kind: "exclusion",
      basis: ["MED-U05", "MED-U01", "MED-U04"]
    },
    care: {
      label: "You are a parent, guardian, or caregiver for a child 13 or younger, or for a person with a disability.",
      kind: "exclusion",
      basis: ["MED-U05", "MED-U01", "MED-U04"]
    },
    health: {
      label: "You are blind, or you have a disability, a mental health condition, a substance use disorder, or a condition that makes daily tasks hard.",
      how: "Utah Medicaid checks its own health records first. If that does not confirm it, you fill out a short form about your condition.",
      kind: "exclusion",
      basis: ["MED-U05", "MED-U01", "MED-U39"]
    },
    treatment: {
      label: "You are in a drug or alcohol treatment and rehabilitation program.",
      how: "Utah Medicaid checks its own health records first.",
      kind: "exclusion",
      tag: "Utah Medicaid pages and federal rule: excluded individuals.",
      basis: ["MED-U01", "MED-U02", "MED-S27"]
    },
    medicare: {
      label: "You have Medicare Part A or Part B.",
      how: "Utah says the rules do not apply to people with Medicare Part A or Part B.",
      kind: "exception",
      basis: ["MED-U05", "MED-U01", "MED-U04", "MED-U07"]
    },
    ssi: {
      label: "You get SSI.",
      how: "Utah says the rules do not apply to people in some other Medicaid groups, like people who get SSI.",
      kind: "exception",
      basis: ["MED-U05", "MED-U04", "MED-U01"]
    },
    snap: {
      label: "You have to meet work rules for SNAP or TANF.",
      kind: "exclusion",
      basis: ["MED-U05", "MED-U01", "MED-U04"]
    },
    aian: {
      label: "You are American Indian or Alaska Native.",
      how: "The state must verify your status. Utah's letter says to send proof by Jan. 1, 2027.",
      kind: "exclusion",
      basis: ["MED-U05", "MED-U02", "MED-U40"]
    },
    foster: {
      label: "You are in foster care, or you were.",
      how: "Some Utah pages say this means people in the Former Foster Care program. Ask the state which one fits you.",
      kind: "exclusion",
      basis: ["MED-U05", "MED-U01", "MED-U02"]
    },
    veteran: {
      label: "You are a veteran with a 100% disability.",
      kind: "exclusion",
      basis: ["MED-U05", "MED-U01", "MED-U04"]
    },
    jail: {
      label: "You are in jail or prison, or you got out in the last 90 days.",
      kind: "exception",
      basis: ["MED-U05", "MED-U01", "MED-U04", "MED-U07"]
    },
    hospital: {
      label: "You were a patient in a hospital, or you lived in a nursing home or another care facility.",
      how: REQUESTED,
      kind: "hardship",
      basis: ["MED-U05", "MED-U04", "MED-U01"]
    },
    travel: {
      label: "You had to travel outside your community for medical care for you or a dependent.",
      how: REQUESTED,
      kind: "hardship",
      basis: ["MED-U05", "MED-U04", "MED-U01"]
    },
    county: {
      label: "You live in a county with a federal emergency or disaster declaration, or with high unemployment.",
      how: "Utah's table lists this hardship without the note that you must request it.",
      kind: "hardship",
      basis: ["MED-U05", "MED-U04", "MED-U01"]
    }
  };

  function kindsPicked(keys) {
    var found = {};
    keys.forEach(function (key) {
      if (REASONS[key] && REASONS[key].kind) { found[REASONS[key].kind] = true; }
    });
    return KIND_ORDER.filter(function (kind) { return found[kind]; });
  }

  function joinOr(words) {
    if (words.length < 3) { return words.join(" or "); }
    return words.slice(0, -1).join(", ") + ", or " + words[words.length - 1];
  }

  return {
    state: "Utah",
    agency: "DWS",
    kinds: KINDS,
    reasons: REASONS,
    text: {
      reasonTitle: function (n, picked) {
        var kinds = kindsPicked(picked);
        if (!kinds.length) { return n === 1 ? "This reason is on Utah's published list." : "These reasons are on Utah's published list."; }
        if (n === 1) { return "Utah's rules list this reason as " + KINDS[kinds[0]].one + "."; }
        return "Utah's rules list these reasons as " + joinOr(kinds.map(function (k) { return KINDS[k].many; })) + ".";
      }
    },
    meets: {
      income: {
        label: "You earn at least $580 a month.",
        how: "Utah's pages do not say whose income counts. The federal rule counts your household's. Ask the state how it counts yours."
      },
      school: {
        label: "You go to school at least half-time.",
        how: "Your school decides what counts as half-time."
      },
      hours: {
        label: "You spend 80 hours a month on work, job training, volunteering, or school, added together.",
        how: "Write down your hours. You can print an hours log from this guide."
      }
    },
    age: {
      body: "The work or activity rules are for adults ages 19 to 64. You can still apply for Utah Medicaid.",
      link: { href: "../apply/", text: "How to apply" }
    },
    reasonIntro: "Here is what you picked:",
    reasonTodo: [
      ["The state checks records it already has first. Under Utah's published process, if DWS confirms a reason from state records, it does not ask for extra forms."],
      ["If it cannot, you can report your reason online in myCase, on your application, or on form 742."],
      ["Utah's page names form 742 but does not link to it. Ask the Department of Workforce Services (DWS) for it at 1-866-435-7414."],
      [{ href: "../reasons/", text: "See every reason, and what proof tribal members send" }]
    ],
    meetTodo: [
      ["The state checks records it already has first, like work and school records."],
      ["If the state needs more, you may need to send a form within 30 days, or you could lose your coverage."]
    ],
    meetLink: { href: "../log/", text: "Print an hours log" },
    mayApply: {
      body: "You can still apply. The state looks at the month before you apply.",
      ways: [
        { strong: "Look again for a reason.", link: { href: "../reasons/", text: "See every reason you may not have to meet the rules" } },
        { strong: "Read what counts.", link: { href: "../rules/", text: "The rules, in plain words" } },
        { strong: "Talk to a free helper.", link: { href: "../help/", text: "Free help and other languages" } }
      ]
    },
    promise: [
      "Only the state decides. This check is not a decision.",
      "Nothing you picked was saved or sent."
    ]
  };
})();
