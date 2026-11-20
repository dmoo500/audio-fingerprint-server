<script setup lang="ts">
import { ExternalLink } from "@lucide/vue";
import { onMounted, onUnmounted, ref, watch } from "vue";
import MusicAssistantMark from "./MusicAssistantMark.vue";

const linkLabel = "Open in Music Assistant Share (new tab)";
const props = defineProps<{
  title: string;
  artists: string[];
  album?: string;
  coverUrl?: string;
  lazy?: boolean;
  compact?: boolean;
}>();
const container = ref<HTMLElement | null>(null);
const visible = ref(!props.lazy);
const href = ref("");
const failed = ref(false);
const tooltipDismissed = ref(false);
let observer: IntersectionObserver | undefined;

onMounted(() => {
  if (!props.lazy) return;
  if (!container.value || typeof IntersectionObserver === "undefined") {
    visible.value = true;
    return;
  }
  observer = new IntersectionObserver(([entry]) => {
    if (!entry?.isIntersecting) return;
    visible.value = true;
    observer?.disconnect();
  });
  observer.observe(container.value);
});
onUnmounted(() => observer?.disconnect());

watch(
  () =>
    [
      visible.value,
      props.title,
      props.artists,
      props.album,
      props.coverUrl,
    ] as const,
  async ([ready, title, artists, album, coverUrl], previous, onCleanup) => {
    if (
      !coverUrl ||
      title !== previous?.[1] ||
      artists !== previous?.[2] ||
      album !== previous?.[3]
    ) {
      href.value = "";
    }
    failed.value = false;
    if (!ready || !title.trim()) return;

    let active = true;
    onCleanup(() => {
      active = false;
    });
    try {
      const credits = artists.filter((artist) => artist.trim());
      const payload = JSON.stringify({
        type: "track",
        title,
        ...(credits.length
          ? { artist: credits.join(", "), artists: credits }
          : {}),
        ...(album?.trim() ? { album } : {}),
        ...(coverUrl ? { art_url: coverUrl } : {}),
      });
      const bytes = new TextEncoder().encode(payload);
      if (bytes.length > 65_536) throw new Error("Share payload is too large");
      const stream = new Blob([bytes])
        .stream()
        .pipeThrough(new CompressionStream("deflate-raw"));
      const compressed = new Uint8Array(
        await new Response(stream).arrayBuffer(),
      );
      const base64url = btoa(
        Array.from(compressed, (byte) => String.fromCharCode(byte)).join(""),
      )
        .replace(/\+/g, "-")
        .replace(/\//g, "_")
        .replace(/=+$/, "");
      if (base64url.length >= 65_536)
        throw new Error("Share link is too large");
      if (active)
        href.value = `https://share.music-assistant.io/#1${base64url}`;
    } catch (error) {
      if (!active) return;
      failed.value = true;
      console.error(
        "Music Assistant share link unavailable",
        error instanceof Error ? error.name : "Unknown error",
      );
    }
  },
  { immediate: true },
);
</script>

<template>
  <div
    v-if="title.trim()"
    ref="container"
    class="song-share"
    :class="{ 'song-share-compact': compact }"
    @keydown.esc="tooltipDismissed = true"
    @mouseleave="tooltipDismissed = false"
    @focusout="tooltipDismissed = false"
  >
    <a
      v-if="href"
      :href="href"
      :title="compact ? undefined : linkLabel"
      :aria-label="`${title}: ${linkLabel}`"
      target="_blank"
      rel="noopener noreferrer"
    >
      <MusicAssistantMark />
      <template v-if="!compact">
        <span>Open in Music Assistant Share</span>
        <ExternalLink :size="14" aria-hidden="true" />
      </template>
    </a>
    <span
      v-if="href && compact && !tooltipDismissed"
      class="share-tooltip"
      aria-hidden="true"
      >{{ linkLabel }}</span
    >
    <span v-else-if="failed">Music Assistant share link unavailable.</span>
  </div>
</template>
