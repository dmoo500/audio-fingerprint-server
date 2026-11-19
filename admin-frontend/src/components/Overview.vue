<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { api, errorMessage, type Snapshot } from "../api";
import HistoryCharts, { type Chart } from "./HistoryCharts.vue";

const props = defineProps<{ snapshot: Snapshot | null }>();
defineEmits<{ navigate: [path: string] }>();
const current = ref<Snapshot | null>(null);
const charts = ref<[string, Chart | null][]>([]);
const error = ref("");
const historyStatus = ref("");
const message = ref("");
const pending = ref(false);
const queue = [["queued", "Queued"], ["processing", "Processing"], ["pending_metadata", "Pending metadata"], ["manual_review", "Manual MusicBrainz review"]];
const submissions = computed(() => current.value?.overview.submissions ?? {});
const total = computed(() => Object.values(submissions.value).reduce((a, b) => a + b, 0));
const colors: Record<string, string> = { processed: "#2f9e44", pending_metadata: "#d08800", failed: "#d63939", queued: "#4263eb", processing: "#ae3ec9" };
const chartStyle = computed(() => {
  let position = 0;
  const segments = Object.entries(submissions.value).filter(([, count]) => count > 0).map(([state, count]) => {
    const start = position;
    position += count / total.value * 100;
    return `${colors[state] ?? "#868e96"} ${start}% ${position}%`;
  });
  return { background: segments.length ? `conic-gradient(${segments.join(",")})` : "#868e96" };
});
watch(() => props.snapshot, value => { if (value) current.value = value; }, { immediate: true });
async function loadOverview() {
  try {
    const result = await api<Snapshot>("/overview");
    if (!current.value) current.value = result;
    if (result.failed) error.value = "Overview data is unavailable.";
  } catch (e) { error.value = errorMessage(e); }
}
async function loadHistory() {
  historyStatus.value = "Loading charts…";
  try {
    charts.value = (await api<{ charts: [string, Chart | null][] }>("/history")).charts;
    historyStatus.value = "Charts updated.";
  } catch (e) { historyStatus.value = `Chart refresh failed: ${errorMessage(e)}`; }
}
async function requestReview() {
  pending.value = true;
  message.value = "";
  try { await api("/pending-review", {}); message.value = "Pending metadata review requested."; }
  catch (e) { message.value = errorMessage(e); }
  finally { pending.value = false; }
}
onMounted(() => { void loadOverview(); void loadHistory(); });
</script>

<template>
  <h1>Fingerprint administration</h1>
  <p class="muted">Operational data, unresolved submissions and personal API keys.</p>
  <p v-if="error" class="alert error" role="alert">{{ error }}</p>
  <template v-if="current?.overview">
    <div class="grid stats">
      <div class="card"><span class="subheader">Ingestion worker</span><strong>{{ current.overview.worker_available ? "Available" : "Unavailable" }}</strong></div>
      <div class="card"><span class="subheader">{{ current.overview.database_backend === "sqlite" ? "SQLite" : "PostgreSQL" }} database</span><strong>{{ (current.overview.database_bytes / 1073741824).toFixed(2) }} GiB</strong><small>Excludes WAL</small></div>
      <div class="card"><span class="subheader">Acoustic references</span><strong>{{ current.overview.references.published }} published</strong><small>{{ current.overview.references.stored }} stored</small></div>
      <div class="card"><span class="subheader">Database layout</span><strong>Version {{ current.overview.format_version }}</strong><small>Admin does not migrate the database</small></div>
    </div>
    <h2>Ingestion queue</h2>
    <div class="grid stats">
      <div v-for="[key, label] in queue" :key="key" class="card"><span class="subheader">{{ label }}</span><strong>{{ key === 'manual_review' ? current.overview.manual_review ?? 'Unavailable' : current.overview.submissions[key!] ?? 0 }}</strong><a v-if="key === 'manual_review'" href="/admin/musicbrainz-reviews" @click.prevent="$emit('navigate', '/admin/musicbrainz-reviews')">Review candidates</a></div>
    </div>
    <p class="muted">Manual MusicBrainz review is a subset of pending metadata.</p>
    <div class="section-heading"><div><h2>30-day history</h2><p class="muted">One sample per UTC hour. Missing hours are gaps.</p></div><button class="secondary" @click="loadHistory">Refresh charts</button></div>
    <p role="status" class="muted">{{ historyStatus }}</p>
    <HistoryCharts :charts="charts" />
    <section class="card">
      <h2>Submission lifecycle</h2>
      <div class="lifecycle"><div class="donut" :style="chartStyle" role="img" :aria-label="total ? `Submission counts: ${Object.entries(submissions).map(([s,c]) => `${s}: ${c}`).join(', ')}` : 'No submissions yet'"><div><strong>{{ total.toLocaleString() }}</strong><small>Total submissions</small></div></div><p>All retained submissions by current status.<br>Counts and chart update together automatically.</p></div>
      <table><thead><tr><th>State</th><th>Count</th></tr></thead><tbody><tr v-for="(count, state) in submissions" :key="state"><td><span :style="{ color: colors[state] ?? '#868e96' }">●</span> {{ state }}</td><td>{{ count }}</td></tr></tbody></table>
    </section>
  </template>
  <section class="card"><h2>Unresolved ISRC submissions</h2><p>All retained submissions with an ISRC that have not been processed successfully, including waiting and failed entries. This does not certify an ISRC-to-recording association.</p><a href="/admin/unresolved/list" @click.prevent="$emit('navigate', '/admin/unresolved/list')">Browse unresolved ISRC submissions</a></section>
  <section class="card"><h2>Request a pending metadata review</h2><p>Ask the worker to review pending entries using its existing pacing, batching and cooldown. This does not delete entries, increase their completed-check count or retry failed entries.</p><button class="secondary" :disabled="pending" @click="requestReview">Request non-destructive pending review</button><p v-if="message" role="status">{{ message }}</p><p class="muted">MusicBrainz cooldown is worker-local and is not reported here.</p></section>
</template>
