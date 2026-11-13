/* Return from oauth.telegram.org (accounts:telegram-login).
   Telegram passes the signed login data as URL-safe base64 JSON in the #tgAuthResult fragment,
   which the server never sees. The script turns it into the query of this same address, where the
   server checks the signature. Without the fragment the visitor goes back to the sign-in page. */
(function () {
  "use strict";

  function decode(value) {
    var base64 = value.replace(/-/g, "+").replace(/_/g, "/");
    while (base64.length % 4) base64 += "=";
    var bytes = Uint8Array.from(atob(base64), function (c) { return c.charCodeAt(0); });
    return JSON.parse(new TextDecoder().decode(bytes));
  }

  var page = document.getElementById("telegram-return");
  var match = window.location.hash.match(/tgAuthResult=([A-Za-z0-9_=-]+)/);
  var data = null;
  try {
    data = match ? decode(match[1]) : null;
  } catch (error) {
    data = null;
  }
  if (!data || typeof data !== "object") {
    window.location.replace(page.dataset.login);
    return;
  }
  var query = new URLSearchParams();
  Object.keys(data).forEach(function (name) { query.set(name, String(data[name])); });
  window.location.replace(window.location.pathname + "?" + query.toString());
})();
