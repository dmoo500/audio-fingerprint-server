<script setup lang="ts">
import ReferenceArtwork from "./ReferenceArtwork.vue";
import {
  ChevronDown,
  Eye,
  Filter,
  RotateCw,
  Search,
  Trash2,
  X,
} from "@lucide/vue";
import {
  computed,
  onMounted,
  onUnmounted,
  ref,
  nextTick,
  shallowRef,
  toRaw,
  watch,
} from "vue";
import { api, display, errorMessage, type Row, type Snapshot } from "../api";
import { useToast } from "../composables/useToast";
import type { ListView, SubmissionNavigation } from "../submissionNavigation";

const props = defineProps<{
  dataset: string;
  id?: string;
  pendingMetadataOnly?: boolean;
  listPath?: string;
  submissionNavigation?: SubmissionNavigation | null;
  snapshot: Snapshot | null;
}>();
const emit = defineEmits<{
  navigate: [path: string];
  browse: [path: string, context: SubmissionNavigation];
}>();
const notify = useToast();
const context = computed(() =>
  props.submissionNavigation?.dataset === props.dataset
    ? props.submissionNavigation
    : undefined,
);
const restored = !props.id ? context.value?.view : undefined;
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
const hiddenFields = ref<string[]>([...(restored?.hiddenFields ?? [])]);
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
    displayedOrder.value.map(({ field, direction }) => [field, direction]),
  ),
);
const search = ref(restored?.search ?? "");
const searchInput = ref<HTMLInputElement | null>(null);
const filterField = ref(
  restored?.filterField ?? (props.pendingMetadataOnly ? "state" : ""),
);
const operator = ref(
  restored?.operator ?? (props.pendingMetadataOnly ? "eq" : "contains"),
);
const filterValue = ref(
  restored?.filterValue ??
    (props.pendingMetadataOnly ? "pending_metadata" : ""),
);
const appliedFilter = ref<Record<string, unknown> | null>(
  restored
    ? restored.appliedFilter
    : props.pendingMetadataOnly
      ? { state: { eq: "pending_metadata" } }
      : null,
);
const skip = ref(restored?.skip ?? 0);
const limit = ref(restored?.limit ?? 50);
const displayedOffset = ref(0);
const order = ref<ListView["order"]>(
  (restored?.order ?? []).map((item) => ({ ...item })),
);
const displayedOrder = computed<ListView["order"]>(() =>
  displayedView.value?.order.length
    ? displayedView.value.order
    : [{ field: "created_at", direction: "desc" }],
);
const selected = ref<string[]>([]);
const status = ref("Loading table…");
const failed = ref(false);
const moreFailed = ref(false);
const busy = ref(false);
const loadedRevision = ref<string | undefined>();
let sequence = 0;
let timer: ReturnType<typeof setTimeout> | undefined;
let displayedQuery = "";
const displayedView = shallowRef<ListView>();
const neighborRequest = new AbortController();
const navigating = ref(false);
const navigationError = ref("");
const sentinel = ref<HTMLElement | null>(null);
let observer: IntersectionObserver | undefined;
let restoring = false;
let mounted = true;
const canLoadMore = computed(
  () =>
    result.value.items.length > 0 &&
    displayedOffset.value + result.value.items.length < result.value.total &&
    displayedOffset.value + result.value.items.length <= 1_000_000,
);
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
async function load(append = false, retry = false) {
  if (
    append &&
    (busy.value ||
      (failed.value && !retry) ||
      changed.value ||
      !canLoadMore.value)
  )
    return;
  const request = ++sequence;
  const revision = latestRevision.value;
  busy.value = true;
  moreFailed.value = false;
  status.value = append
    ? "Loading more records…"
    : "Loading table… Previous rows remain until results arrive.";
  const params = new URLSearchParams({
    skip: String(
      append ? displayedOffset.value + result.value.items.length : skip.value,
    ),
    limit: String(limit.value),
  });
  let where: unknown = search.value.trim() || null;
  if (appliedFilter.value)
    where = where ? { and: [where, appliedFilter.value] } : appliedFilter.value;
  if (where) params.set("where", JSON.stringify(where));
  order.value.forEach((item) =>
    params.append("order_by", `${item.field}:${item.direction}`),
  );
  const view: ListView = {
    search: search.value,
    filterField: filterField.value,
    operator: operator.value,
    filterValue: filterValue.value,
    appliedFilter: toRaw(appliedFilter.value),
    order: order.value.map((item) => ({ ...item })),
    skip: skip.value,
    limit: limit.value,
    hiddenFields: [],
  };
  try {
    const data = await api<TableResult>(`/tables/${props.dataset}?${params}`);
    if (request !== sequence) return;
    if (append) {
      if (
        revision !== latestRevision.value ||
        data.total !== result.value.total
      ) {
        status.value = "The table changed. Refresh to load more records.";
        failed.value = true;
        return;
      }
      const existing = new Set(result.value.items.map((row) => String(row.id)));
      if (
        !data.items.length ||
        data.items.some((row) => existing.has(String(row.id)))
      ) {
        status.value = "The table changed. Refresh to load more records.";
        failed.value = true;
        return;
      }
      result.value = { ...data, items: [...result.value.items, ...data.items] };
    } else {
      result.value = data;
      displayedQuery = params.toString();
      displayedView.value = view;
      displayedOffset.value = Number(params.get("skip"));
      selected.value = [];
      loadedRevision.value =
        revision ?? (latestRevision.value ? "__unknown__" : undefined);
    }
    failed.value = false;
    status.value = `${data.total.toLocaleString()} ${where ? "matching " : ""}records.`;
  } catch (e) {
    if (request !== sequence) return;
    failed.value = true;
    moreFailed.value = append;
    status.value = `Table request failed: ${errorMessage(e)}. Previous rows may be outdated.`;
  } finally {
    if (request === sequence) {
      busy.value = false;
      await nextTick();
      if (sentinel.value && observer) {
        observer.unobserve(sentinel.value);
        observer.observe(sentinel.value);
      }
    }
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
  const entry = {
    field,
    direction: existing?.direction === "asc" ? "desc" : "asc",
  } as const;
  order.value = !multi
    ? [entry]
    : existing
      ? order.value.map((item) => (item.field === field ? entry : item))
      : [...order.value, entry];
  if (timer) clearTimeout(timer);
  skip.value = 0;
  void load();
}
function detailPath(id: string) {
  return `/admin/${props.dataset}/detail/${encodeURIComponent(id)}`;
}
function openDetail(id: string) {
  const path = detailPath(id);
  if (!displayedView.value) {
    emit("navigate", path);
    return;
  }
  const listPath = props.listPath ?? `/admin/${props.dataset}/list`;
  const current: SubmissionNavigation = {
    dataset: props.dataset,
    listPath,
    view: {
      ...displayedView.value,
      hiddenFields: [...hiddenFields.value],
      loadedCount: result.value.items.length,
      scrollY: window.scrollY,
    },
    query: displayedQuery,
    offset: displayedOffset.value,
    ids: result.value.items.map((row) => String(row.id)),
    total: result.value.total,
  };
  emit("browse", path, current);
}
const detailIndex = computed(
  () => context.value?.ids.indexOf(props.id ?? "") ?? -1,
);
const position = computed(() =>
  context.value && detailIndex.value >= 0
    ? context.value.offset + detailIndex.value
    : -1,
);
const backPath = computed(
  () => context.value?.listPath ?? `/admin/${props.dataset}/list`,
);
function backToList() {
  if (context.value) emit("browse", backPath.value, toRaw(context.value));
  else emit("navigate", backPath.value);
}
async function openNeighbor(direction: -1 | 1) {
  const current = context.value;
  const target = position.value + direction;
  if (
    !current ||
    position.value < 0 ||
    target < 0 ||
    target >= current.total ||
    navigating.value ||
    busy.value
  )
    return;
  navigationError.value = "";
  const nearby = current.ids[detailIndex.value + direction];
  if (nearby !== undefined) {
    emit("browse", detailPath(nearby), toRaw(current));
    return;
  }
  const offset = Math.min(target, position.value);
  const params = new URLSearchParams(current.query);
  params.set("skip", String(offset));
  params.set("limit", "2");
  navigating.value = true;
  try {
    const data = await api<TableResult>(
      `/tables/${props.dataset}?${params}`,
      undefined,
      neighborRequest.signal,
    );
    const ids = data.items.map((row) => String(row.id));
    const id = ids[target - offset];
    if (ids[position.value - offset] !== props.id || !id) {
      navigationError.value =
        "The list changed. Return to the list to continue.";
      return;
    }
    emit("browse", detailPath(id), {
      ...toRaw(current),
      offset,
      ids,
      total: data.total,
    });
  } catch (error) {
    if (!neighborRequest.signal.aborted)
      navigationError.value = `Could not load adjacent entry: ${errorMessage(error)}`;
  } finally {
    navigating.value = false;
  }
}
function toggleColumn(field: string) {
  if (field === "id") return;
  hiddenFields.value = hiddenFields.value.includes(field)
    ? hiddenFields.value.filter((hidden) => hidden !== field)
    : [...hiddenFields.value, field];
}
async function action(operation: "retry" | "delete", ids: string[]) {
  if (
    !ids.length ||
    ids.length > 100 ||
    busy.value ||
    (!props.id && failed.value)
  )
    return;
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
    if (props.id && operation === "delete") backToList();
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
  if (!props.id && typeof IntersectionObserver !== "undefined") {
    observer = new IntersectionObserver(
      (entries) => {
        if (!restoring && entries.some((entry) => entry.isIntersecting))
          void load(true);
      },
      { rootMargin: "200px" },
    );
    if (sentinel.value) observer.observe(sentinel.value);
  }
  if (props.id) void loadDetail();
  else
    void (async () => {
      restoring = true;
      await load();
      while (
        mounted &&
        restored?.loadedCount &&
        result.value.items.length < restored.loadedCount &&
        canLoadMore.value &&
        !busy.value &&
        !failed.value &&
        !changed.value
      ) {
        const count = result.value.items.length;
        await load(true);
        if (result.value.items.length === count) break;
      }
      if (mounted && restored?.scrollY !== undefined) {
        await nextTick();
        window.scrollTo(0, restored.scrollY);
      }
      restoring = false;
      if (mounted && sentinel.value && observer) {
        observer.unobserve(sentinel.value);
        observer.observe(sentinel.value);
      }
    })();
});
onUnmounted(() => {
  mounted = false;
  observer?.disconnect();
  neighborRequest.abort();
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
    <nav class="actions" aria-label="Detail navigation">
      <a :href="backPath" @click.exact.prevent="backToList"
        >← Back to {{ title }}</a
      >
      <template v-if="context && position >= 0">
        <button
          type="button"
          class="secondary"
          :disabled="position === 0 || busy || navigating"
          @click="openNeighbor(-1)"
        >
          Previous
        </button>
        <span>{{ position + 1 }} / {{ context.total }}</span>
        <button
          type="button"
          class="secondary"
          :disabled="
            position + 1 >= context.total ||
            busy ||
            navigating ||
            position + 1 > 1_000_000
          "
          @click="openNeighbor(1)"
        >
          Next
        </button>
      </template>
    </nav>
    <p v-if="navigationError" class="alert error" role="alert">
      {{ navigationError }}
    </p>
    <div v-if="detail" class="card">
      <ReferenceArtwork
        v-if="
          dataset === 'references' && Number(detail.publication_ready) === 1
        "
        :key="id"
        :reference="id"
      />
      <img
        v-else-if="typeof detail.artwork_url === 'string'"
        class="detail-cover"
        :src="detail.artwork_url"
        alt="Album cover"
        width="120"
        height="120"
        referrerpolicy="no-referrer"
      />
      <dl class="details">
        <template v-for="(value, field) in detail" :key="field">
          <template v-if="field !== 'artwork_url'">
            <dt>{{ field }}</dt>
            <dd>
              <pre>{{ display(value, String(field)) }}</pre>
            </dd>
          </template>
        </template>
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
        @click="load()"
      >
        {{
          failed
            ? "Retry table refresh"
            : changed
              ? "Newer data available — refresh"
              : "Table is up to date"
        }}</button><span class="muted"
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
          :disabled="
            !selected.length || selected.length > 100 || busy || failed
          "
          @click="action('retry', selected)"
        >
          Retry selected</button
        ><button
          class="danger"
          :disabled="
            !selected.length || selected.length > 100 || busy || failed
          "
          @click="action('delete', selected)"
        >
          Delete selected</button
        ><span
          >{{ selected.length }} selected<span v-if="selected.length > 100">
            — select at most 100 for an action</span
          ></span
        >
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
                  aria-label="Select all loaded rows"
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
              <th scope="col" class="artwork-cell">Artwork</th>
              <th
                v-for="field in visibleFields"
                :key="field"
                scope="col"
                :aria-sort="
                  displayedOrder[0]?.field === field
                    ? displayedOrder[0].direction === 'asc'
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
                    :href="detailPath(String(row.id))"
                    :aria-label="`View ${row.id}`"
                    title="View details"
                    @click.exact.prevent="openDetail(String(row.id))"
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
              <td class="artwork-cell">
                <img
                  v-if="typeof row.artwork_url === 'string'"
                  :key="row.artwork_url"
                  :src="row.artwork_url"
                  alt="Album cover"
                  width="28"
                  height="28"
                  loading="lazy"
                  referrerpolicy="no-referrer"
                />
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
                  :href="detailPath(String(row.id))"
                  :aria-label="value"
                  @click.exact.prevent="openDetail(String(row.id))"
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
                    visibleFields.length + 2 + Number(dataset !== 'references'),
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
        <button
          v-if="canLoadMore"
          class="secondary"
          :disabled="busy || (failed && !moreFailed) || changed"
          @click="load(true, moreFailed)"
        >
          {{ moreFailed ? "Retry loading more" : "Load more" }}
        </button>
        <span v-else-if="displayedOffset + result.items.length < result.total"
          >Use search or filters to narrow the results.</span
        >
      </div>
      <div ref="sentinel" aria-hidden="true"></div>
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
