<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { api, display, errorMessage, type Row, type Snapshot } from "../api";

const props = defineProps<{ dataset: string; id?: string; snapshot: Snapshot | null }>();
const emit = defineEmits<{ navigate: [path: string] }>();
type TableResult = { items: Row[]; total: number; fields: string[]; sortable: string[]; searchable: string[] };
const labels: Record<string, string> = { submissions: "Submissions", unresolved: "Unresolved ISRC submissions", references: "References" };
const title = computed(() => labels[props.dataset] ?? props.dataset);
const result = ref<TableResult>({ items: [], total: 0, fields: [], sortable: [], searchable: [] });
const detail = ref<Row | null>(null);
const search = ref("");
const filterField = ref("");
const operator = ref("contains");
const filterValue = ref("");
const appliedFilter = ref<Record<string, unknown> | null>(null);
const skip = ref(0);
const limit = ref(50);
const order = ref<{ field: string; direction: "asc" | "desc" }[]>([]);
const selected = ref<string[]>([]);
const status = ref("Loading table…");
const failed = ref(false);
const busy = ref(false);
const loadedRevision = ref<string | undefined>();
let sequence = 0;
let timer: ReturnType<typeof setTimeout> | undefined;
const latestRevision = computed(() => props.snapshot?.revisions[props.dataset]);
const changed = computed(() => !!latestRevision.value && !!loadedRevision.value && latestRevision.value !== loadedRevision.value);
watch(latestRevision, value => { if (!loadedRevision.value && value && !busy.value) loadedRevision.value = value; });
async function load() {
  const request = ++sequence;
  const revision = latestRevision.value;
  busy.value = true;
  status.value = "Loading table… Previous rows remain until results arrive.";
  const params = new URLSearchParams({ skip: String(skip.value), limit: String(limit.value) });
  let where: unknown = search.value.trim() || null;
  if (appliedFilter.value) where = where ? { and: [where, appliedFilter.value] } : appliedFilter.value;
  if (where) params.set("where", JSON.stringify(where));
  order.value.forEach(item => params.append("order_by", `${item.field}:${item.direction}`));
  try {
    const data = await api<TableResult>(`/tables/${props.dataset}?${params}`);
    if (request !== sequence) return;
    result.value = data;
    selected.value = [];
    loadedRevision.value = revision ?? (latestRevision.value ? "__unknown__" : undefined);
    failed.value = false;
    status.value = `${data.total.toLocaleString()} ${where ? "matching " : ""}records.`;
  } catch (e) {
    if (request !== sequence) return;
    failed.value = true;
    status.value = `Table request failed: ${errorMessage(e)}. Previous rows may be outdated.`;
  } finally { if (request === sequence) busy.value = false; }
}
async function loadDetail() {
  try { detail.value = await api<Row>(`/tables/${props.dataset}/${encodeURIComponent(props.id!)}`); status.value = ""; }
  catch (e) { status.value = errorMessage(e); failed.value = true; }
}
function searchChanged() {
  if (timer) clearTimeout(timer);
  sequence++;
  selected.value = [];
  busy.value = true;
  status.value = "Preparing search…";
  timer = setTimeout(() => { skip.value = 0; void load(); }, 350);
}
function searchNow() {
  if (timer) clearTimeout(timer);
  skip.value = 0;
  void load();
}
function applyFilter() {
  if (!filterField.value) return;
  appliedFilter.value = { [filterField.value]: { [operator.value]: ["is_null", "is_not_null"].includes(operator.value) ? true : filterValue.value } };
  skip.value = 0;
  void load();
}
function clearFilter() { appliedFilter.value = null; filterField.value = ""; filterValue.value = ""; skip.value = 0; void load(); }
function sort(field: string, multi: boolean) {
  const existing = order.value.find(item => item.field === field);
  const direction = existing?.direction === "asc" ? "desc" : "asc";
  if (!multi) order.value = [];
  else order.value = order.value.filter(item => item.field !== field);
  order.value.push({ field, direction });
  skip.value = 0;
  void load();
}
function page(delta: number) { skip.value = Math.max(0, skip.value + delta); void load(); }
async function action(operation: "retry" | "delete", ids: string[]) {
  if (!ids.length || busy.value || (!props.id && failed.value)) return;
  const explanation = operation === "delete"
    ? "Delete the selected submissions? Acoustic references and landmarks are retained."
    : "Retry the selected submissions? Queued and processing jobs cannot be changed.";
  if (!window.confirm(explanation)) return;
  busy.value = true;
  try {
    await api(`/tables/${props.dataset}/actions`, { operation, ids, confirmed: true });
    if (props.id && operation === "delete") emit("navigate", `/admin/${props.dataset}/list`);
    else if (props.id) await loadDetail();
    else await load();
  } catch (e) { status.value = errorMessage(e); failed.value = true; }
  finally { busy.value = false; }
}
onMounted(() => { if (props.id) void loadDetail(); else void load(); });
onUnmounted(() => { sequence++; if (timer) clearTimeout(timer); });
</script>

