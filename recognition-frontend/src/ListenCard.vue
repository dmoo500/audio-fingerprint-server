<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue";
import SongResult from "./SongResult.vue";
import type { Song } from "./types";

const props = defineProps<{
  available: boolean;
  availabilityMessage: string;
}>();
const emit = defineEmits<{ unavailable: []; matched: [] }>();
const label = ref("Find this song");
const title = ref("Good music deserves a name.");
const status = ref("Tap to listen. Keep the music nearby.");
const microphone = ref("");
const song = ref<Song | null>(null);
const recording = ref(false);
const listening = ref(false);
const canCancel = ref(false);
const progress = ref(0);
let controller: AbortController | undefined;

async function listen() {
  if (controller || !props.available) return;
  song.value = null;
  if (
    !window.isSecureContext ||
    !navigator.mediaDevices?.getUserMedia ||
    typeof AudioWorkletNode === "undefined"
  ) {
    title.value = "Your microphone needs a secure connection.";
    status.value =
      "Open this page over HTTPS (or localhost) in a supported browser.";
    return;
  }
  controller = new AbortController();
  recording.value = true;
  label.value = "Getting ready";
  title.value = "Let’s hear what’s playing.";
  status.value = "Allow microphone access when your browser asks.";
  let ticker: ReturnType<typeof setInterval> | undefined;
  try {
    const { bytes, sampleRate } = await FingerprintCapture.record({
      workletUrl: "/assets/capture.js",
      signal: controller.signal,
      onStart(info) {
        microphone.value = info.microphone
          ? `Microphone: ${info.microphone}`
          : "Microphone name unavailable from this browser.";
        listening.value = true;
        canCancel.value = true;
        progress.value = 0;
        label.value = "Listening";
        title.value = "A little closer to the song.";
        status.value = "Listening for ten seconds. Keep the music playing.";
        const started = Date.now();
        ticker = setInterval(() => {
          progress.value = Math.min(10, (Date.now() - started) / 1000);
        }, 100);
      },
    });
    clearInterval(ticker);
    listening.value = false;
    progress.value = 10;
    label.value = "Finding it";
    title.value = "Connecting the dots.";
    status.value = "Your microphone is off. Looking for a match…";
    const response = await fetch(`/api/recognize?sample_rate=${sampleRate}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/octet-stream",
        "X-Recognition-Request": "microphone",
      },
      body: bytes,
      credentials: "same-origin",
      signal: AbortSignal.any([controller.signal, AbortSignal.timeout(30000)]),
    });
    if (response.status >= 500) emit("unavailable");
    const data: unknown = await response.json();
    if (!response.ok) {
      throw new Error(
        isRecord(data) && typeof data.detail === "string"
          ? data.detail
          : "Recognition is temporarily unavailable.",
      );
    }
    if (!isRecord(data))
      throw new Error("The server returned an invalid recognition result.");
    if (data.status === "matched" && isSong(data.match)) {
      song.value = data.match;
      title.value = "That’s the one.";
      status.value =
        response.headers.get("X-Recognition-History") === "unavailable"
          ? "Match found, but it could not be saved to your history."
          : "Here’s the recording we found.";
      emit("matched");
    } else if (data.status === "no_match" && data.match === null) {
      title.value = "Not in tune this time.";
      status.value =
        "Try closer to the music. This song or passage may not be in our collection yet.";
    } else if (data.status === "ambiguous" && data.match === null) {
      title.value = "A few possibilities. No sure answer.";
      status.value = "Try another part of the song with less background noise.";
    } else {
      throw new Error("The server returned an invalid recognition result.");
    }
  } catch (error) {
    const name = error instanceof Error ? error.name : "";
    if (name === "AbortError") {
      title.value = "Listening cancelled.";
      status.value = "Nothing saved. Try again whenever you’re ready.";
    } else {
      title.value = "We couldn’t catch that.";
      status.value =
        name === "NotAllowedError"
          ? "Microphone access was denied. Allow it in your browser to try again."
          : name === "NotFoundError"
            ? "No microphone found. Connect one and try again."
            : name === "TimeoutError"
              ? "Recognition timed out. Please try again."
              : name === "SyntaxError"
                ? "Recognition returned an invalid response. Please try again."
                : error instanceof Error
                  ? error.message
                  : "Recognition is unavailable. Please try again.";
      console.error("Recognition failed", name);
    }
  } finally {
    clearInterval(ticker);
    listening.value = false;
    canCancel.value = false;
    recording.value = false;
    controller = undefined;
    label.value = "Find another song";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isSong(value: unknown): value is Song {
  return (
    isRecord(value) &&
    typeof value.title === "string" &&
    Array.isArray(value.artists) &&
    value.artists.every((artist: unknown) => typeof artist === "string") &&
    typeof value.album === "string" &&
    (value.artwork === undefined || typeof value.artwork === "string")
  );
}

function stop() {
  controller?.abort();
}
onMounted(() => window.addEventListener("pagehide", stop));
onUnmounted(() => {
  window.removeEventListener("pagehide", stop);
  stop();
});
</script>

<template>
  <div id="listen-card" class="listen-card" :class="{ listening }">
    <div class="card-top">
      <span>YOUR NEXT “WHAT’S THIS SONG?”</span><span class="dot"></span>
    </div>
    <div class="listen-orbit">
      <button
        id="listen"
        class="listen-button"
        type="button"
        aria-describedby="privacy availability"
        :disabled="!available || recording"
        @click="listen"
      >
        <svg viewBox="0 0 32 32" width="42" height="42" aria-hidden="true">
          <rect
            x="11"
            y="3"
            width="10"
            height="17"
            rx="5"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
          />
          <path
            d="M7 15v1a9 9 0 0 0 18 0v-1M16 25v5m-5 0h10"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
          />
        </svg>
        <span id="button-label">{{ label }}</span>
      </button>
    </div>
    <h2 id="status-title">{{ title }}</h2>
    <p id="status" role="status" aria-live="polite">{{ status }}</p>
    <p
      id="availability"
      class="availability"
      role="status"
      aria-live="polite"
      :hidden="available"
    >
      {{ availabilityMessage }}
    </p>
    <progress
      id="progress"
      max="10"
      :value="progress"
      aria-label="Recording progress"
      :hidden="!listening"
    ></progress>
    <button
      id="cancel"
      class="cancel-button"
      type="button"
      :hidden="!canCancel"
      @click="controller?.abort()"
    >
      Cancel
    </button>
    <SongResult v-if="song" :song="song" />
    <p id="microphone" class="fine">{{ microphone }}</p>
    <p id="privacy" class="privacy">
      Only listens when you ask. Your short recording is processed on the
      server, never saved or added to the collection.
    </p>
  </div>
</template>
