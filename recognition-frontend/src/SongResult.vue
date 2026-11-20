<script setup lang="ts">
import type { Song } from "./types";
import { useArtwork } from "./useArtwork";

const props = defineProps<{ song: Song }>();
const { cover, coverVisible, coverStatus, coverLoaded, coverFailed } =
  useArtwork(() => {
    const path = props.song.artwork;
    return path && /^\/api\/artwork\/[A-Za-z0-9_-]+$/.test(path)
      ? path
      : undefined;
  }, "omit");
</script>

<template>
  <article id="result" class="result" aria-label="Recognized song">
    <div class="cover-frame">
      <div class="cover-placeholder" aria-hidden="true">
        <div class="cover-record">
          <span class="cover-label">
            <svg viewBox="0 0 32 32" width="40" height="40">
              <path
                d="M7 12v8m6-13v18m6-21v24m6-17v10"
                fill="none"
                stroke="currentColor"
                stroke-width="2.5"
                stroke-linecap="round"
              />
            </svg>
          </span>
        </div>
        <span class="cover-caption">EVERY SONG LEAVES A FINGERPRINT</span>
      </div>
      <img
        v-if="cover"
        id="song-cover"
        class="song-cover"
        width="500"
        height="500"
        :src="cover"
        :alt="`Album cover: ${song.album}`"
        referrerpolicy="no-referrer"
        :hidden="!coverVisible"
        @load="coverLoaded"
        @error="coverFailed"
      />
    </div>
    <p class="eyebrow">FOUND YOUR SOUND</p>
    <h3 id="song-title">{{ song.title }}</h3>
    <p id="song-artists">{{ song.artists.join(", ") }}</p>
    <p id="song-album" class="fine">{{ song.album }}</p>
    <p id="cover-status" class="fine" role="status">{{ coverStatus }}</p>
  </article>
</template>
