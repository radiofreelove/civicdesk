/* Oregon Health Plan (OHP) 2027 1-minute check data for static/js/checker.js. */
window.CivicDeskCheck = (function () {
  "use strict";
  return {
    reasons: {
      pregnant: {
        label: "You are pregnant, or you were in the last 12 months.",
        how: "Tell OHP. No proof is needed."
      },
      child: {
        label: "You care for a child 13 or younger.",
        how: "Tell OHP how you are related to the child. Starting in 2028, OHP may ask for a calendar or schedule."
      },
      caregiver: {
        label: "You care for someone with a disability or a serious health condition.",
        how: "Tell OHP. If you do not live with them and are not related, you need to give at least 80 hours of care a month."
      },
      health: {
        label: "You have a serious or disabling health condition.",
        how: "Tell OHP. In 2027, you do not need a note from a doctor."
      },
      ssi: {
        label: "You get SSI or SSDI.",
        how: "Tell OHP. OHA counts this as a qualifying health condition."
      },
      treatment: {
        label: "You are in drug or alcohol treatment, or you were in the last 5 years.",
        how: "Tell OHP. Starting in 2028, OHP may ask for a paper that shows your treatment dates."
      },
      medicare: {
        label: "You have Medicare.",
        how: "The rules do not apply to people with Medicare. OHP can usually check this itself.",
        notApply: true
      },
      disability_ohp: {
        label: "You are applying for OHP because of a disability.",
        how: "The rules do not apply to OHP programs based on disability.",
        notApply: true
      },
      snap: {
        label: "You get SNAP, and you meet SNAP work rules.",
        how: "OHP checks this for you. Being excused from SNAP work rules is not enough."
      },
      tanf: {
        label: "You get TANF, and you meet TANF work rules.",
        how: "OHP checks this for you."
      },
      aian: {
        label: "You are American Indian or Alaska Native.",
        how: "Tell OHP. No proof is needed."
      },
      foster: {
        label: "You were in foster care at age 18, and you are under 26.",
        how: "OHP checks this for you."
      },
      veteran: {
        label: "You are a veteran with a 100% disability rating from the VA.",
        how: "Tell OHP. If OHP asks, send your VA award letter."
      },
      jail: {
        label: "You got out of jail or prison in the last 4 months.",
        how: "Tell OHP. No proof is needed."
      },
      hospital: {
        label: "You stayed at least one night as a patient in a hospital, nursing facility, or behavioral health facility.",
        how: "Tell OHP."
      },
      travel: {
        label: "You were away from home 7 or more days in a month to get medical care for you, your child, or a tax dependent.",
        how: "Tell OHP."
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
      ["When you apply, tell OHP your reason."],
      ["Already applied? Tell OHP in your ONE account at ", { href: "https://benefits.oregon.gov/", text: "benefits.oregon.gov" }, ", or call 800-699-9075."]
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
