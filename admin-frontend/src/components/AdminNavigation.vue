<script setup lang="ts">
import { Menu, X } from "@lucide/vue";
import { ref } from "vue";

defineProps<{ activePath: string }>();
const emit = defineEmits<{ navigate: [path: string] }>();
const expanded = ref(false);
const links = [
  { label: "Overview", path: "/admin/" },
  { label: "Submissions", path: "/admin/submissions/list" },
  { label: "Unresolved ISRC", path: "/admin/unresolved/list" },
  { label: "References", path: "/admin/references/list" },
  { label: "MusicBrainz reviews", path: "/admin/musicbrainz-reviews" },
  { label: "Personal keys", path: "/admin/keys" },
  { label: "Applications", path: "/admin/applications" },
];

function follow(event: MouseEvent, path: string) {
  if (
    event.button !== 0 ||
    event.metaKey ||
    event.ctrlKey ||
    event.altKey ||
    event.shiftKey
  )
    return;
  event.preventDefault();
  expanded.value = false;
  emit("navigate", path);
}
</script>

<template>
  <div class="sidebar">
    <button
      class="sidebar-toggle secondary"
      type="button"
      aria-controls="admin-navigation"
      :aria-expanded="expanded"
      :aria-label="expanded ? 'Hide menu' : 'Show menu'"
      @click="expanded = !expanded"
    >
      <X v-if="expanded" :size="24" aria-hidden="true" />
      <Menu v-else :size="24" aria-hidden="true" />
    </button>
    <nav
      id="admin-navigation"
      aria-label="Administration"
      :class="{ expanded }"
    >
      <ul>
        <li v-for="{ label, path } in links" :key="path">
          <a
            :href="path"
            :aria-current="activePath === path ? 'page' : undefined"
            @click="follow($event, path)"
            >{{ label }}</a
          >
        </li>
      </ul>
    </nav>
  </div>
</template>
