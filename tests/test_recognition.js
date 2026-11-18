const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");
const script = readFileSync(join(__dirname, "../fingerprint_server/recognition/static/app.js"), "utf8");

function page({ status = "matched", failure, captureFailure, countFailure = false, statistics, artwork, artworkResponse } = {}) {
  const elements = new Map();
  const requests = [];
  const timers = new Map();
  const documentHandlers = {};
  const windowHandlers = {};
  let nextTimer = 0;
  let captureSignal;
  const get = (id) => {
    if (!elements.has(id)) elements.set(id, {
      hidden: true, disabled: false, textContent: "", handlers: {},
      removeAttribute(name) { delete this[name]; },
      classList: { add() {}, remove() {} },
      addEventListener(event, handler) { this.handlers[event] = handler; },
    });
    return elements.get(id);
  };
  const context = {
    document: {
      hidden: false, getElementById: get,
      addEventListener(event, handler) { documentHandlers[event] = handler; },
    },
    window: {
      isSecureContext: true,
      addEventListener(event, handler) { windowHandlers[event] = handler; },
    },
    navigator: { mediaDevices: { getUserMedia() {} } },
    AudioWorkletNode() {},
    AbortController, AbortSignal, URL,
    setInterval() {}, clearInterval() {},
    setTimeout(callback, delay) { timers.set(++nextTimer, { callback, delay }); return nextTimer; },
    clearTimeout(id) { timers.delete(id); },
    console: { error() {} },
    FingerprintCapture: {
      async record({ onStart, signal }) {
        captureSignal = signal;
        if (captureFailure) throw captureFailure;
        onStart({ microphone: "Selected test microphone" });
        return { bytes: new ArrayBuffer(16), sampleRate: 48000 };
      },
    },
    async fetch(url, options) {
      requests.push({ url, options });
      if (url.startsWith("/api/artwork/")) return {
        ok: true, async json() {
          return artworkResponse ? artworkResponse() : { status: "ready", url: artwork };
        },
      };
      if (url === "/api/statistics") return statistics ? statistics() : {
        ok: !countFailure, async json() { return { recordings: countFailure ? -1 : 1234 }; },
      };
      return {
        ok: !failure,
        async json() {
          if (failure) return { detail: failure };
          return {
            status,
            match: status === "matched"
              ? { title: "<img onerror=alert(1)>", artists: ["Example artist"], album: "Example album",
                ...(artwork ? { artwork: "/api/artwork/opaque" } : {}) }
              : null,
          };
        },
      };
    },
  };
  vm.runInNewContext(script, context);
  return {
    elements, requests, timers, document: context.document, documentHandlers, windowHandlers,
    captureSignal: () => captureSignal,
  };
}

test("artwork loads separately without hiding the song and cannot overwrite a later capture", async () => {
  const { elements } = page({ artwork: "https://archive.org/example.jpg" });
  await elements.get("listen").handlers.click();
  await new Promise(setImmediate);
  const image = elements.get("song-cover");
  assert.equal(elements.get("result").hidden, false);
  assert.equal(image.src, "https://archive.org/example.jpg");
  assert.equal(image.hidden, true);
  image.onload();
  assert.equal(image.hidden, false);
  image.onerror();
  assert.equal(image.hidden, true);
  assert.match(elements.get("cover-status").textContent, /could not be loaded/);
  const oldOnload = image.onload;
  await elements.get("listen").handlers.click();
  oldOnload();
  assert.equal(image.hidden, true);
});

test("untrusted artwork cannot become an image source", async () => {
  const { elements } = page({ artwork: "https://evil.example/cover.jpg" });
  await elements.get("listen").handlers.click();
  await new Promise(setImmediate);
  assert.equal(elements.get("song-cover").src, undefined);
  assert.equal(elements.get("result").hidden, false);
  assert.match(elements.get("cover-status").textContent, /Invalid/);
});

test("pending artwork is checked after one second before backing off to three seconds", async () => {
  let pending = true;
  const view = page({
    artwork: "https://archive.org/example.jpg",
    artworkResponse: () => pending
      ? { status: "pending", url: null }
      : { status: "ready", url: "https://archive.org/example.jpg" },
  });
  await view.elements.get("listen").handlers.click();
  await new Promise(setImmediate);
  for (const delay of [1000, 1000, 1000, 3000]) {
    const [id, timer] = [...view.timers.entries()].find(([, timer]) => timer.delay !== 60000);
    assert.equal(timer.delay, delay);
    view.timers.delete(id);
    timer.callback();
    await new Promise(setImmediate);
  }
  pending = false;
  const [id, timer] = [...view.timers.entries()].find(([, timer]) => timer.delay !== 60000);
  view.timers.delete(id);
  timer.callback();
  await new Promise(setImmediate);
  assert.equal(view.elements.get("song-cover").src, "https://archive.org/example.jpg");
  assert.equal(view.elements.get("result").hidden, false);
});

