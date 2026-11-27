<script setup lang="ts">
import { Music2 } from "@lucide/vue";
import { useArtwork } from "./useArtwork";

const props = defineProps<{ artwork?: string }>();
const { cover, coverVisible, coverStatus, coverLoaded, coverFailed } =
  useArtwork(
    () =>
      props.artwork && /^\/api\/artwork\/[A-Za-z0-9_-]+$/.test(props.artwork)
        ? props.artwork
        : undefined,
    "omit",
  );
</script>

<template>
  <span class="history-thumbnail" :title="coverStatus">
    <Music2 v-if="!coverVisible" :size="21" aria-hidden="true" />
    <img
      v-if="cover"
      :src="cover"
      :hidden="!coverVisible"
      alt=""
      width="56"
      height="56"
      referrerpolicy="no-referrer"
      @load="coverLoaded"
      @error="coverFailed"
    />
  </span>
</template>
