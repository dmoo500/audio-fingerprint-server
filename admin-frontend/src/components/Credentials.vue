<script setup lang="ts">
import { onMounted, ref } from "vue";
import { api, errorMessage, type Row } from "../api";
import { useToast } from "../composables/useToast";

const props = defineProps<{ kind: "keys" | "applications" }>();
const notify = useToast();
const items = ref<Row[]>([]);
const legacy = ref(false);
const name = ref("");
const role = ref("read");
const newKey = ref("");
const error = ref("");
const busy = ref(false);
const confirmed = ref<Record<string, boolean>>({});
async function load() {
  const response = await api<{ items: Row[]; legacy_configured?: boolean }>(
    `/${props.kind}`,
  );
  items.value = response.items;
  legacy.value = !!response.legacy_configured;
}
async function issue() {
  busy.value = true;
  error.value = "";
  try {
    const body =
      props.kind === "keys"
        ? { operation: "issue", username: name.value, role: role.value }
        : { operation: "issue", name: name.value };
    const response = await api<{ new_key?: string }>(`/${props.kind}`, body);
    newKey.value = response.new_key ?? "";
    name.value = "";
    await load();
  } catch (e) {
    error.value = errorMessage(e);
  } finally {
    busy.value = false;
  }
}
async function revoke(item: Row) {
  const id = String(props.kind === "keys" ? item.username : item.id);
  if (!confirmed.value[id]) return;
  busy.value = true;
  error.value = "";
  try {
    await api(
      `/${props.kind}`,
      props.kind === "keys"
        ? { operation: "revoke", username: id, confirmed: true }
        : { operation: "revoke", id, confirmed: true },
    );
    confirmed.value[id] = false;
    notify("Key revoked.");
    await load();
  } catch (e) {
    error.value = errorMessage(e);
  } finally {
    busy.value = false;
  }
}
onMounted(async () => {
  try {
    await load();
  } catch (e) {
    error.value = errorMessage(e);
  }
});
</script>

<template>
  <h1>{{ kind === "keys" ? "Personal API keys" : "Application keys" }}</h1>
  <p v-if="kind === 'keys'">
    Existing secrets are never displayed. Revoking a key prevents subsequent
    gateway requests; already running requests may finish. Application keys are
    managed separately.
  </p>
  <p v-else>
    Each application has its own revocable key. Clients send it in
    <code>X-Application-Key</code> together with a personal API key. Application
    keys do not grant personal permissions. Existing secrets are never
    displayed; already running requests may finish.
  </p>
  <p v-if="error" class="alert error" role="alert">{{ error }}</p>
  <div v-if="legacy && kind === 'applications'" class="alert">
    <strong>Legacy environment application key: configured.</strong> This shared
    token remains valid and cannot be revoked here. Migrate clients to separate
    application keys and rotate <code>FP_APPLICATION_KEY</code> consistently on
    the backend services. Removing the variable is not currently supported.
  </div>
  <div v-if="newKey" class="alert warning" role="status">
    <h2>Save the new key now</h2>
    <p>This value is shown only once. Save it before leaving this page.</p>
    <pre class="secret">{{ newKey }}</pre>
    <button class="secondary" @click="newKey = ''">Dismiss key</button>
  </div>
  <section class="card">
    <h2>
      Create {{ kind === "keys" ? "a personal key" : "an application key" }}
    </h2>
    <form class="actions" autocomplete="off" @submit.prevent="issue">
      <label
        >{{ kind === "keys" ? "Name prefix" : "Application name" }}
        <input
          v-model="name"
          required
          :maxlength="kind === 'keys' ? 31 : 64"
          :pattern="
            kind === 'keys' ? '[A-Za-z0-9_\\-]{1,31}' : '[A-Za-z0-9_\\-]{1,64}'
          "
      /></label>
      <label v-if="kind === 'keys'"
        >Permissions
        <select v-model="role">
          <option value="read">Read / recognition</option>
          <option value="upload">Upload</option>
          <option value="both">Read and upload</option>
        </select></label
      >
      <button :disabled="busy || !!newKey">Create key</button>
    </form>
    <p v-if="kind === 'keys'" class="muted">
      A random suffix prevents replacing an existing named consumer.
    </p>
  </section>
  <div class="card table-card">
    <div
      class="table-wrap"
      tabindex="0"
      role="region"
      :aria-label="
        kind === 'keys' ? 'Personal API key table' : 'Application key table'
      "
    >
      <table
        class="records-table"
        :aria-label="kind === 'keys' ? 'Personal API keys' : 'Application keys'"
      >
        <thead>
          <tr>
            <th>Name</th>
            <th v-if="kind === 'applications'">ID</th>
            <th>{{ kind === "keys" ? "Permissions" : "Status" }}</th>
            <th class="revoke-column">Revoke</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="item in items"
            :key="String(kind === 'keys' ? item.username : item.id)"
          >
            <td class="record-value">
              {{ kind === "keys" ? item.username : item.name }}
            </td>
            <td v-if="kind === 'applications'" class="record-value">
              <code>app-{{ item.id }}</code>
            </td>
            <td>
              {{
                kind === "keys"
                  ? item.role
                  : item.revoked_at != null
                    ? "Revoked"
                    : "Active"
              }}
            </td>
            <td class="revoke-column">
              <form
                v-if="kind === 'keys' || item.revoked_at == null"
                class="revoke-actions"
                @submit.prevent="revoke(item)"
              >
                <label
                  ><input
                    v-model="
                      confirmed[
                        String(kind === 'keys' ? item.username : item.id)
                      ]
                    "
                    type="checkbox"
                    required
                  />
                  Confirm revocation</label
                ><button class="danger small" :disabled="busy">Revoke</button>
              </form>
            </td>
          </tr>
          <tr v-if="!items.length">
            <td :colspan="kind === 'keys' ? 3 : 4">No keys available.</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.revoke-column {
  width: 1%;
}

.revoke-actions {
  display: inline-flex;
  align-items: center;
  gap: 0.65rem;
}
</style>
