<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { api, errorMessage, type Snapshot } from "../api";
import HistoryCharts, { type Chart } from "./HistoryCharts.vue";
import { useToast } from "../composables/useToast";

const props = defineProps<{ snapshot: Snapshot | null }>();
defineEmits<{ navigate: [path: string] }>();
const current = ref<Snapshot | null>(null);
const charts = ref<[string, Chart | null][]>([]);
const error = ref("");
const historyLoading = ref(false);
const historyError = ref("");
const pending = ref(false);
const notify = useToast();
const queue: {
  key: string;
  label: string;
  link?: { href: string; label: string };
}[] = [
  { key: "queued", label: "Queued" },
  { key: "processing", label: "Processing" },
  {
    key: "pending_metadata",
    label: "Pending metadata",
    link: {
      href: "/admin/submissions/list?state=pending_metadata",
      label: "View pending submissions",
    },
  },
  {
    key: "manual_review",
    label: "Manual MusicBrainz review",
    link: { href: "/admin/musicbrainz-reviews", label: "Review candidates" },
  },
];
const submissions = computed(() => current.value?.overview.submissions ?? {});
const total = computed(() =>
  Object.values(submissions.value).reduce((a, b) => a + b, 0),
);
const colors: Record<string, string> = {
  processed: "#2f9e44",
  pending_metadata: "#d08800",
  failed: "#d63939",
  queued: "#4263eb",
  processing: "#ae3ec9",
};
const chartStyle = computed(() => {
  let position = 0;
  const segments = Object.entries(submissions.value)
    .filter(([, count]) => count > 0)
    .map(([state, count]) => {
      const start = position;
      position += (count / total.value) * 100;
      return `${colors[state] ?? "#868e96"} ${start}% ${position}%`;
    });
  return {
    background: segments.length
      ? `conic-gradient(${segments.join(",")})`
      : "#868e96",
  };
});
watch(
  () => props.snapshot,
  (value) => {
    if (value) current.value = value;
  },
  { immediate: true },
);
async function loadOverview() {
  try {
    const result = await api<Snapshot>("/overview");
    if (!current.value) current.value = result;
    if (result.failed) error.value = "Overview data is unavailable.";
  } catch (e) {
    error.value = errorMessage(e);
  }
}
async function loadHistory(refresh = false) {
  if (historyLoading.value) return;
  historyLoading.value = true;
  historyError.value = "";
  try {
    charts.value = (
      await api<{ charts: [string, Chart | null][] }>("/history")
    ).charts;
    if (refresh) notify("Charts updated.");
  } catch (e) {
    historyError.value = `Chart refresh failed: ${errorMessage(e)}`;
    if (refresh) notify(historyError.value, "error");
  } finally {
    historyLoading.value = false;
  }
}
async function requestReview() {
  pending.value = true;
  try {
    await api("/pending-review", {});
    notify("Pending metadata review requested.");
  } catch (e) {
    notify(errorMessage(e), "error");
  } finally {
    pending.value = false;
  }
}
onMounted(() => {
  void loadOverview();
  void loadHistory();
});
</script>

