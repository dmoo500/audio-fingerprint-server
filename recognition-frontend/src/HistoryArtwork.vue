<script setup lang="ts">
import { Music2 } from "@lucide/vue";
import { onMounted, onUnmounted, ref } from "vue";
import { useArtwork } from "./useArtwork";

const props = defineProps<{ hitId?: number }>();
const thumbnail = ref<HTMLElement | null>(null);
const requested = ref(false);
let observer: IntersectionObserver | undefined;
const {
  cover,
  publicCover,
  coverVisible,
  coverStatus,
  coverLoaded,
  coverFailed,
} = useArtwork(
  () =>
    requested.value && props.hitId !== undefined
      ? `/api/account/history/${props.hitId}/artwork`
      : undefined,
  "same-origin",
);

onMounted(() => {
  if (
    props.hitId === undefined ||
    !thumbnail.value ||
    typeof IntersectionObserver === "undefined"
  )
    return;
  observer = new IntersectionObserver(([entry]) => {
    if (!entry?.isIntersecting) return;
    requested.value = true;
    observer?.disconnect();
  });
  observer.observe(thumbnail.value);
});
onUnmounted(() => observer?.disconnect());
</script>

<template>
  <span ref="thumbnail" class="history-thumbnail" :title="coverStatus">
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
  <slot :cover-url="coverVisible ? publicCover : undefined"></slot>
</template>
