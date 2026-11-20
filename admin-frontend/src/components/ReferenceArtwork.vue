<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue";
import { api } from "../api";

const props = defineProps<{ reference: string }>();
const cover = ref("");
const status = ref("Looking for album artwork…");
const controller = new AbortController();
let timer: ReturnType<typeof setTimeout> | undefined;

async function load(attempt = 0) {
  try {
    const result = await api<{ status: string; url: string | null }>(
      `/references/${encodeURIComponent(props.reference)}/artwork`,
      undefined,
      AbortSignal.any([controller.signal, AbortSignal.timeout(10000)]),
    );
    if (controller.signal.aborted) return;
    if (result.status === "ready" && result.url) {
      cover.value = result.url;
    } else if (result.status === "missing") {
      status.value = "No matching album artwork available.";
    } else if (result.status === "pending") {
      if (attempt < 15) {
        timer = setTimeout(() => void load(attempt + 1), 2000);
      } else {
        status.value = "Album artwork is still being prepared.";
      }
    } else {
      status.value = "Album artwork is temporarily unavailable.";
    }
  } catch {
    if (!controller.signal.aborted)
      status.value = "Album artwork is temporarily unavailable.";
  }
}

onMounted(() => void load());
onUnmounted(() => {
  controller.abort();
  clearTimeout(timer);
});
</script>

<template>
  <section aria-label="Album artwork">
    <img
      v-if="cover"
      :src="cover"
      alt="Album cover"
      width="120"
      height="120"
      referrerpolicy="no-referrer"
      @load="status = 'Cover art via Cover Art Archive.'"
      @error="
        cover = '';
        status = 'Album artwork could not be loaded.';
      "
    />
    <p role="status">{{ status }}</p>
  </section>
</template>