test("collection count refreshes every minute, pauses when hidden and resumes on return", async () => {
  let recordings = 1234;
  const view = page({ statistics: async () => ({
    ok: true, async json() { return { recordings }; },
  }) });
  await new Promise(setImmediate);
  assert.equal(view.timers.size, 1);
  const timer = [...view.timers.values()][0];
  assert.equal(timer.delay, 60000);
  recordings = 1250;
  await timer.callback();
  assert.equal(view.elements.get("recordings").textContent, "1,250");
  assert.equal(view.timers.size, 1);
  view.document.hidden = true;
  await view.documentHandlers.visibilitychange();
  assert.equal(view.timers.size, 0);
  assert.equal(view.requests.length, 2);
  view.document.hidden = false;
  await view.documentHandlers.visibilitychange();
  assert.equal(view.requests.length, 3);
  view.windowHandlers.pagehide();
  assert.equal(view.timers.size, 0);
  await view.windowHandlers.pageshow();
  assert.equal(view.requests.length, 4);
  assert.equal(view.timers.size, 1);
});

test("collection refresh retries after failure and never overlaps an active request", async () => {
  let finish;
  const view = page({ statistics: () => new Promise((resolve) => { finish = resolve; }) });
  await view.documentHandlers.visibilitychange();
  assert.equal(view.requests.length, 1);
  finish({ ok: false, async json() { return {}; } });
  await new Promise(setImmediate);
  assert.match(view.elements.get("count-status").textContent, /unavailable/);
  assert.equal(view.timers.size, 1);
  const retry = [...view.timers.values()][0].callback();
  assert.equal(view.requests.length, 2);
  finish({ ok: true, async json() { return { recordings: 1251 }; } });
  await retry;
  assert.equal(view.elements.get("recordings").textContent, "1,251");
  assert.equal(view.timers.size, 1);
});

test("public page uses no credentials and renders only a song card, not raw match JSON", async () => {
  const { elements, requests } = page();
  await new Promise(setImmediate);
  assert.equal(elements.get("recordings").textContent, "1,234");
  await elements.get("listen").handlers.click();
  assert.equal(elements.get("result").hidden, false);
  assert.equal(elements.get("song-title").textContent, "<img onerror=alert(1)>");
  assert.equal(elements.get("song-artists").textContent, "Example artist");
  assert.equal(elements.get("microphone").textContent, "Microphone: Selected test microphone");
  assert.equal(elements.get("listen").disabled, false);
  assert.equal(requests.length, 2);
  const request = requests[1];
  assert.equal(request.url, "/api/recognize?sample_rate=48000");
  assert.equal(request.options.credentials, "omit");
  assert.deepEqual(Object.keys(request.options.headers).sort(), ["Content-Type", "X-Recognition-Request"]);
});

for (const status of ["no_match", "ambiguous", "invalid"]) {
  test(`recognition handles ${status} explicitly`, async () => {
    const { elements } = page({ status });
    await elements.get("listen").handlers.click();
    assert.equal(elements.get("result").hidden, true);
    assert.equal(elements.get("listen").disabled, false);
    assert.match(elements.get("status-title").textContent,
      status === "no_match" ? /Not in tune/ : status === "ambiguous" ? /No sure answer/ : /couldn’t catch/);
  });
}

test("a missing collection count is not displayed as zero", async () => {
  const { elements } = page({ countFailure: true });
  await new Promise(setImmediate);
  assert.equal(elements.get("recordings").textContent, "—");
  assert.match(elements.get("count-status").textContent, /unavailable/);
});

test("upstream throttling is shown, never disguised as no-match", async () => {
  const { elements } = page({ failure: "Please wait a minute." });
  await elements.get("listen").handlers.click();
  assert.equal(elements.get("status").textContent, "Please wait a minute.");
  assert.equal(elements.get("result").hidden, true);
});

test("microphone denial restores the button and does not upload", async () => {
  const denied = new Error("Permission denied");
  denied.name = "NotAllowedError";
  const { elements, requests } = page({ captureFailure: denied });
  await elements.get("listen").handlers.click();
  assert.match(elements.get("status").textContent, /Microphone access was denied/);
  assert.equal(elements.get("listen").disabled, false);
  assert.equal(requests.length, 1);
});
