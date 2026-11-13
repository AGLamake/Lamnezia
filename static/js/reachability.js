// Checks from the user's own device whether VPN servers answer over UDP or TCP.
// An element with data-probe="stun:host:port https://host:port/" (UDP and TCP addresses of one
// server, separated by spaces) is shown when any of them does not answer.
// The result stays in the page: nothing is sent to the panel and nothing is stored.
(() => {
  const TIMEOUT_MS = 4000;

  function probe(url) {
    return url.startsWith("https://") ? probeTcp(url) : probeUdp(url);
  }

  // A request to the port of XRay (Reality). Reality answers with the TLS of another site, so
  // the request fails, but fast; a closed port fails fast too and cannot be told apart. Only
  // silence until the timeout means "unreachable". The page's CSP allows exactly these hosts.
  function probeTcp(url) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
    return fetch(url, { mode: "no-cors", cache: "no-store", credentials: "omit", signal: controller.signal })
      .then(() => "reachable")
      .catch(() => (controller.signal.aborted ? "unreachable" : "reachable"))
      .finally(() => clearTimeout(timer));
  }

  // A STUN request to the server goes out through WebRTC ICE gathering. "reachable": the
  // server answered; "unreachable": no answer in time or the address failed; "unknown": the
  // browser gathers no candidates at all (WebRTC is off or blocked), so nothing can be told.
  function probeUdp(url) {
    return new Promise((resolve) => {
      let connection;
      try {
        connection = new RTCPeerConnection({ iceServers: [{ urls: url }] });
      } catch {
        resolve("unknown");
        return;
      }
      let candidates = 0;
      let failed = false;
      let done = false;
      const finish = (state) => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        connection.close();
        resolve(state);
      };
      const timer = setTimeout(() => finish(candidates ? "unreachable" : "unknown"), TIMEOUT_MS);
      connection.addEventListener("icecandidate", ({ candidate }) => {
        if (!candidate || !candidate.candidate) {
          // Gathering ended before the timeout. Without a server-reflexive candidate the
          // server either failed or answered with the device's own address (no NAT), which
          // the browser drops as a duplicate of the host candidate.
          finish(!candidates ? "unknown" : failed ? "unreachable" : "reachable");
        } else if (/ typ srflx/.test(candidate.candidate)) {
          finish("reachable");
        } else {
          candidates += 1;
        }
      });
      connection.addEventListener("icecandidateerror", () => {
        failed = true;
      });
      connection.createDataChannel("probe");
      connection
        .createOffer()
        .then((offer) => connection.setLocalDescription(offer))
        .catch(() => finish("unknown"));
    });
  }

  // One check per address, even when several warnings share it.
  function run() {
    const checks = new Map();
    const check = (url) => {
      if (!checks.has(url)) checks.set(url, probe(url));
      return checks.get(url);
    };
    for (const element of document.querySelectorAll("[data-probe]")) {
      const urls = element.dataset.probe.split(" ").filter(Boolean);
      Promise.all(urls.map(check)).then((states) => {
        const down = states.includes("unreachable");
        element.dataset.probeState = down ? "unreachable" : states.includes("reachable") ? "reachable" : "unknown";
        element.hidden = !down;
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", run);
  } else {
    run();
  }
})();
