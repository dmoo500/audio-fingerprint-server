import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const script = readFileSync(
  new URL("../fingerprint_server/recognition/static/recording.js", import.meta.url),
  "utf8",
);

function recorder({ mode = "complete" } = {}) {
  let node;
  let stopped = 0;
  let closed = 0;
  let start;
  const started = new Promise((resolve) => { start = resolve; });
  const context = {
    AbortController, DOMException, Float32Array, ArrayBuffer, DataView,
    setTimeout, clearTimeout,
    navigator: { mediaDevices: {
      async getUserMedia() {
        return {
          getTracks: () => [{ stop() { stopped++; } }],
          getAudioTracks: () => [{ label: "Selected test microphone" }],
        };
      },
    } },
    AudioContext: class {
      sampleRate = 8000;
      audioWorklet = { async addModule() {} };
      async resume() {}
      async close() { closed++; this.state = "closed"; }
      createMediaStreamSource() {
        return {
          connect() {
            start();
            if (mode === "complete") {
              const samples = new Float32Array(80000);
              samples[0] = 0.5;
              node.port.onmessage({ data: { samples, done: true } });
            } else if (mode === "invalid") {
              node.port.onmessage({ data: { samples: "not audio", done: true } });
            } else if (mode === "incomplete") {
              node.port.onmessage({ data: { samples: new Float32Array(2), done: true } });
            } else if (mode === "processor-error") {
              node.onprocessorerror();
            }
          },
          disconnect() {},
        };
      }
    },
    AudioWorkletNode: class {
      port = {};
      constructor() { node = this; }
      connect() {}
      disconnect() {}
    },
  };
  vm.runInNewContext(script, context);
  return {
    capture: context.FingerprintCapture.record,
    started,
    cleaned: () => ({ stopped, closed }),
  };
}

test("shared recorder returns exactly ten seconds of little-endian PCM after closing microphone", async () => {
  const { capture, cleaned } = recorder();
  let microphone;
  const { bytes, sampleRate } = await capture({
    workletUrl: "/assets/capture.js",
    onStart(info) { microphone = info.microphone; },
  });
  assert.equal(microphone, "Selected test microphone");
  assert.equal(sampleRate, 8000);
  assert.equal(bytes.byteLength, 80000 * 4);
  assert.equal(new DataView(bytes).getFloat32(0, true), 0.5);
  assert.deepEqual(cleaned(), { stopped: 1, closed: 1 });
});

for (const mode of ["invalid", "incomplete", "processor-error"]) {
  test(`shared recorder cleans up on ${mode}`, async () => {
    const { capture, cleaned } = recorder({ mode });
    await assert.rejects(capture({ workletUrl: "/assets/capture.js" }));
    assert.deepEqual(cleaned(), { stopped: 1, closed: 1 });
  });
}

test("cancellation stops capture and closes resources without yielding audio", async () => {
  const { capture, cleaned, started } = recorder({ mode: "pending" });
  const controller = new AbortController();
  const pending = capture({ workletUrl: "/assets/capture.js", signal: controller.signal });
  await started;
  controller.abort();
  await assert.rejects(pending, { name: "AbortError" });
  assert.deepEqual(cleaned(), { stopped: 1, closed: 1 });
});
