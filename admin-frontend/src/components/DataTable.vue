<script setup lang="ts">
import {
  ChevronDown,
  Eye,
  Filter,
  RotateCw,
  Search,
  Trash2,
  X,
} from "@lucide/vue";
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { api, display, errorMessage, type Row, type Snapshot } from "../api";
import { useToast } from "../composables/useToast";

const props = defineProps<{
  dataset: string;
  id?: string;
  pendingMetadataOnly?: boolean;
  snapshot: Snapshot | null;
}>();
const emit = defineEmits<{ navigate: [path: string] }>();
const notify = useToast();
type TableResult = {
  items: Row[];
  total: number;
  fields: string[];
  sortable: string[];
  searchable: string[];
};
const labels: Record<string, string> = {
  submissions: "Submissions",
  unresolved: "Unresolved ISRC submissions",
  references: "References",
};
const columnLabels: Record<string, string> = {
  id: "ID",
  isrc: "ISRC",
  recording_mbid: "Recording MBID",
  created_at: "Created (UTC)",
  reference_id: "Reference ID",
};
function columnLabel(field: string) {
  return columnLabels[field] ?? field.replaceAll("_", " ");
}
const expandedCell = ref({ label: "", value: "" });
const cellDialog = ref<HTMLDialogElement | null>(null);

function cellPreview(value: string, field: string) {
  const identifier = ["id", "reference_id", "recording_mbid"].includes(field);
  const limit = identifier ? 16 : field === "error" ? 36 : 28;
  const chars = Array.from(value.replace(/\s+/g, " ").trim());
  if (chars.length <= limit) return chars.join("");
  return identifier
    ? `${chars.slice(0, 10).join("")}…${chars.slice(-5).join("")}`
    : `${chars.slice(0, limit - 1).join("")}…`;
}

function showCell(field: string, value: string) {
  expandedCell.value = { label: columnLabel(field), value };
  cellDialog.value?.showModal();
}

