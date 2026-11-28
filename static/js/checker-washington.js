/* Washington Apple Health 2027 1-minute check data for static/js/checker.js. */
window.CivicDeskCheck = (function () {
  "use strict";
  // Each reason's kind follows Washington HCA's published structure (MED-WA22, MED-WA05, and
  // WAC 182-505-0250 in MED-WA15): "excluded" (outside Apple Health for Adults), "exemption"
  // (exemptions listed by HCA), and "hardship" (short-term hardships: 2 requested from HCA,
  // 2 automatic by county). tests/test_playwright_browser.py checks kinds and basis IDs.
  var REQUESTED = "You must ask HCA to grant this short-term hardship.";

  var KINDS = {
    excluded: {
      one: "outside Apple Health for Adults",
      many: "groups outside Apple Health for Adults",
      tag: "Outside Apple Health for Adults (WAC 182-505-0250)."
    },
    exemption: {
      one: "an exemption",
      many: "exemptions",
      tag: "HCA's list: exemption."
    },
    hardship: {
      one: "a short-term hardship",
      many: "short-term hardships",
      tag: "HCA's list: short-term hardship."
    }
  };
  var KIND_ORDER = ["excluded", "exemption", "hardship"];

  var REASONS = {
    pregnant: {
      label: "You are pregnant, or you are in the 12 months after a pregnancy.",
      kind: "exemption",
      basis: ["MED-WA22", "MED-WA05"]
    },
    care: {
      label: "You are a parent, caretaker, or guardian of a child under 14, or of a person with a disability.",
      kind: "exemption",
      basis: ["MED-WA22", "MED-WA05"]
    },
    health: {
      label: "You are blind or have a disability, a substance use disorder, a disabling mental health condition, or a serious or complex medical condition.",
      how: "HCA calls this medically frail. Through 2027, you can self-attest in Healthplanfinder.",
      kind: "exemption",
      basis: ["MED-WA22", "MED-WA05", "MED-WA04", "MED-S27", "MED-S29"]
    },
    treatment: {
      label: "You are in treatment for alcohol or substance use.",
      kind: "exemption",
      basis: ["MED-WA22", "MED-WA05", "MED-S27"]
    },
    medicare: {
      label: "You have Medicare Part A or Part B.",
      how: "The work rules are only for Apple Health for Adults. That group is for people with no Medicare.",
      notApply: true,
      kind: "excluded",
      basis: ["MED-WA15", "MED-WA22", "MED-WA05"]
    },
    snap: {
      label: "You are in a SNAP (food) household that meets SNAP's work rules, or you get TANF (cash) and meet TANF's work rules.",
      kind: "exemption",
      basis: ["MED-WA22", "MED-WA05"]
    },
    aian: {
      label: "You are American Indian or Alaska Native.",
      how: "Under HCA's rules, American Indian and Alaska Native members keep 12-month renewal periods instead of moving to 6-month renewals.",
      kind: "exemption",
      basis: ["MED-WA22", "MED-WA05", "MED-WA15"]
    },
    foster: {
      label: "You are a foster youth, or a former foster youth under 26.",
      kind: "exemption",
      basis: ["MED-WA22", "MED-WA05"]
    },
    veteran: {
      label: "You are a disabled veteran with a 100 percent total disability rating.",
      kind: "exemption",
      basis: ["MED-WA22", "MED-WA05"]
    },
    jail: {
      label: "You are in jail or prison, or you got out in the last 90 days.",
      kind: "exemption",
      basis: ["MED-WA22", "MED-WA05", "MED-S27"]
    },
    hospital: {
      label: "You stayed part or all of a month as an inpatient in a hospital, nursing home, or similar facility.",
      how: REQUESTED,
      kind: "hardship",
      basis: ["MED-WA22", "MED-S27"]
    },
    travel: {
      label: "You traveled outside your community for care for a serious or complex health condition (for you or a dependent).",
      how: REQUESTED,
      kind: "hardship",
      basis: ["MED-WA22", "MED-S27"]
    },
    county: {
      label: "You live in a county with a presidentially declared disaster, or with unemployment at least 1.5 times the national rate.",
      how: "HCA applies this short-term hardship automatically by county.",
      kind: "hardship",
      basis: ["MED-WA22", "MED-S27"]
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
    state: "Washington",
    agency: "HCA",
    kinds: KINDS,
    reasons: REASONS,
    text: {
      reasonTitle: function (n, picked) {
        var kinds = kindsPicked(picked);
        if (!kinds.length) { return n === 1 ? "This reason is on Washington's published list." : "These reasons are on Washington's published list."; }
        if (n === 1) {
          if (kinds[0] === "excluded") { return "Washington's rules list this group as outside Apple Health for Adults."; }
          return "Washington's rules list this reason as " + KINDS[kinds[0]].one + ".";
        }
        return "Washington's rules list these reasons as " + joinOr(kinds.map(function (k) { return KINDS[k].many; })) + ".";
      }
    },
    meets: {
      income: {
        label: "Your household has at least $580 in income a month.",
        how: "This is the Medicaid household income HCA already counts for Apple Health (called MAGI). It does not have to come from a job. Seasonal workers may use an average of $580 a month over 6 months."
      },
      work: {
        label: "You work at least 80 hours a month.",
        how: "The work can be for pay, for goods or services, or unpaid."
      },
      school: {
        label: "You are in an educational program at least half-time.",
        how: "Under the federal rule, this includes high school and GED programs."
      },
      program: {
        label: "You are in a work program at least 80 hours a month."
      },
      service: {
        label: "You do community service at least 80 hours a month."
      },
      hours: {
        label: "You do a mix of activities that adds up to at least 80 hours a month.",
        how: "You may add up hours of work, community service, a work program, and converted school credits (1 to 6 credits = 13 to 78 hours). You can print an hours log from this guide."
      }
    },
    age: {
      body: "The work rules are only for Apple Health for Adults. That group is ages 19 to 64. You can still apply for coverage.",
      link: { href: "../apply/", text: "How to apply" }
    },
    reasonIntro: "Here is what you picked:",
    reasonTodo: [
      ["The state checks its own data first."],
      ["You may state your own exemption status (self-attestation, including for medical frailty through 2027)."],
      ["Keep your contact information up to date and read notices or text messages from HCA; on Nov. 17, 2026 HCA announced delayed work-rule enforcement through June 30, 2027."],
      [{ href: "../reasons/", text: "See every reason you may not have to meet the rules" }]
    ],
    meetTodo: [
      ["The state checks its own data first."],
      ["You may state your own income or work status (self-attestation)."],
      ["After you apply, a letter may ask for proof. It gives you 15 days. Coverage may close if the proof does not arrive."]
    ],
    meetLink: { href: "../log/", text: "Print an hours log" },
    mayApply: {
      body: "You can still apply. Enrollment is open all year. The rule looks at the month before you apply, and HCA announced delayed enforcement through June 30, 2027.",
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
