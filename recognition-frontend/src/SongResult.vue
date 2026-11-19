<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from "vue";
import type { Song } from "./types";

const props = defineProps<{ song: Song }>();
const cover = ref("");
const coverVisible = ref(false);
const coverStatus = ref("");
let coverController: AbortController | undefined;

watch(() => props.song, (song, _previous, onCleanup) => {
  const controller = new AbortController();
  coverController = controller;
  const { signal } = controller;
  onCleanup(() => controller.abort());
  cover.value = "";
  coverVisible.value = false;
  coverStatus.value = "";
  if (song.artwork && /^\/api\/artwork\/[A-Za-z0-9_-]+$/.test(song.artwork)) {
    void loadCover(song.artwork, signal);
  }
}, { immediate: true });

async function loadCover(path: string, signal: AbortSignal) {
  coverStatus.value = "Looking for album artwork…";
  try {
    for (let attempt = 0; attempt < 20; attempt++) {
      const response = await fetch(path, {
        credentials: "omit",
        signal: AbortSignal.any([signal, AbortSignal.timeout(10000)]),
      });
      const data: unknown = await response.json();
      if (!response.ok || !data || typeof data !== "object" || !("status" in data)) {
        throw new Error("Album artwork is temporarily unavailable.");
      }
      signal.throwIfAborted();
      if (data.status === "ready" && "url" in data && typeof data.url === "string") {
        const url = new URL(data.url);
        if (url.protocol !== "https:" || url.username || url.password
          || !(url.hostname === "archive.org" || url.hostname.endsWith(".archive.org")
            || url.hostname === "coverartarchive.org")) {
          throw new Error("Invalid album artwork address.");
        }
        cover.value = url.href;
        return;
      }
      if (data.status === "missing") {
        coverStatus.value = "No matching album artwork available.";
        return;
      }
      if (data.status !== "pending") throw new Error("Album artwork is temporarily unavailable.");
      await new Promise<void>((resolve, reject) => {
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
    coverStatus.value = error instanceof Error ? error.message : "Album artwork is temporarily unavailable.";
    console.error("Album artwork failed", error instanceof Error ? error.name : "Unknown error");
  }
}

function stopArtwork() {
  coverController?.abort();
}

function coverLoaded() {
  if (coverController?.signal.aborted) return;
  coverVisible.value = true;
  coverStatus.value = "Cover art via Cover Art Archive.";
}

function coverFailed() {
  if (coverController?.signal.aborted) return;
  coverVisible.value = false;
  coverStatus.value = "Album artwork could not be loaded.";
}
onMounted(() => window.addEventListener("pagehide", stopArtwork));
onUnmounted(() => window.removeEventListener("pagehide", stopArtwork));
</script>

<template>
  <article id="result" class="result" aria-label="Recognized song">
    <div class="cover-frame">
      <div class="cover-placeholder" aria-hidden="true">
        <div class="cover-record"><span class="cover-label">
          <svg viewBox="0 0 32 32" width="40" height="40">
            <path d="M7 12v8m6-13v18m6-21v24m6-17v10"
              fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"/>
          </svg>
        </span></div>
        <span class="cover-caption">EVERY SONG LEAVES A FINGERPRINT</span>
      </div>
      <img v-if="cover" id="song-cover" class="song-cover" width="500" height="500"
        :src="cover" :alt="`Album cover: ${song.album}`" referrerpolicy="no-referrer"
        :hidden="!coverVisible"
        @load="coverLoaded" @error="coverFailed">
    </div>
    <p class="eyebrow">FOUND YOUR SOUND</p>
    <h3 id="song-title">{{ song.title }}</h3>
    <p id="song-artists">{{ song.artists.join(", ") }}</p>
    <p id="song-album" class="fine">{{ song.album }}</p>
    <p id="cover-status" class="fine" role="status">{{ coverStatus }}</p>
  </article>
</template>