const title = computed(() => labels[props.dataset] ?? props.dataset);
const result = ref<TableResult>({
  items: [],
  total: 0,
  fields: [],
  sortable: [],
  searchable: [],
});
const detail = ref<Row | null>(null);
const hiddenFields = ref<string[]>([]);
const visibleFields = computed(() =>
  result.value.fields.filter((field) => !hiddenFields.value.includes(field)),
);
function visibleCells(row: Row) {
  return visibleFields.value.map((field) => {
    const value = display(row[field], field);
    return { field, value, preview: cellPreview(value, field) };
  });
}
const sortDirection = computed(() =>
  Object.fromEntries(
    order.value.map(({ field, direction }) => [field, direction]),
  ),
);
const search = ref("");
const searchInput = ref<HTMLInputElement | null>(null);
const filterField = ref(props.pendingMetadataOnly ? "state" : "");
const operator = ref(props.pendingMetadataOnly ? "eq" : "contains");
const filterValue = ref(props.pendingMetadataOnly ? "pending_metadata" : "");
const appliedFilter = ref<Record<string, unknown> | null>(
  props.pendingMetadataOnly ? { state: { eq: "pending_metadata" } } : null,
);
const skip = ref(0);
const limit = ref(50);
const lastOffset = computed(
  () =>
    Math.floor(Math.max(0, result.value.total - 1) / limit.value) * limit.value,
);
const displayedOffset = ref(0);
const displayedLimit = ref(limit.value);
const order = ref<{ field: string; direction: "asc" | "desc" }[]>([]);
const selected = ref<string[]>([]);
const status = ref("Loading table…");
const failed = ref(false);
const busy = ref(false);
const loadedRevision = ref<string | undefined>();
let sequence = 0;
let timer: ReturnType<typeof setTimeout> | undefined;
const latestRevision = computed(() => props.snapshot?.revisions[props.dataset]);
const changed = computed(
  () =>
    !!latestRevision.value &&
    !!loadedRevision.value &&
    latestRevision.value !== loadedRevision.value,
);
watch(latestRevision, (value) => {
  if (!loadedRevision.value && value && !busy.value)
    loadedRevision.value = value;
});
async function load() {
  const request = ++sequence;
  const revision = latestRevision.value;
  busy.value = true;
  status.value = "Loading table… Previous rows remain until results arrive.";
  const params = new URLSearchParams({
    skip: String(skip.value),
    limit: String(limit.value),
  });
  let where: unknown = search.value.trim() || null;
  if (appliedFilter.value)
    where = where ? { and: [where, appliedFilter.value] } : appliedFilter.value;
  if (where) params.set("where", JSON.stringify(where));
  order.value.forEach((item) =>
    params.append("order_by", `${item.field}:${item.direction}`),
  );
  try {
    const data = await api<TableResult>(`/tables/${props.dataset}?${params}`);
    if (request !== sequence) return;
    result.value = data;
    displayedOffset.value = Number(params.get("skip"));
    displayedLimit.value = Number(params.get("limit"));
    selected.value = [];
    loadedRevision.value =
      revision ?? (latestRevision.value ? "__unknown__" : undefined);
    failed.value = false;
    status.value = `${data.total.toLocaleString()} ${where ? "matching " : ""}records.`;
  } catch (e) {
    if (request !== sequence) return;
    failed.value = true;
    status.value = `Table request failed: ${errorMessage(e)}. Previous rows may be outdated.`;
  } finally {
    if (request === sequence) busy.value = false;
  }
}
async function loadDetail() {
  try {
    detail.value = await api<Row>(
      `/tables/${props.dataset}/${encodeURIComponent(props.id!)}`,
    );
    status.value = "";
  } catch (e) {
    status.value = errorMessage(e);
    failed.value = true;
  }
}
function searchChanged() {
  if (timer) clearTimeout(timer);
  sequence++;
  selected.value = [];
  busy.value = true;
  status.value = "Preparing search…";
  timer = setTimeout(() => {
    skip.value = 0;
    void load();
  }, 350);
}
function searchNow() {
  if (timer) clearTimeout(timer);
  skip.value = 0;
  void load();
}
function clearSearch() {
  search.value = "";
  searchInput.value?.focus();
  searchNow();
}
function applyFilter() {
  if (!filterField.value) return;
  appliedFilter.value = {
    [filterField.value]: {
      [operator.value]: ["is_null", "is_not_null"].includes(operator.value)
        ? true
        : filterValue.value,
    },
  };
  skip.value = 0;
  void load();
}
function clearFilter() {
  appliedFilter.value = null;
  filterField.value = "";
  filterValue.value = "";
  skip.value = 0;
  void load();
}
function sort(field: string, multi: boolean) {
  const existing = order.value.find((item) => item.field === field);
  const direction = existing?.direction === "asc" ? "desc" : "asc";
  if (!multi) order.value = [];
  else order.value = order.value.filter((item) => item.field !== field);
  order.value.push({ field, direction });
  skip.value = 0;
  void load();
}
function page(delta: number) {
  skip.value = Math.max(0, skip.value + delta);
  void load();
}
function detailPath(row: Row) {
  return `/admin/${props.dataset}/detail/${encodeURIComponent(String(row.id))}`;
}
function toggleColumn(field: string) {
  if (field === "id") return;
  hiddenFields.value = hiddenFields.value.includes(field)
    ? hiddenFields.value.filter((hidden) => hidden !== field)
    : [...hiddenFields.value, field];
}
async function action(operation: "retry" | "delete", ids: string[]) {
  if (!ids.length || busy.value || (!props.id && failed.value)) return;
  const explanation =
    operation === "delete"
      ? "Delete the selected submissions? Acoustic references and landmarks are retained."
      : "Retry the selected submissions? Queued and processing jobs cannot be changed.";
  if (!window.confirm(explanation)) return;
  busy.value = true;
  try {
    await api(`/tables/${props.dataset}/actions`, {
      operation,
      ids,
      confirmed: true,
    });
    notify(
      operation === "delete"
        ? "Selected submissions deleted."
        : "Selected submissions queued for retry.",
    );
    if (props.id && operation === "delete")
      emit("navigate", `/admin/${props.dataset}/list`);
    else if (props.id) await loadDetail();
    else await load();
  } catch (e) {
    status.value = errorMessage(e);
    failed.value = true;
  } finally {
    busy.value = false;
  }
}
onMounted(() => {
  if (props.id) void loadDetail();
  else void load();
});
onUnmounted(() => {
  sequence++;
  if (timer) clearTimeout(timer);
});
</script>

