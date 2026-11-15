const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

const script = readFileSync(join(__dirname, "../fingerprint_server/static/microphone.js"), "utf8");

function page(applicationKey = "") {
  const elements = new Map();
  const requests = [];
  let microphoneStopped = false;
  let worklet;
  const document = {
    hidden: false,
    addEventListener() {},
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, {
        value: id === "key" ? "personal-key-with-at-least-24-characters" : "",
        textContent: "",
        handlers: {},
        addEventListener(event, handler) { this.handlers[event] = handler; },
      });
      return elements.get(id);
    },
  };
  document.getElementById("application-key").value = applicationKey;
  const context = {
    document,
    window: { isSecureContext: true },
    navigator: {
      mediaDevices: {
        async getUserMedia() {
          return { getTracks: () => [{ stop() { microphoneStopped = true; } }] };
        },
      },
    },
    AudioContext: class {
      sampleRate = 48000;
      audioWorklet = { async addModule() {} };
      async resume() {}
      async close() { this.state = "closed"; }
      createMediaStreamSource() {
        return {
          connect() {
            worklet.port.onmessage({ data: {
              samples: new Float32Array(48000 * 10), done: true,
            } });
          },
          disconnect() {},
        };
      }
    },
    AudioWorkletNode: class {
      port = {};
      constructor() { worklet = this; }
      connect() {}
      disconnect() {}
    },
    Float32Array,
    ArrayBuffer,
    DataView,
    AbortSignal,
    console,
    setInterval() {},
    setTimeout,
    clearTimeout,
    async fetch(url, options) {
      requests.push({ url, options });
      if (url.includes("/recognize")) {
        assert.equal(microphoneStopped, true);
        return { ok: true, async json() { return { status: "no_match", match: null }; } };
      }
      return { ok: true, async json() { return {
        references: { total: 0, partial: 0, full: 0, excluded: 2 },
        submissions: { queued: 0, processing: 0, processed: 0, failed: 0, pending_metadata: 3 },
        worker_available: true,
      }; } };
    },
  };
  vm.runInNewContext(script, context);
  return { elements, requests };
}

for (const applicationKey of ["", "fingerprint-application-key-at-least-24-characters"]) {
  test(`statistics and PCM send the correct tokens (${applicationKey ? "gateway" : "legacy"})`, async () => {
    const { elements, requests } = page(applicationKey);
    await new Promise(setImmediate);
    await elements.get("record").handlers.click();
    assert.equal(requests.length, 2);
    assert.equal(elements.get("references-excluded").textContent, "2");
    assert.equal(elements.get("submissions-pending_metadata").textContent, "3");
    for (const { options } of requests) {
      assert.equal(options.headers["X-API-Key"], "personal-key-with-at-least-24-characters");
      assert.equal(options.headers["X-Application-Key"], applicationKey || undefined);
      assert.equal(options.credentials, "omit");
    }
    assert.equal(requests[1].options.body.byteLength, 48000 * 10 * 4);
    assert.equal(elements.get("status").textContent, "No match. The song may not be indexed, or this section was not sampled.");
    elements.get("references-total").textContent = "123";
    elements.get("application-key").handlers.input();
    assert.equal(elements.get("references-total").textContent, "—");
  });
}
