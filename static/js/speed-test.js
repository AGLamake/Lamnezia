// Measures the download and upload speed to every port of a running browser speed test.
// An element [data-speed-test] with data-hash="sha-256 hex"> holds a row [data-speed-url] per port with
// cells [data-download] and [data-upload], a button [data-speed-run] and a note
// [data-speed-unsupported]; the texts come from its data-text-* attributes (translated).
// WebTransport goes over HTTP/3, that is UDP to the very port. The result stays in the page.
(() => {
  const SECONDS = 8;
  const CONNECT_MS = 6000;
  const STREAMS = 4;
  const CHUNK = 1 << 20;
  const DOWNLOAD = 0x44; // "D"
  const UPLOAD = 0x55; // "U"

  const now = () => performance.now();
  const mbits = (bytes, ms) => ((bytes * 8) / (ms / 1000) / 1e6).toFixed(1);

  function hashBytes(hex) {
    return new Uint8Array(hex.match(/../g).map((pair) => parseInt(pair, 16)));
  }

  async function connect(url, hash) {
    const transport = new WebTransport(url, {
      serverCertificateHashes: [{ algorithm: "sha-256", value: hashBytes(hash) }],
    });
    transport.closed.catch(() => {});
    let timer;
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error("timeout")), CONNECT_MS);
    });
    try {
      await Promise.race([transport.ready, timeout]);
    } catch (error) {
      transport.close();
      throw error;
    } finally {
      clearTimeout(timer);
    }
    return transport;
  }

  async function request(transport, body) {
    const stream = await transport.createBidirectionalStream();
    const writer = stream.writable.getWriter();
    await writer.write(body);
    await writer.close();
    return stream.readable.getReader();
  }

  // Bytes are counted as they arrive, until the deadline.
  async function downloading(transport, deadline, counter) {
    const body = new Uint8Array(5);
    body[0] = DOWNLOAD;
    new DataView(body.buffer).setUint32(1, CHUNK);
    while (now() < deadline) {
      const reader = await request(transport, body);
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        if (now() > deadline) return;
        counter.bytes += value.byteLength;
      }
    }
  }

  // Only what the server says it has received counts.
  async function uploading(transport, deadline, counter) {
    const body = new Uint8Array(CHUNK);
    body[0] = UPLOAD;
    while (now() < deadline) {
      const reader = await request(transport, body);
      const answer = new Uint8Array(8);
      let length = 0;
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        answer.set(value.subarray(0, 8 - length), length);
        length = Math.min(8, length + value.byteLength);
      }
      if (length === 8 && now() <= deadline) {
        counter.bytes += Number(new DataView(answer.buffer).getBigUint64(0));
      }
    }
  }

  // The speed in Mbit/s over SECONDS with STREAMS parallel requests.
  async function measure(transport, direction) {
    const counter = { bytes: 0 };
    const started = now();
    const deadline = started + SECONDS * 1000;
    const loops = Array.from({ length: STREAMS }, () => direction(transport, deadline, counter));
    await Promise.race([
      Promise.all(loops),
      new Promise((resolve) => setTimeout(resolve, SECONDS * 1000 + 500)),
    ]);
    if (now() < deadline) throw new Error("ended early");
    return mbits(counter.bytes, deadline - started);
  }

  async function test(row, hash, texts) {
    const download = row.querySelector("[data-download]");
    const upload = row.querySelector("[data-upload]");
    upload.textContent = "";
    download.textContent = texts.measuring;
    let transport;
    try {
      transport = await connect(row.dataset.speedUrl, hash);
    } catch {
      download.textContent = texts.blocked;
      return;
    }
    try {
      download.textContent = `${await measure(transport, downloading)} ${texts.unit}`;
      upload.textContent = texts.measuring;
      upload.textContent = `${await measure(transport, uploading)} ${texts.unit}`;
    } catch {
      for (const cell of [download, upload]) {
        if (cell.textContent === texts.measuring) cell.textContent = texts.failed;
      }
    } finally {
      transport.close();
    }
  }

  function setUp(section) {
    const button = section.querySelector("[data-speed-run]");
    if (typeof WebTransport === "undefined") {
      section.querySelector("[data-speed-unsupported]").hidden = false;
      button.disabled = true;
      return;
    }
    const texts = {
      measuring: section.dataset.textMeasuring,
      blocked: section.dataset.textBlocked,
      failed: section.dataset.textFailed,
      unit: section.dataset.textUnit,
    };
    button.addEventListener("click", async () => {
      button.disabled = true;
      for (const row of section.querySelectorAll("[data-speed-url]")) {
        await test(row, section.dataset.hash, texts);
      }
      button.disabled = false;
    });
  }

  function run() {
    document.querySelectorAll("[data-speed-test]").forEach(setUp);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", run);
  } else {
    run();
  }
})();