<template>
  <h1>{{ title }}{{ id ? " detail" : "" }}</h1>
  <p v-if="status" :class="failed ? 'alert error' : 'muted'" role="status">
    {{ status }}
  </p>
  <template v-if="id">
    <p>
      <a
        :href="`/admin/${dataset}/list`"
        @click.prevent="$emit('navigate', `/admin/${dataset}/list`)"
        >← Back to {{ title }}</a
      >
    </p>
    <div v-if="detail" class="card">
      <dl class="details">
        <template v-for="(value, field) in detail" :key="field"
          ><dt>{{ field }}</dt>
          <dd>
            <pre>{{ display(value, String(field)) }}</pre>
          </dd></template
        >
      </dl>
      <div v-if="dataset !== 'references'" class="actions">
        <button
          class="secondary"
          :disabled="busy"
          @click="action('retry', [id])"
        >
          Retry submission</button
        ><button
          class="danger"
          :disabled="busy"
          @click="action('delete', [id])"
        >
          Delete submission
        </button>
      </div>
    </div>
  </template>
  <template v-else>
    <div class="actions">
      <button
        class="secondary"
        :disabled="busy || (!changed && !failed)"
        @click="load"
      >
        {{
          failed
            ? "Retry table refresh"
            : changed
              ? "Newer data available — refresh"
              : "Table is up to date"
        }}</button
      ><span class="muted"
        >Updates never replace your current rows automatically.</span
      >
    </div>
    <p class="muted">
      Search covers all records and ignores case, spaces and punctuation.
      Advanced filters use literal matching. Click a heading to sort;
      Shift-click adds another sort column.
    </p>
    <div class="card table-card">
      <div class="table-tools">
        <details v-if="result.fields.length" class="table-disclosure">
          <summary>
            <Eye :size="16" aria-hidden="true" />
            Show/Hide columns
            <ChevronDown :size="14" aria-hidden="true" />
          </summary>
          <div class="column-options">
            <label v-for="field in result.fields" :key="field">
              <input
                type="checkbox"
                :checked="!hiddenFields.includes(field)"
                :disabled="field === 'id'"
                @change="toggleColumn(field)"
              />
              {{ columnLabel(field) }}
            </label>
          </div>
        </details>
        <details class="filter table-disclosure">
          <summary>
            <Filter :size="16" aria-hidden="true" />
            Filter{{ appliedFilter ? " (active)" : "" }}
          </summary>
          <form class="actions" @submit.prevent="applyFilter">
            <label
              >Field
              <select v-model="filterField" required>
                <option value="">Select field</option>
                <option
                  v-for="field in result.searchable"
                  :key="field"
                  :value="field"
                >
                  {{ field }}
                </option>
              </select></label
            >
            <label
              >Operator
              <select v-model="operator">
                <option
                  v-for="op in [
                    'contains',
                    'not_contains',
                    'eq',
                    'neq',
                    'startswith',
                    'not_startswith',
                    'endswith',
                    'not_endswith',
                    'is_null',
                    'is_not_null',
                  ]"
                  :key="op"
                  :value="op"
                >
                  {{ op }}
                </option>
              </select></label
            >
            <label v-if="!['is_null', 'is_not_null'].includes(operator)"
              >Value <input v-model="filterValue" maxlength="200"
            /></label>
            <button>Apply filter</button
            ><button type="button" class="secondary" @click="clearFilter">
              Clear filter
            </button>
          </form>
        </details>
      </div>
      <div class="table-controls">
        <div class="table-size">
          <select
            v-model.number="limit"
            aria-label="Rows per page"
            @change="
              skip = 0;
              load();
            "
          >
            <option :value="25">Show 25 rows</option>
            <option :value="50">Show 50 rows</option>
            <option :value="100">Show 100 rows</option>
          </select>
        </div>
        <div class="table-search">
          <Search :size="18" aria-hidden="true" />
          <input
            ref="searchInput"
            v-model="search"
            aria-label="Search"
            placeholder="Search…"
            maxlength="200"
            type="search"
            @input="searchChanged"
            @keydown.enter.prevent="searchNow"
          />
          <button
            v-if="search"
            class="clear-search"
            type="button"
            aria-label="Clear search"
            title="Clear search"
            @click="clearSearch"
          >
            <X :size="16" aria-hidden="true" />
          </button>
        </div>
      </div>
      <div
        v-if="dataset !== 'references' && selected.length"
        class="table-controls table-bulk"
      >
        <button
          class="secondary"
          :disabled="!selected.length || busy || failed"
          @click="action('retry', selected)"
        >
          Retry selected</button
        ><button
          class="danger"
          :disabled="!selected.length || busy || failed"
          @click="action('delete', selected)"
        >
          Delete selected</button
        ><span>{{ selected.length }} selected</span>
      </div>
      <div
        class="table-wrap"
        tabindex="0"
        role="region"
        :aria-label="`${title} table`"
        :aria-busy="busy"
      >
        <table class="records-table" :aria-label="title">
          <thead>
            <tr>
              <th v-if="dataset !== 'references'">
                <input
                  type="checkbox"
                  aria-label="Select all rows"
                  :checked="
                    result.items.length > 0 &&
                    selected.length === result.items.length
                  "
                  @change="
                    selected = ($event.target as HTMLInputElement).checked
                      ? result.items.map((row) => String(row.id))
                      : []
                  "
                />
              </th>
              <th
                scope="col"
                class="row-actions-heading"
                aria-label="Row actions"
              ></th>
              <th
                v-for="field in visibleFields"
                :key="field"
                scope="col"
                :aria-sort="
                  order[0]?.field === field
                    ? order[0].direction === 'asc'
                      ? 'ascending'
                      : 'descending'
                    : undefined
                "
              >
                <button
                  v-if="result.sortable.includes(field)"
                  class="sort"
                  @click="sort(field, $event.shiftKey)"
                >
                  {{ columnLabel(field) }}
                  <svg
                    class="sort-indicator"
                    :data-direction="sortDirection[field]"
                    width="10"
                    height="16"
                    viewBox="0 0 10 16"
                    aria-hidden="true"
                  >
                    <path d="M5 1 9 7H1Z" />
                    <path d="M1 9h8l-4 6Z" />
                  </svg>
                </button>
                <template v-else>{{ columnLabel(field) }}</template>
              </th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in result.items" :key="String(row.id)">
              <td v-if="dataset !== 'references'">
                <input
                  v-model="selected"
                  type="checkbox"
                  :value="String(row.id)"
                  :aria-label="`Select ${row.id}`"
                />
              </td>
              <td class="row-actions-cell">
                <div class="row-actions">
                  <a
                    :href="detailPath(row)"
                    :aria-label="`View ${row.id}`"
                    title="View details"
                    @click.exact.prevent="emit('navigate', detailPath(row))"
                  >
                    <Eye :size="16" aria-hidden="true" />
                  </a>
                  <template v-if="dataset !== 'references'">
                    <button
                      type="button"
                      :aria-label="`Retry ${row.id}`"
                      title="Retry submission"
                      :disabled="busy || failed"
                      @click="action('retry', [String(row.id)])"
                    >
                      <RotateCw :size="16" aria-hidden="true" />
                    </button>
                    <button
                      type="button"
                      :aria-label="`Delete ${row.id}`"
                      title="Delete submission"
                      :disabled="busy || failed"
                      @click="action('delete', [String(row.id)])"
                    >
                      <Trash2 :size="16" aria-hidden="true" />
                    </button>
                  </template>
                </div>
              </td>
              <td
                v-for="{ field, value, preview } in visibleCells(row)"
                :key="field"
                class="record-value"
                :class="{ 'empty-cell': row[field] == null }"
                :data-field="field"
                :title="value"
              >
                <a
                  v-if="field === 'id'"
                  :href="`/admin/${dataset}/detail/${encodeURIComponent(String(row.id))}`"
                  :aria-label="value"
                  @click.prevent="
                    $emit(
                      'navigate',
                      `/admin/${dataset}/detail/${encodeURIComponent(String(row.id))}`,
                    )
                  "
                  >{{ preview }}</a
                >
                <button
                  v-else-if="preview !== value"
                  type="button"
                  class="expand-cell"
                  aria-haspopup="dialog"
                  :aria-label="`Show full ${columnLabel(field)}`"
                  @click="showCell(field, value)"
                >
                  {{ preview }}
                </button>
                <span v-else>{{ value }}</span>
              </td>
            </tr>
            <tr v-if="!result.items.length">
              <td
                :colspan="
                  Math.max(
                    1,
                    visibleFields.length + 1 + Number(dataset !== 'references'),
                  )
                "
              >
                {{
                  busy
                    ? "Loading records…"
                    : failed
                      ? "Records could not be loaded."
                      : "No records found."
                }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="table-footer">
        <span
          >Showing {{ result.items.length ? displayedOffset + 1 : 0 }} to
          {{ result.items.length ? displayedOffset + result.items.length : 0 }}
          of {{ result.total }} entries</span
        >
        <nav class="table-pages" aria-label="Table pages">
          <button
            class="secondary"
            :disabled="skip === 0 || busy"
            @click="page(-skip)"
          >
            First
          </button>
          <button
            class="secondary"
            :disabled="skip === 0 || busy"
            @click="page(-limit)"
          >
            Previous
          </button>
          <span
            class="current-page"
            :aria-label="`Page ${Math.floor(displayedOffset / displayedLimit) + 1}`"
            aria-current="page"
            >{{ Math.floor(displayedOffset / displayedLimit) + 1 }}</span
          >
          <button
            class="secondary"
            :disabled="skip >= lastOffset || skip + limit > 1_000_000 || busy"
            @click="page(limit)"
          >
            Next
          </button>
          <button
            class="secondary"
            :disabled="skip >= lastOffset || lastOffset > 1_000_000 || busy"
            @click="page(lastOffset - skip)"
          >
            Last
          </button>
        </nav>
      </div>
    </div>
    <dialog
      ref="cellDialog"
      class="cell-dialog"
      :aria-label="`Full value: ${expandedCell.label}`"
      @keydown.esc.prevent.stop="cellDialog?.close()"
    >
      <h2>{{ expandedCell.label }}</h2>
      <p>{{ expandedCell.value }}</p>
      <button
        type="button"
        class="secondary"
        autofocus
        @click="cellDialog?.close()"
      >
        Close
      </button>
    </dialog>
  </template>
</template>