<template>
  <h1>Fingerprint administration</h1>
  <p class="muted">
    Operational data, unresolved submissions and personal API keys.
  </p>
  <p v-if="error" class="alert error" role="alert">{{ error }}</p>
  <template v-if="current?.overview">
    <div class="grid stats">
      <div class="card">
        <span class="subheader">Ingestion worker</span
        ><strong>{{
          current.overview.worker_available ? "Available" : "Unavailable"
        }}</strong>
      </div>
      <div class="card">
        <span class="subheader"
          >{{
            current.overview.database_backend === "sqlite"
              ? "SQLite"
              : "PostgreSQL"
          }}
          database</span
        ><strong
          >{{
            (current.overview.database_bytes / 1073741824).toLocaleString(
              undefined,
              { minimumFractionDigits: 2, maximumFractionDigits: 2 },
            )
          }}
          GiB</strong
        ><small>Excludes WAL</small>
      </div>
      <div class="card">
        <span class="subheader">Acoustic references</span
        ><strong
          >{{
            current.overview.references.published.toLocaleString()
          }}
          published</strong
        ><small
          >{{
            current.overview.references.stored.toLocaleString()
          }}
          stored</small
        >
      </div>
      <div class="card">
        <span class="subheader">Searchable recordings</span>
        <strong>{{
          current.overview.searchable_recordings.toLocaleString()
        }}</strong>
        <small>Verified variants counted once</small>
      </div>
      <div class="card">
        <span class="subheader">Database layout</span
        ><strong>Version {{ current.overview.format_version }}</strong
        ><small>Admin does not migrate the database</small>
      </div>
    </div>
    <h2>Ingestion queue</h2>
    <div class="grid stats">
      <div v-for="{ key, label, link } in queue" :key="key" class="card">
        <span class="subheader">{{ label }}</span>
        <strong>{{
          key === "manual_review"
            ? (current.overview.manual_review?.toLocaleString() ??
              "Unavailable")
            : (current.overview.submissions[key] ?? 0).toLocaleString()
        }}</strong
        ><a
          v-if="link"
          :href="link.href"
          @click.exact.prevent="$emit('navigate', link.href)"
          >{{ link.label }}</a
        >
      </div>
    </div>
    <p class="muted">
      Manual MusicBrainz review is a subset of pending metadata.
    </p>
    <div class="section-heading">
      <div>
        <h2>30-day history</h2>
        <p class="muted">One sample per UTC hour. Missing hours are gaps.</p>
      </div>
      <button
        class="secondary"
        :disabled="historyLoading"
        @click="loadHistory(true)"
      >
        {{ historyLoading ? "Loading charts…" : "Refresh charts" }}
      </button>
    </div>
    <p v-if="historyError" class="alert error">{{ historyError }}</p>
    <HistoryCharts :charts="charts" />
    <section class="card">
      <h2>Submission lifecycle</h2>
      <div class="lifecycle">
        <div
          class="donut"
          :style="chartStyle"
          role="img"
          :aria-label="
            total
              ? `Submission counts: ${Object.entries(submissions)
                  .map(([s, c]) => `${s}: ${c.toLocaleString()}`)
                  .join(', ')}`
              : 'No submissions yet'
          "
        >
          <div>
            <strong>{{ total.toLocaleString() }}</strong
            ><small>Total submissions</small>
          </div>
        </div>
        <p>
          All retained submissions by current status.<br />Counts and chart
          update together automatically.
        </p>
      </div>
      <div
        class="table-wrap"
        tabindex="0"
        role="region"
        aria-label="Submission counts table"
      >
        <table class="records-table" aria-label="Submission counts by state">
          <thead>
            <tr>
              <th>State</th>
              <th>Count</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(count, state) in submissions" :key="state">
              <td>
                <span :style="{ color: colors[state] ?? '#868e96' }">●</span>
                {{ state }}
              </td>
              <td>{{ count.toLocaleString() }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </template>
  <section class="card">
    <h2>Unresolved ISRC submissions</h2>
    <p>
      All retained submissions with an ISRC that have not been processed
      successfully, including waiting and failed entries. This does not certify
      an ISRC-to-recording association.
    </p>
    <a
      href="/admin/unresolved/list"
      @click.prevent="$emit('navigate', '/admin/unresolved/list')"
      >Browse unresolved ISRC submissions</a
    >
  </section>
  <section class="card">
    <h2>Request a pending metadata review</h2>
    <p>
      Ask the worker to review pending entries using its existing pacing,
      batching and cooldown. This does not delete entries, increase their
      completed-check count or retry failed entries.
    </p>
    <button class="secondary" :disabled="pending" @click="requestReview">
      Request non-destructive pending review
    </button>
    <p class="muted">
      MusicBrainz cooldown is worker-local and is not reported here.
    </p>
  </section>
</template>
