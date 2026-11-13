// Live support: the panel signals over WebSocket that a thread or an unread counter changed,
// and the page fetches the change with HTMX ("support:thread" / "support:unread" on <body>;
// the staff's lists of chats also follow "support:inbox", another staff member's answer).
// The socket carries no messages. While it is down the page polls every 15 s instead.
// Runs on pages with the [data-support-live] counter; [data-live-thread] names the page's thread.
// The chat in the widget is not one: its signals come to every page of the user.
(() => {
  const POLL_MS = 15000;
  const PING_MS = 20000;
  const RETRY_MAX_MS = 60000;

  let retryMs = 1000;
  let retryTimer = null;
  let pollTimer = null;
  let pingTimer = null;

  const signal = (name) => {
    if (name === "thread" && document.hidden) return; // fetched when the tab is shown again
    document.body.dispatchEvent(new Event(`support:${name}`));
  };

  const openThread = () => document.querySelector("[data-live-thread]")?.dataset.liveThread || "";

  function poll(on) {
    clearInterval(pollTimer);
    pollTimer = on
      ? setInterval(() => {
          signal("unread");
          signal("thread");
        }, POLL_MS)
      : null;
  }

  function connect() {
    clearTimeout(retryTimer);
    const thread = openThread();
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const path = thread ? `/ws/support/${thread}/` : "/ws/support/";
    const ws = new WebSocket(`${scheme}://${location.host}${path}`);
    ws.addEventListener("open", () => {
      retryMs = 1000;
      poll(false);
      // Whatever changed while the socket was down.
      signal("unread");
      signal("thread");
      // Keeps an idle connection open through proxies; the panel does not answer.
      pingTimer = setInterval(() => ws.send(JSON.stringify({ type: "ping" })), PING_MS);
    });
    ws.addEventListener("message", (event) => signal(JSON.parse(event.data).signal));
    ws.addEventListener("close", (event) => {
      clearInterval(pingTimer);
      if (event.code === 4403) return; // not allowed: polling would be refused too
      poll(true);
      retryTimer = setTimeout(connect, retryMs);
      retryMs = Math.min(retryMs * 2, RETRY_MAX_MS);
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    if (!document.querySelector("[data-support-live]")) return;
    if (!("WebSocket" in window)) {
      poll(true);
      return;
    }
    connect();
    document.addEventListener("visibilitychange", () => signal("thread"));
  });
})();
