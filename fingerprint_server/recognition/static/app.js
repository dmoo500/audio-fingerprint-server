const listen = document.getElementById("listen");
const cancel = document.getElementById("cancel");
const label = document.getElementById("button-label");
const title = document.getElementById("status-title");
const status = document.getElementById("status");
const result = document.getElementById("result");
const progress = document.getElementById("progress");
const card = document.getElementById("listen-card");
let controller;
let countTimer;
let countLoading = false;
let coverController;
const cover = document.getElementById("song-cover");
const coverStatus = document.getElementById("cover-status");

async function loadCover(path, album, signal) {
  coverStatus.textContent = "Looking for album artwork…";
  try {
    for (let attempt = 0; attempt < 20; attempt++) {
      const response = await fetch(path, {
        credentials: "omit", signal: AbortSignal.any([signal, AbortSignal.timeout(10000)]),
      });
      const data = await response.json();
      if (!response.ok) throw new Error("Album artwork is temporarily unavailable.");
      signal.throwIfAborted();
      if (data.status === "ready" && typeof data.url === "string") {
        const url = new URL(data.url);
        if (url.protocol !== "https:" || url.username || url.password
          || !(url.hostname === "archive.org" || url.hostname.endsWith(".archive.org")
            || url.hostname === "coverartarchive.org")) {
          throw new Error("Invalid album artwork address.");
        }
        cover.onload = () => {
          if (signal.aborted) return;
          cover.hidden = false;
          coverStatus.textContent = "Cover art via Cover Art Archive.";
        };
        cover.onerror = () => {
          if (signal.aborted) return;
          cover.hidden = true;
          coverStatus.textContent = "Album artwork could not be loaded.";
        };
        cover.alt = `Album cover: ${album}`;
        cover.src = url.href;
        return;
      }
      if (data.status === "missing") {
        coverStatus.textContent = "No matching album artwork available.";
        return;
      }
      if (data.status !== "pending") throw new Error("Album artwork is temporarily unavailable.");
      await new Promise((resolve, reject) => {
        const abort = () => { clearTimeout(timer); reject(signal.reason); };
        const timer = setTimeout(() => {
          signal.removeEventListener("abort", abort);
          resolve();
        }, attempt < 3 ? 1000 : 3000);
        signal.addEventListener("abort", abort, { once: true });
        if (signal.aborted) abort();
      });
    }
    throw new Error("Album artwork is still being prepared. Try again later.");
  } catch (error) {
    if (signal.aborted) return;
    coverStatus.textContent = error.message || "Album artwork is temporarily unavailable.";
    console.error("Album artwork failed", error.name);
  }
}

async function loadCount() {
  clearTimeout(countTimer);
  if (document.hidden || countLoading) return;
  countLoading = true;
  const count = document.getElementById("recordings");
  const message = document.getElementById("count-status");
  try {
    const response = await fetch("/api/statistics", { credentials: "omit", signal: AbortSignal.timeout(10000) });
    const data = await response.json();
    if (!response.ok || !Number.isSafeInteger(data.recordings) || data.recordings < 0) {
      throw new Error("The collection count is temporarily unavailable.");
    }
    count.textContent = data.recordings.toLocaleString();
    message.textContent = "Only searchable recordings are counted. Not every song or passage is covered.";
  } catch (error) {
    count.textContent = "—";
    message.textContent = "Collection count unavailable right now. You can still try recognition.";
    console.error("Collection count failed", error.name);
  } finally {
    countLoading = false;
    if (!document.hidden) countTimer = setTimeout(loadCount, 60000);
  }
}

