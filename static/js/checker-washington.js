/* Washington Apple Health 2027 1-minute check data for static/js/checker.js. */
window.CivicDeskCheck = (function () {
  "use strict";
  return {
    reasons: {
      pregnant: {
        label: "You are pregnant, or you are in the 12 months after a pregnancy."
      },
      care: {
        label: "You are a parent, caretaker, or guardian of a child under 14, or of a person with a disability."
      },
      health: {
        label: "You are blind or have a disability, a substance use disorder, a disabling mental health condition, or a serious or complex medical condition.",
        how: "HCA calls this medically frail. This list comes from federal law."
      },
      treatment: {
        label: "You are in treatment for alcohol or substance use."
      },
      medicare: {
        label: "You have Medicare Part A or Part B.",
        how: "The work rules are only for Apple Health for Adults. That group is for people with no Medicare.",
        notApply: true
      },
      snap: {
        label: "You are in a SNAP (food) household that meets SNAP's work rules, or you get TANF (cash) and meet TANF's work rules."
      },
      aian: {
        label: "You are American Indian or Alaska Native.",
        how: "You are also left out of the shorter 6-month coverage periods."
      },
      foster: {
        label: "You are a foster youth, or a former foster youth under 26."
      },
      veteran: {
        label: "You are a disabled veteran with a 100 percent total disability rating."
      },
      jail: {
        label: "You got out of jail or prison in the last 90 days."
      },
      hardship: {
        label: "You had a short-term hardship.",
        how: "HCA names four kinds: travel outside your community for care for a serious condition, and a stay in a hospital or care facility (you must ask for these two); and living where the President declared a disaster, or in a county with unemployment 1.5 times the national rate (these two are automatic)."
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
        how: "You may add up hours of work, community service, a work program, and school. You can print an hours log from this guide."
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
