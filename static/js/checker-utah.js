/* Utah Medicaid 2027 1-minute check data for static/js/checker.js. */
window.CivicDeskCheck = (function () {
  "use strict";
  var HARDSHIP = "Utah calls this a short-term hardship. You must ask the state for this exception.";

  return {
    reasons: {
      pregnant: {
        label: "You are pregnant, or you get Medicaid for the months after a birth."
      },
      care: {
        label: "You are a parent, guardian, or caregiver for a child 13 or younger, or for a person with a disability."
      },
      health: {
        label: "You are blind, or you have a disability, a mental health condition, a substance use disorder, or a condition that makes daily tasks hard.",
        how: "Utah Medicaid checks its own health records first. If that does not confirm it, you fill out a short form about your condition."
      },
      treatment: {
        label: "You are in a drug or alcohol treatment and rehabilitation program.",
        how: "Utah Medicaid checks its own health records first."
      },
      medicare: {
        label: "You have Medicare Part A or Part B.",
        how: "Utah says the rules do not apply to people with Medicare Part A or Part B.",
        notApply: true
      },
      ssi: {
        label: "You get SSI.",
        how: "Utah says the rules do not apply to people in some other Medicaid groups, like people who get SSI.",
        notApply: true
      },
      snap: {
        label: "You have to meet work rules for SNAP or TANF."
      },
      aian: {
        label: "You are American Indian or Alaska Native.",
        how: "The state must verify your status. Utah's letter says to send proof by Jan. 1, 2027."
      },
      foster: {
        label: "You are in foster care, or you were.",
        how: "Some Utah pages say this means people in the Former Foster Care program. Ask the state which one fits you."
      },
      veteran: {
        label: "You are a veteran with a 100% disability."
      },
      jail: {
        label: "You got out of jail or prison in the last 90 days."
      },
      hospital: {
        label: "You were a patient in a hospital, or you lived in a nursing home or another care facility.",
        how: HARDSHIP
      },
      travel: {
        label: "You had to travel outside your community for medical care for you or a dependent.",
        how: HARDSHIP
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
      ["The state checks records it already has first. If it can confirm your reason, you do not need to do anything."],
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
