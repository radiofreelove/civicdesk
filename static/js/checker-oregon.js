/* Oregon Health Plan (OHP) 2027 1-minute check data for static/js/checker.js. */
window.CivicDeskCheck = (function () {
  "use strict";
  // Each reason's kind follows OHA's exemptions explainer and checklist (MED-S03): "excluded"
  // (groups outside the OHP work rules), "auto" (exemptions OHP checks automatically from state
  // records), and "self_report" (exemptions reported directly to OHP, with 2028 proof notes).
  // tests/test_playwright_browser.py checks kinds and basis IDs.
  var KINDS = {
    excluded: {
      one: "outside the OHP work rules",
      many: "groups outside the OHP work rules",
      tag: "OHP list: excluded group (rules do not apply)."
    },
    auto: {
      one: "an exemption OHP checks automatically",
      many: "exemptions OHP checks automatically",
      tag: "OHP list: exemption checked automatically by OHP."
    },
    self_report: {
      one: "an exemption you report to OHP",
      many: "exemptions you report to OHP",
      tag: "OHP list: exemption you report to OHP."
    }
  };
  var KIND_ORDER = ["excluded", "auto", "self_report"];

  var REASONS = {
    pregnant: {
      label: "You are pregnant, or you were in the last 12 months.",
      how: "Tell OHP. No proof is needed.",
      kind: "self_report",
      basis: ["MED-S03", "MED-S02"]
    },
    child: {
      label: "You care for a child 13 or younger.",
      how: "Tell OHP how you are related to the child. Starting in 2028, OHP may ask for a calendar or schedule.",
      kind: "self_report",
      basis: ["MED-S03", "MED-S02"]
    },
    caregiver: {
      label: "You care for someone with a disability or a serious health condition.",
      how: "Tell OHP. If you do not live with them and are not related, you need to give at least 80 hours of care a month.",
      kind: "self_report",
      basis: ["MED-S03", "MED-S02", "MED-S42"]
    },
    health: {
      label: "You have a serious or disabling health condition.",
      how: "Tell OHP. In 2027, no doctor's note is needed (starting in 2028, OHP may ask for a provider statement if not permanent).",
      kind: "self_report",
      basis: ["MED-S03", "MED-S01", "MED-S07"]
    },
    ssi: {
      label: "You get SSI or SSDI.",
      how: "Tell OHP. OHA counts this as a qualifying health condition.",
      kind: "self_report",
      basis: ["MED-S03", "MED-S07"]
    },
    treatment: {
      label: "You are in drug or alcohol treatment, or in stable recovery within the last 5 years.",
      how: "Tell OHP. Starting in 2028, OHP may ask for a paper that shows your treatment dates.",
      kind: "self_report",
      basis: ["MED-S03", "MED-S42"]
    },
    medicare: {
      label: "You have Medicare.",
      how: "The rules do not apply to people with Medicare. OHP can usually check this itself.",
      notApply: true,
      kind: "excluded",
      basis: ["MED-S02", "MED-S03"]
    },
    disability_ohp: {
      label: "You are applying for OHP because of a disability.",
      how: "The rules do not apply to OHP programs based on disability.",
      notApply: true,
      kind: "excluded",
      basis: ["MED-S02", "MED-S03"]
    },
    snap: {
      label: "You get SNAP, and you meet SNAP work rules.",
      how: "OHP checks this from state records. Being excused from SNAP work rules is not enough.",
      kind: "auto",
      basis: ["MED-S02", "MED-S03"]
    },
    tanf: {
      label: "You get TANF, and you meet TANF work rules.",
      how: "OHP checks this from state records.",
      kind: "auto",
      basis: ["MED-S02", "MED-S03"]
    },
    aian: {
      label: "You are American Indian or Alaska Native.",
      how: "Tell OHP. No proof is needed.",
      kind: "self_report",
      basis: ["MED-S03", "MED-S02"]
    },
    foster: {
      label: "You were in foster care at age 18, and you are under 26.",
      how: "OHP checks this from state records.",
      kind: "auto",
      basis: ["MED-S02", "MED-S03"]
    },
    veteran: {
      label: "You are a veteran with a 100% disability rating from the VA.",
      how: "Tell OHP. No doctor's note in 2027; starting in 2028, OHP may ask for your VA 100% award letter.",
      kind: "self_report",
      basis: ["MED-S03", "MED-S07"]
    },
    jail: {
      label: "You got out of jail or prison in the last 4 months.",
      how: "Tell OHP. No proof is needed.",
      kind: "self_report",
      basis: ["MED-S03", "MED-S02"]
    },
    hospital: {
      label: "You stayed at least one night as a patient in a hospital, nursing facility, or behavioral health facility.",
      how: "Tell OHP. OHP lists this as a short-term medical hardship.",
      kind: "self_report",
      basis: ["MED-S03", "MED-S27"]
    },
    travel: {
      label: "You were away from home 7 or more days in a month to get medical care for you, your child, or a tax dependent.",
      how: "Tell OHP. OHP lists this as a short-term medical hardship.",
      kind: "self_report",
      basis: ["MED-S03", "MED-S27"]
    },
    county: {
      label: "You live in a county with a federal disaster declaration or high unemployment.",
      how: "OHP checks county-level disaster and high-unemployment status automatically.",
      kind: "auto",
      basis: ["MED-S02", "MED-S03", "MED-S27"]
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
    state: "Oregon",
    agency: "OHP",
    kinds: KINDS,
    reasons: REASONS,
    text: {
      reasonTitle: function (n, picked) {
        var kinds = kindsPicked(picked);
        if (!kinds.length) { return n === 1 ? "This reason is on Oregon's published list." : "These reasons are on Oregon's published list."; }
        if (n === 1) {
          if (kinds[0] === "excluded") { return "Oregon's rules list this group as outside the OHP work rules."; }
          return "Oregon's rules list this reason as " + KINDS[kinds[0]].one + ".";
        }
        return "Oregon's rules list these reasons as " + joinOr(kinds.map(function (k) { return KINDS[k].many; })) + ".";
      }
    },
    meets: {
      income: {
        label: "Your household brings in $580 or more a month.",
        how: "OHP may check payroll records first. Keep pay stubs, just in case."
      },
      school: {
        label: "You go to school at least half-time.",
        how: "OHP may check school records first. Keep proof that you are enrolled, just in case."
      },
      hours: {
        label: "You spend 80 hours a month on work, volunteering, school, or job training, added together.",
        how: "Write down your hours. You can print an hours log from this guide."
      }
    },
    age: {
      body: "The work or activity rules are for adults ages 19 to 64. You can still apply for OHP.",
      link: { href: "../apply/", text: "How to apply" }
    },
    reasonIntro: "Here is what you picked, and how OHP checks each one:",
    reasonTodo: [
      ["When you apply, tell OHP your reason if it is not checked automatically."],
      ["Already applied? Tell OHP in your ONE account at ", { href: "https://benefits.oregon.gov/", text: "benefits.oregon.gov" }, ", or call 800-699-9075."],
      ["Oregon directed OHA to pause work-rule denials through June 30, 2027 (subject to federal approval), while record checks still begin Jan. 1, 2027."]
    ],
    meetTodo: [
      ["When you apply, answer the questions about work, school, and volunteering."],
      ["If OHP asks for proof, you get a letter. You have 35 days to reply."]
    ],
    meetLink: { href: "../log/", text: "Print an hours log" },
    mayApply: {
      body: "You can still apply. OHP looks at the month before you apply, and Oregon has announced delayed work-rule enforcement through June 30, 2027.",
      ways: [
        { strong: "Look again for a reason.", link: { href: "../reasons/", text: "See every reason you may not have to meet the rules" } },
        { strong: "Find work, school, or volunteer options.", link: { href: "https://www.oregon.gov/oha/hsd/ohp/pages/community-resources.aspx", text: "OHP's job and volunteer resources" } },
        { strong: "Talk to a free helper.", link: { href: "../help/", text: "Free help and other languages" } }
      ]
    },
    promise: [
      "Only OHP decides. This check is not a decision.",
      "Nothing you picked was saved or sent."
    ]
  };
})();