<template>
  <h1>{{ title }}{{ id ? ' detail' : '' }}</h1>
  <p v-if="status" :class="failed ? 'alert error' : 'muted'" role="status">{{ status }}</p>
  <template v-if="id">
    <p><a :href="`/admin/${dataset}/list`" @click.prevent="$emit('navigate', `/admin/${dataset}/list`)">← Back to {{ title }}</a></p>
    <div v-if="detail" class="card">
      <dl class="details"><template v-for="(value, field) in detail" :key="field"><dt>{{ field }}</dt><dd><pre>{{ display(value, String(field)) }}</pre></dd></template></dl>
      <div v-if="dataset !== 'references'" class="actions"><button class="secondary" :disabled="busy" @click="action('retry', [id])">Retry submission</button><button class="danger" :disabled="busy" @click="action('delete', [id])">Delete submission</button></div>
    </div>
  </template>
  <template v-else>
    <div class="actions"><button class="secondary" :disabled="busy || (!changed && !failed)" @click="load">{{ failed ? 'Retry table refresh' : changed ? 'Newer data available — refresh' : 'Table is up to date' }}</button><span class="muted">Updates never replace your current rows automatically.</span></div>
    <p class="muted">Search covers all records and ignores case, spaces and punctuation. Advanced filters use literal matching. Click a heading to sort; Shift-click adds another sort column.</p>
    <div class="card">
      <label>Search <input v-model="search" maxlength="200" type="search" @input="searchChanged" @keydown.enter.prevent="searchNow"></label>
      <details class="filter"><summary>Advanced filter</summary><form class="actions" @submit.prevent="applyFilter">
        <label>Field <select v-model="filterField" required><option value="">Select field</option><option v-for="field in result.searchable" :key="field" :value="field">{{ field }}</option></select></label>
        <label>Operator <select v-model="operator"><option v-for="op in ['contains','not_contains','eq','neq','startswith','not_startswith','endswith','not_endswith','is_null','is_not_null']" :key="op" :value="op">{{ op }}</option></select></label>
        <label v-if="!['is_null','is_not_null'].includes(operator)">Value <input v-model="filterValue" maxlength="200"></label>
        <button>Apply filter</button><button type="button" class="secondary" @click="clearFilter">Clear filter</button>
      </form><p v-if="appliedFilter">Advanced filter active</p></details>
      <div v-if="dataset !== 'references'" class="actions"><button class="secondary" :disabled="!selected.length || busy || failed" @click="action('retry', selected)">Retry selected</button><button class="danger" :disabled="!selected.length || busy || failed" @click="action('delete', selected)">Delete selected</button><span>{{ selected.length }} selected</span></div>
      <div class="table-wrap" :aria-busy="busy"><table><thead><tr><th v-if="dataset !== 'references'"><input type="checkbox" aria-label="Select all rows" :checked="result.items.length > 0 && selected.length === result.items.length" @change="selected = ($event.target as HTMLInputElement).checked ? result.items.map(row => String(row.id)) : []"></th><th v-for="field in result.fields" :key="field"><button v-if="result.sortable.includes(field)" class="sort" @click="sort(field, $event.shiftKey)">{{ field }} {{ order.find(item => item.field === field)?.direction === 'asc' ? '↑' : order.find(item => item.field === field)?.direction === 'desc' ? '↓' : '' }}</button><template v-else>{{ field }}</template></th></tr></thead>
      <tbody><tr v-for="row in result.items" :key="String(row.id)"><td v-if="dataset !== 'references'"><input v-model="selected" type="checkbox" :value="String(row.id)" :aria-label="`Select ${row.id}`"></td><td v-for="field in result.fields" :key="field"><a v-if="field === 'id'" :href="`/admin/${dataset}/detail/${encodeURIComponent(String(row.id))}`" @click.prevent="$emit('navigate', `/admin/${dataset}/detail/${encodeURIComponent(String(row.id))}`)">{{ display(row[field], field) }}</a><template v-else>{{ display(row[field], field) }}</template></td></tr><tr v-if="!result.items.length"><td :colspan="result.fields.length + 1">No records found.</td></tr></tbody></table></div>
      <nav class="pagination" aria-label="Table pages"><button class="secondary" :disabled="skip === 0 || busy" @click="page(-limit)">Previous</button><span>{{ result.total ? skip + 1 : 0 }}–{{ Math.min(skip + limit, result.total) }} of {{ result.total }}</span><button class="secondary" :disabled="skip + limit >= result.total || busy" @click="page(limit)">Next</button><label>Rows <select v-model.number="limit" @change="skip = 0; load()"><option :value="25">25</option><option :value="50">50</option><option :value="100">100</option></select></label></nav>
    </div>
  </template>
</template>
