<script setup lang="ts">
import {
  AudioLines,
  AudioWaveform,
  Boxes,
  CircleHelp,
  ClipboardCheck,
  KeyRound,
  LayoutDashboard,
  ListMusic,
  Menu,
  X,
} from "@lucide/vue";
import { ref } from "vue";

defineProps<{ activePath: string }>();
const emit = defineEmits<{ navigate: [path: string] }>();
const expanded = ref(false);
const links = [
  { label: "Overview", path: "/admin/", icon: LayoutDashboard },
  {
    label: "Recognition activity",
    path: "/admin/recognition",
    icon: AudioWaveform,
  },
  { label: "Submissions", path: "/admin/submissions/list", icon: ListMusic },
  {
    label: "Unresolved ISRC",
    path: "/admin/unresolved/list",
    icon: CircleHelp,
  },
  { label: "References", path: "/admin/references/list", icon: AudioLines },
  {
    label: "MusicBrainz reviews",
    path: "/admin/musicbrainz-reviews",
    icon: ClipboardCheck,
  },
  { label: "Personal keys", path: "/admin/keys", icon: KeyRound },
  { label: "Applications", path: "/admin/applications", icon: Boxes },
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
    <a class="sidebar-brand" href="/admin/" @click="follow($event, '/admin/')">
      Fingerprint administration
    </a>
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
        <li v-for="{ label, path, icon } in links" :key="path">
          <a
            :href="path"
            :aria-current="activePath === path ? 'page' : undefined"
            @click="follow($event, path)"
            ><component :is="icon" :size="18" aria-hidden="true" />
            <span>{{ label }}</span></a
          >
        </li>
      </ul>
    </nav>
  </div>
</template>
