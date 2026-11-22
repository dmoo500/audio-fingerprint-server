<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { api, errorMessage } from "../api";

type Bucket = {
  bucket: number;
  attempts: number;
  matched: number;
  no_match: number;
  ambiguous: number;
  failed: number;
};
type Statistics = {
  available: boolean;
  bucket_minutes?: number;
  buckets: Bucket[];
};

const period = ref("day");
const statistics = ref<Statistics | null>(null);
const loading = ref(false);
const error = ref("");
const totals = computed(() =>
  (statistics.value?.buckets ?? []).reduce(
    (result, bucket) => ({
      attempts: result.attempts + bucket.attempts,
      matched: result.matched + bucket.matched,
      no_match: result.no_match + bucket.no_match,
      ambiguous: result.ambiguous + bucket.ambiguous,
      failed: result.failed + bucket.failed,
    }),
    { attempts: 0, matched: 0, no_match: 0, ambiguous: 0, failed: 0 },
  ),
);
async function load() {
  loading.value = true;
  error.value = "";
  statistics.value = null;
  try {
    statistics.value = await api<Statistics>(
      `/recognition-statistics?period=${encodeURIComponent(period.value)}`,
    );
  } catch (e) {
    error.value = errorMessage(e);
  } finally {
    loading.value = false;
  }
}
function utc(minute: number) {
  return new Date(minute * 60000).toISOString().replace("T", " ").slice(0, 16);
}
onMounted(load);
</script>

<template>
  <section>
    <h2>Recognition activity</h2>
    <p class="muted">
      Requests received by the fingerprint service, including direct API and
      recognition website requests. Earlier requests rejected by the website or
      gateway are not included. No audio, IP addresses or individual events are
      stored.
    </p>
    <label for="recognition-period">Period</label>
    <select id="recognition-period" v-model="period" :disabled="loading" @change="load">
      <option value="hour">Last hour</option>
      <option value="day">Last 24 hours</option>
      <option value="month">Last 30 days</option>
      <option value="year">Last 365 days</option>
      <option value="all">All time</option>
    </select>
    <button class="secondary" :disabled="loading" @click="load">Refresh</button>
    <p v-if="loading" class="muted">Loading recognition statistics…</p>
    <p v-else-if="error" class="alert error" role="alert">{{ error }}</p>
    <p v-else-if="statistics && !statistics.available" class="muted">
      Recognition statistics are not installed yet. Restart the fingerprint
      service after updating it.
    </p>
    <template v-else-if="statistics">
      <p class="muted">
        Counting begins when this feature is installed; earlier activity is not
        available. Periods are rounded to UTC minutes, and partial buckets
        contain only activity within the selected period. Storage errors are
        logged and can leave counts incomplete. An attempt without an outcome
        may still be running or have been interrupted.
      </p>
      <div class="grid stats">
        <div v-for="(count, label) in totals" :key="label" class="card">
          <span class="subheader">{{ label.replace("_", " ") }}</span>
          <strong>{{ count.toLocaleString() }}</strong>
        </div>
      </div>
      <p class="muted">
        Intervals are {{ statistics.bucket_minutes?.toLocaleString() }} minutes;
        only intervals with activity are shown. Times are UTC.
      </p>
      <div v-if="statistics.buckets.length" class="table-wrap">
        <table class="records-table" aria-label="Recognition activity by interval">
          <thead>
            <tr>
              <th>Interval start (UTC)</th>
              <th>Attempts</th>
              <th>Matched</th>
              <th>No match</th>
              <th>Ambiguous</th>
              <th>Failed</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="bucket in statistics.buckets" :key="bucket.bucket">
              <td>{{ utc(bucket.bucket) }}</td>
              <td>{{ bucket.attempts.toLocaleString() }}</td>
              <td>{{ bucket.matched.toLocaleString() }}</td>
              <td>{{ bucket.no_match.toLocaleString() }}</td>
              <td>{{ bucket.ambiguous.toLocaleString() }}</td>
              <td>{{ bucket.failed.toLocaleString() }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-else class="muted">No recognition activity for this period.</p>
    </template>
  </section>
</template>
