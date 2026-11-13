// Runs in <head> before CSS loads. Applies the theme and page width, so the page never flashes in
// the wrong one, and quiets page transitions (below).
// The saved theme is "light", "dark" or "system" (the default: follow the device); the saved
// page width is "normal" (the default) or "wide". static/js/ui.js changes them through
// window.lamneziaTheme and window.lamneziaWidth.
(function () {
  var media = window.matchMedia("(prefers-color-scheme: dark)");

  function saved() {
    try {
      var value = localStorage.getItem("theme");
      return value === "light" || value === "dark" ? value : "system";
    } catch (e) {
      return "system";
    }
  }

  function apply(choice) {
    var dark = choice === "dark" || (choice === "system" && media.matches);
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    document.documentElement.dataset.themeChoice = choice;
  }

  window.lamneziaTheme = {
    choice: saved,
    set: function (choice) {
      try {
        if (choice === "system") localStorage.removeItem("theme");
        else localStorage.setItem("theme", choice);
      } catch (e) {}
      apply(choice);
    },
  };

  function savedWidth() {
    try {
      return localStorage.getItem("width") === "wide" ? "wide" : "normal";
    } catch (e) {
      return "normal";
    }
  }

  window.lamneziaWidth = {
    choice: savedWidth,
    set: function (choice) {
      try {
        if (choice === "wide") localStorage.setItem("width", "wide");
        else localStorage.removeItem("width");
      } catch (e) {}
      document.documentElement.dataset.width = choice;
    },
  };
  document.documentElement.dataset.width = savedWidth();

  media.addEventListener("change", function () {
    if (saved() === "system") apply("system");
  });
  apply(saved());
})();

// Page transitions (@view-transition in app.css): one cut short by the next navigation rejects
// its promises. Nothing waits on them, so they are caught here, in a script that runs before the
// page is revealed, instead of ending up in the console as uncaught errors. When the new page does
// not take part in the transition at all (reduced motion, a slow machine), Chrome rejects it before
// the page is revealed and hands out nothing to catch, so that rejection is dropped on the window.
(function () {
  function ignore() {}

  function settle(event) {
    var transition = event.viewTransition;
    if (!transition) return;
    transition.ready.catch(ignore);
    transition.finished.catch(ignore);
    transition.updateCallbackDone.catch(ignore);
  }

  window.addEventListener("pageswap", settle);
  window.addEventListener("pagereveal", settle);
  window.addEventListener("unhandledrejection", function (event) {
    var reason = event.reason;
    if (reason instanceof DOMException && /^Transition was/.test(reason.message)) {
      event.preventDefault();
    }
  });
})();
