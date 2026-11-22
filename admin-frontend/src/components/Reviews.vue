<script setup lang="ts">
import { nextTick, onMounted, ref } from "vue";
import { api, errorMessage } from "../api";
import { useToast } from "../composables/useToast";

type Candidate = {
  id: string;
  title?: string;
  artists?: string | string[];
  length?: number | null;
  disambiguation?: string;
  earliest_album?: { title: string; date: string };
  albums?: string | string[];
};
type Review = {
  submission_id: string;
  title?: string;
  metadata?: { artists?: string | string[] };
  candidates: Candidate[];
};
const items = ref<Review[]>([]);
const skip = ref(0);
const requestedOffset = ref(0);
const hasMore = ref(false);
const choices = ref<Record<string, string>>({});
const busy = ref(false);
const loadError = ref("");
const fallback = ref<Record<string, boolean>>({});
const notify = useToast();
function text(value?: string | string[]) {
  return Array.isArray(value) ? value.join(", ") : value || "";
}
function track(item: Review) {
  return [item.title, text(item.metadata?.artists)].filter(Boolean).join(" - ");
}
function duration(length: number) {
  const seconds = Math.floor(length / 1000);
  const part = (n: number) => String(n).padStart(2, "0");
  return `${seconds >= 3600 ? `${part(Math.floor(seconds / 3600))}:` : ""}${part(Math.floor(seconds / 60) % 60)}:${part(seconds % 60)}`;
}
async function copy(item: Review, event: Event) {
  const container = (event.currentTarget as HTMLElement).closest(
    "[data-copy-track]",
  );
  try {
    if (!navigator.clipboard) throw new Error("Clipboard unavailable");
    await navigator.clipboard.writeText(track(item));
    fallback.value[item.submission_id] = false;
    notify("Title and artist copied.");
  } catch {
    fallback.value[item.submission_id] = true;
    await nextTick();
    const input = container?.querySelector("input");
    input?.focus();
    input?.select();
  }
}
async function load(offset = skip.value) {
  requestedOffset.value = offset;
  busy.value = true;
  loadError.value = "";
  try {
    const data = await api<{ items: Review[]; has_more: boolean }>(
      `/reviews?skip=${offset}`,
    );
    items.value = data.items;
    skip.value = offset;
    hasMore.value = data.has_more;
    choices.value = {};
  } catch (e) {
    loadError.value = errorMessage(e);
  } finally {
    busy.value = false;
  }
}
async function select(item: Review) {
  const selected = choices.value[item.submission_id];
  if (!selected || busy.value || loadError.value) return;
  busy.value = true;
  try {
    await api("/reviews", {
      submission_id: item.submission_id,
      selected_mbid: selected,
    });
    notify("Recording selected and submission queued.");
    await load();
  } catch (e) {
    notify(errorMessage(e), "error");
  } finally {
    busy.value = false;
  }
}
function page(delta: number) {
  void load(Math.max(0, skip.value + delta));
}
onMounted(() => {
  void load();
});
</script>

<template>
  <h1>MusicBrainz reviews</h1>
  <p>
    Choose the correct recording for a pending submission. Saving queues the
    submission for the worker; this page does not contact MusicBrainz.
  </p>
  <p class="muted">
    The earliest dated official album in the available results is a hint, not
    proof that the recording matches.
  </p>
  <div v-if="loadError" class="alert error" role="alert">
    {{ loadError }}
    <button :disabled="busy" @click="load(requestedOffset)">
      Retry loading reviews
    </button>
  </div>
  <p v-if="!items.length && !busy && !loadError" class="card">
    No pending MusicBrainz candidate reviews.
  </p>
  <section v-for="item in items" :key="item.submission_id" class="card">
    <div data-copy-track>
      <h2>
        {{ item.title || "Untitled submission" }}
        <button
          v-if="track(item)"
          class="secondary small"
          type="button"
          aria-label="Copy title and artist"
          @click="copy(item, $event)"
        >
          Copy
        </button>
      </h2>
      <input
        v-if="fallback[item.submission_id]"
        :value="track(item)"
        readonly
        aria-label="Title and artist to copy"
        :aria-describedby="`copy-hint-${item.submission_id}`"
      />
      <small
        v-if="fallback[item.submission_id]"
        :id="`copy-hint-${item.submission_id}`"
      >
        Could not copy automatically. Press Ctrl+C or Command+C.
      </small>
    </div>
    <p class="muted">
      Submission <code>{{ item.submission_id }}</code>
    </p>
    <form @submit.prevent="select(item)">
      <div
        v-for="candidate in item.candidates"
        :key="candidate.id"
        class="candidate"
      >
        <label
          ><input
            v-model="choices[item.submission_id]"
            type="radio"
            :name="item.submission_id"
            :value="candidate.id"
            required
          />
          <span
            ><strong>{{ candidate.title || "Untitled recording" }}</strong
            ><br />{{ text(candidate.artists) || "Unknown artist"
            }}<br />Recording MBID: <code>{{ candidate.id }}</code
            ><span v-if="candidate.length != null">
              · Length: {{ duration(candidate.length) }}</span
            ><span v-if="candidate.disambiguation">
              · {{ candidate.disambiguation }}</span
            ><span v-if="candidate.earliest_album"
              ><br />Earliest known official album:
              <strong>{{ candidate.earliest_album.title }}</strong> ({{
                candidate.earliest_album.date
              }})</span
            ></span
          >
        </label>
        <details v-if="candidate.albums?.length">
          <summary>Show album credits</summary>
          {{ text(candidate.albums) }}
        </details>
      </div>
      <p v-if="!item.candidates.length">No candidates are available.</p>
      <button v-else :disabled="busy || !!loadError">
        Select recording and queue submission
      </button>
    </form>
  </section>
  <nav v-if="skip || hasMore" class="pagination" aria-label="Review pages">
    <button class="secondary" :disabled="skip === 0 || busy" @click="page(-50)">
      Previous reviews</button
    ><button class="secondary" :disabled="!hasMore || busy" @click="page(50)">
      Next reviews
    </button>
  </nav>
</template>
