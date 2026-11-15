const button = document.getElementById("record");
const keyInput = document.getElementById("key");
const applicationKeyInput = document.getElementById("application-key");
const status = document.getElementById("status");
const result = document.getElementById("result");
const refreshButton = document.getElementById("refresh-statistics");
const statisticsStatus = document.getElementById("statistics-status");
const counterNames = {
  references: ["total", "partial", "full"],
  submissions: ["queued", "processing", "processed", "failed"],
};
let statisticsPending = false;

function clearCounts() {
  for (const [group, names] of Object.entries(counterNames)) {
    for (const name of names) document.getElementById(`${group}-${name}`).textContent = "—";
  }
}

async function refreshStatistics() {
  if (statisticsPending) return;
  const key = keyInput.value.trim();
  const applicationKey = applicationKeyInput.value.trim();
  if (key.length < 24) {
    clearCounts();
    statisticsStatus.textContent = "Enter your recognition key to load counts.";
    return;
  }
  statisticsPending = true;
  refreshButton.disabled = true;
  try {
    const response = await fetch("/api/v1/dejavu/statistics", {
      headers: {
        "X-API-Key": key,
        ...(applicationKey ? { "X-Application-Key": applicationKey } : {}),
      },
      signal: AbortSignal.timeout(10000),
      credentials: "omit",
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || data.message || `Statistics failed: HTTP ${response.status}`);
    for (const [group, names] of Object.entries(counterNames)) {
      for (const name of names) {
        if (!Number.isSafeInteger(data[group]?.[name]) || data[group][name] < 0) {
          throw new Error("Server returned invalid collection counts.");
        }
      }
    }
    if (typeof data.worker_available !== "boolean") throw new Error("Invalid worker status.");
    if (key !== keyInput.value.trim() || applicationKey !== applicationKeyInput.value.trim()) return;
    for (const [group, names] of Object.entries(counterNames)) {
      for (const name of names) {
        document.getElementById(`${group}-${name}`).textContent = data[group][name].toLocaleString();
      }
    }
    statisticsStatus.textContent = data.worker_available
      ? `Updated ${new Date().toLocaleTimeString()}. Automatic refresh every 15 seconds.`
      : "Ingestion worker stopped. Counts are current, but new uploads cannot be processed.";
  } catch (error) {
    if (key !== keyInput.value.trim() || applicationKey !== applicationKeyInput.value.trim()) return;
    clearCounts();
    statisticsStatus.textContent = `Collection counts unavailable: ${error.message}`;
  } finally {
    statisticsPending = false;
    refreshButton.disabled = false;
  }
}

refreshButton.addEventListener("click", refreshStatistics);
keyInput.addEventListener("input", clearCounts);
keyInput.addEventListener("change", refreshStatistics);
applicationKeyInput.addEventListener("input", clearCounts);
applicationKeyInput.addEventListener("change", refreshStatistics);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshStatistics();
});
setInterval(() => {
  if (!document.hidden && keyInput.value.trim().length >= 24) refreshStatistics();
}, 15000);
refreshStatistics();

button.addEventListener("click", async () => {
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    status.textContent = "Microphone access requires localhost or HTTPS and a supported browser.";
    return;
  }
  const key = keyInput.value.trim();
  const applicationKey = applicationKeyInput.value.trim();
  if (key.length < 24) {
    status.textContent = "Enter your recognition API key first.";
    return;
  }
  button.disabled = true;
  result.textContent = "";
  let stream;
  let context;
  let node;
  let source;
  try {
    status.textContent = "Waiting for microphone permission...";
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: false, noiseSuppression: false, autoGainControl: false },
    });
    context = new AudioContext();
    await context.audioWorklet.addModule("/static/capture.js");
    await context.resume();
    const frames = context.sampleRate * 10;
    node = new AudioWorkletNode(context, "bounded-capture", { processorOptions: { frames } });
    source = context.createMediaStreamSource(stream);
    status.textContent = "Recording 10 seconds...";
    const samples = await new Promise((resolve, reject) => {
      const captured = new Float32Array(frames);
      let position = 0;
      const timer = setTimeout(() => reject(new Error("Microphone capture timed out.")), 15000);
      node.onprocessorerror = () => {
        clearTimeout(timer);
        reject(new Error("Microphone audio processing failed."));
      };
      node.port.onmessage = ({ data }) => {
        if (!(data.samples instanceof Float32Array) || position + data.samples.length > frames) {
          clearTimeout(timer);
          reject(new Error("Invalid microphone capture."));
          return;
        }
        captured.set(data.samples, position);
        position += data.samples.length;
        if (data.done) {
          clearTimeout(timer);
          if (position !== frames) reject(new Error("Incomplete microphone capture."));
          else resolve(captured);
        }
      };
      source.connect(node);
      node.connect(context.destination); // The processor outputs silence, never microphone audio.
    });
    stream.getTracks().forEach((track) => track.stop());
    node.disconnect();
    source.disconnect();
    const sampleRate = context.sampleRate;
    await context.close();
    context = null;
    status.textContent = "Recognizing...";
    // Explicit little-endian wire encoding also works on non-little-endian clients.
    const bytes = new ArrayBuffer(samples.length * 4);
    const view = new DataView(bytes);
    samples.forEach((value, index) => view.setFloat32(index * 4, value, true));
    const response = await fetch(`/api/v1/dejavu/recognize?sample_rate=${sampleRate}&channels=1`, {
      method: "POST",
      headers: {
        "Content-Type": "application/octet-stream",
        "X-API-Key": key,
        ...(applicationKey ? { "X-Application-Key": applicationKey } : {}),
      },
      body: bytes,
      signal: AbortSignal.timeout(20000),
      credentials: "omit",
    });
    const document = await response.json();
    if (!response.ok) throw new Error(document.detail || document.message || `Recognition failed: HTTP ${response.status}`);
    status.textContent = document.status === "matched" ? "Match found."
      : document.status === "ambiguous" ? "Ambiguous: no reliable single match."
        : "No match. The song may not be indexed, or this section was not sampled.";
    result.textContent = JSON.stringify(document.match, null, 2);
  } catch (error) {
    console.error("Microphone recognition failed", error.name);
    status.textContent = error.message || "Microphone recognition failed.";
  } finally {
    stream?.getTracks().forEach((track) => track.stop());
    node?.disconnect();
    source?.disconnect();
    if (context && context.state !== "closed") await context.close();
    button.disabled = false;
  }
});