listen.addEventListener("click", async () => {
  if (controller) return;
  coverController?.abort();
  cover.hidden = true;
  cover.onload = null;
  cover.onerror = null;
  cover.removeAttribute("src");
  coverStatus.textContent = "";
  result.hidden = true;
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia || typeof AudioWorkletNode === "undefined") {
    title.textContent = "Your microphone needs a secure connection.";
    status.textContent = "Open this page over HTTPS (or localhost) in a supported browser.";
    return;
  }
  controller = new AbortController();
  listen.disabled = true;
  label.textContent = "Getting ready";
  title.textContent = "Let’s hear what’s playing.";
  status.textContent = "Allow microphone access when your browser asks.";
  let ticker;
  try {
    const { bytes, sampleRate } = await FingerprintCapture.record({
      workletUrl: "/assets/capture.js",
      signal: controller.signal,
      onStart({ microphone }) {
        document.getElementById("microphone").textContent = microphone
          ? `Microphone: ${microphone}` : "Microphone name unavailable from this browser.";
        cancel.hidden = false;
        progress.hidden = false;
        progress.value = 0;
        card.classList.add("listening");
        label.textContent = "Listening";
        title.textContent = "A little closer to the song.";
        status.textContent = "Listening for ten seconds. Keep the music playing.";
        const started = Date.now();
        ticker = setInterval(() => { progress.value = Math.min(10, (Date.now() - started) / 1000); }, 100);
      },
    });
    clearInterval(ticker);
    card.classList.remove("listening");
    progress.value = 10;
    label.textContent = "Finding it";
    title.textContent = "Connecting the dots.";
    status.textContent = "Your microphone is off. Looking for a match…";
    const response = await fetch(`/api/recognize?sample_rate=${sampleRate}`, {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream", "X-Recognition-Request": "microphone" },
      body: bytes,
      credentials: "omit",
      signal: AbortSignal.any([controller.signal, AbortSignal.timeout(30000)]),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Recognition is temporarily unavailable.");
    if (data.status === "matched" && data.match && typeof data.match.title === "string"
      && Array.isArray(data.match.artists) && data.match.artists.every((artist) => typeof artist === "string")
      && typeof data.match.album === "string") {
      document.getElementById("song-title").textContent = data.match.title;
      document.getElementById("song-artists").textContent = data.match.artists.join(", ");
      document.getElementById("song-album").textContent = data.match.album;
      result.hidden = false;
      if (typeof data.match.artwork === "string" && /^\/api\/artwork\/[A-Za-z0-9_-]+$/.test(data.match.artwork)) {
        coverController = new AbortController();
        void loadCover(data.match.artwork, data.match.album, coverController.signal);
      }
      title.textContent = "That’s the one.";
      status.textContent = "Here’s the recording we found.";
    } else if (data.status === "no_match" && data.match === null) {
      title.textContent = "Not in tune this time.";
      status.textContent = "Try closer to the music. This song or passage may not be in our collection yet.";
    } else if (data.status === "ambiguous" && data.match === null) {
      title.textContent = "A few possibilities. No sure answer.";
      status.textContent = "Try another part of the song with less background noise.";
    } else {
      throw new Error("The server returned an invalid recognition result.");
    }
  } catch (error) {
    if (error.name === "AbortError") {
      title.textContent = "Listening cancelled.";
      status.textContent = "Nothing saved. Try again whenever you’re ready.";
    } else {
      title.textContent = "We couldn’t catch that.";
      status.textContent = error.name === "NotAllowedError" ? "Microphone access was denied. Allow it in your browser to try again."
        : error.name === "NotFoundError" ? "No microphone found. Connect one and try again."
          : error.name === "TimeoutError" ? "Recognition timed out. Please try again."
            : error.name === "SyntaxError" ? "Recognition returned an invalid response. Please try again."
              : error.message || "Recognition is unavailable. Please try again.";
      console.error("Recognition failed", error.name);
    }
  } finally {
    clearInterval(ticker);
    card.classList.remove("listening");
    progress.hidden = true;
    cancel.hidden = true;
    controller = undefined;
    listen.disabled = false;
    label.textContent = "Find another song";
  }
});

cancel.addEventListener("click", () => controller?.abort());
window.addEventListener("pagehide", () => {
  controller?.abort();
  coverController?.abort();
  clearTimeout(countTimer);
});
window.addEventListener("pageshow", loadCount);
document.addEventListener("visibilitychange", loadCount);
loadCount();
