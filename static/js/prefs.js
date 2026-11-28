/* Runs synchronously before first paint so saved reading settings apply without a flash.
   Saved on this device only; "Forget everything" in Reading settings erases them. */
(function () {
  var root = document.documentElement;
  try {
    if (window.self !== window.top) {
      root.hidden = true;
      window.top.location = window.self.location.href;
    }
  } catch (e) {
    root.hidden = true;
  }
  root.classList.add("js");
  var prefs = null;
  try {
    prefs = JSON.parse(window.localStorage.getItem("civicdesk:prefs") || "null");
  } catch (e) {
    prefs = null;
  }
  if (!prefs || typeof prefs !== "object") return;
  var allowed = {
    theme: ["auto", "warm", "light", "dark", "contrast"],
    size: ["m", "l", "xl"],
    spacing: ["normal", "wide"],
    font: ["system", "hyper"],
    brief: ["off", "on"],
    shiftexit: ["on", "off"]
  };
  Object.keys(allowed).forEach(function (key) {
    if (allowed[key].indexOf(prefs[key]) !== -1) {
      root.setAttribute("data-" + key, prefs[key]);
    }
  });
})();
