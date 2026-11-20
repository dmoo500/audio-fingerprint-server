<script setup lang="ts">
import { UserRound, X } from "@lucide/vue";
import { onMounted, ref, watch } from "vue";
import HistoryArtwork from "./HistoryArtwork.vue";

type Hit = {
  id: number;
  has_artwork: boolean;
  title: string;
  artists: string[];
  recognized_at: number;
};
const props = defineProps<{
  revision: number;
  historyTarget: HTMLElement | null;
}>();
const emit = defineEmits<{ accountsEnabled: [enabled: boolean] }>();
const dialog = ref<HTMLDialogElement | null>(null);
const enabled = ref(false);
const username = ref<string | null>(null);
const registering = ref(false);
const name = ref("");
const password = ref("");
const invite = ref("");
const busy = ref(false);
const error = ref("");
const notice = ref("");
const hits = ref<Hit[]>([]);
const historyLoaded = ref(false);
const confirmClear = ref(false);
let historyRequest = 0;

function resetForm() {
  registering.value = false;
  password.value = "";
  invite.value = "";
  notice.value = "";
  confirmClear.value = false;
}

async function request<T>(path: string, body?: object): Promise<T> {
  const response = await fetch(`/api/account/${path}`, {
    credentials: "same-origin",
    signal: AbortSignal.timeout(10000),
    ...(body === undefined
      ? {}
      : {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }),
  });
  if (!response.ok) {
    if (response.status === 401 && path.startsWith("history")) {
      username.value = null;
      hits.value = [];
      historyLoaded.value = false;
      throw new Error("Your session expired. Sign in again.");
    }
    const data: unknown = await response.json();
    throw new Error(
      data &&
        typeof data === "object" &&
        "detail" in data &&
        typeof data.detail === "string"
        ? data.detail
        : "The account request failed.",
    );
  }
  return (await response.json()) as T;
}

function showError(reason: unknown) {
  error.value =
    reason instanceof Error && !(reason instanceof SyntaxError)
      ? reason.message
      : "Account service is temporarily unavailable.";
}

async function loadHistory() {
  const owner = username.value;
  if (!owner) return;
  const generation = ++historyRequest;
  try {
    const result = await request<{ items: Hit[] }>("history");
    if (generation === historyRequest && username.value === owner) {
      hits.value = result.items;
      historyLoaded.value = true;
    }
  } catch (reason) {
    if (generation === historyRequest) showError(reason);
  }
}

async function update(action: "authenticate" | "logout" | "clear") {
  if (busy.value) return;
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    if (action === "logout") {
      await request("logout", {});
      historyRequest++;
      username.value = null;
      hits.value = [];
      historyLoaded.value = false;
      confirmClear.value = false;
    } else if (action === "clear") {
      await request("history/clear", {});
      confirmClear.value = false;
      await loadHistory();
    } else if (registering.value) {
      await request("register", {
        username: name.value,
        password: password.value,
        invite: invite.value,
      });
      invite.value = "";
      registering.value = false;
      notice.value = "Account created. You can now sign in.";
    } else {
      const result = await request<{ username: string }>("login", {
        username: name.value,
        password: password.value,
      });
      username.value = result.username;
      await loadHistory();
    }
  } catch (reason) {
    showError(reason);
  } finally {
    password.value = "";
    busy.value = false;
  }
}

onMounted(async () => {
  try {
    const data = await request<{ enabled: boolean; username: string | null }>(
      "session",
    );
    enabled.value = data.enabled;
    emit("accountsEnabled", data.enabled);
    username.value = data.username;
    await loadHistory();
  } catch (reason) {
    showError(reason);
  }
});
watch(() => props.revision, loadHistory);
</script>

<template>
  <button
    v-if="enabled || error"
    type="button"
    class="header-icon account-trigger"
    :aria-label="
      username ? `Account: ${username}` : 'Account and recognition history'
    "
    aria-haspopup="dialog"
    :title="
      username ? `Account: ${username}` : 'Account and recognition history'
    "
    @click="dialog?.showModal()"
  >
    <UserRound :size="18" aria-hidden="true" />
    <span v-if="username" class="account-name">{{ username }}</span>
  </button>
  <dialog
    v-if="enabled || error"
    ref="dialog"
    class="recognition-dialog recognition-account"
    aria-labelledby="account-heading"
    @close="resetForm"
  >
    <div class="account-heading">
      <h2 id="account-heading">
        {{ registering ? "Register" : "Account" }}
      </h2>
      <button
        type="button"
        class="header-icon"
        aria-label="Close account"
        autofocus
        @click="dialog?.close()"
      >
        <X :size="18" aria-hidden="true" />
      </button>
    </div>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <template v-if="enabled && username">
      <p>
        Signed in as {{ username }}. Matches are saved to your private history.
      </p>
      <button
        type="button"
        class="account-control"
        :disabled="busy"
        @click="update('logout')"
      >
        Sign out
      </button>
      <p v-if="!historyLoaded && !error">Loading history…</p>
      <p v-else-if="historyLoaded && !hits.length">No matches saved yet.</p>
      <button
        v-if="hits.length && !confirmClear"
        type="button"
        class="account-control"
        :disabled="busy"
        @click="confirmClear = true"
      >
        Clear history
      </button>
      <div v-if="confirmClear">
        <p>Delete all your saved matches?</p>
        <button
          type="button"
          class="account-control"
          :disabled="busy"
          @click="update('clear')"
        >
          Delete saved matches
        </button>
        <button
          type="button"
          class="account-control"
          :disabled="busy"
          @click="confirmClear = false"
        >
          Cancel
        </button>
      </div>
    </template>
    <template v-else-if="enabled">
      <p>
        Recognition works without an account. When signed in, successful matches
        are saved automatically. Audio is never stored.
      </p>
      <form @submit.prevent="update('authenticate')">
        <label>
          Username
          <input
            v-model="name"
            autocomplete="username"
            minlength="3"
            maxlength="32"
            pattern="[A-Za-z0-9_\-]{3,32}"
            required
          />
        </label>
        <label>
          Password
          <input
            v-model="password"
            type="password"
            :autocomplete="registering ? 'new-password' : 'current-password'"
            minlength="12"
            maxlength="128"
            required
          />
        </label>
        <label v-if="registering">
          Invitation code
          <input
            v-model="invite"
            autocomplete="off"
            spellcheck="false"
            maxlength="43"
            required
          />
        </label>
        <button type="submit" class="account-control" :disabled="busy">
          {{ registering ? "Create account" : "Sign in" }}
        </button>
      </form>
      <button
        type="button"
        class="account-control"
        :disabled="busy"
        @click="
          registering = !registering;
          error = '';
          notice = '';
          password = '';
          invite = '';
        "
      >
        {{ registering ? "Back to sign in" : "Register with an invitation" }}
      </button>
    </template>
  </dialog>
  <Teleport
    v-if="historyTarget && enabled && username && hits.length"
    :to="historyTarget"
  >
    <section class="recognition-history" aria-labelledby="history-heading">
      <h2 id="history-heading">Recognition history</h2>
      <ol tabindex="0" aria-labelledby="history-heading">
        <li v-for="hit in hits" :key="hit.id">
          <HistoryArtwork :hit-id="hit.has_artwork ? hit.id : undefined" />
          <div class="history-track">
            <strong>{{ hit.title }}</strong>
            <span v-if="hit.artists.length">{{ hit.artists.join(", ") }}</span>
          </div>
          <div class="history-date">
            <span>Recognized on</span>
            <time :datetime="new Date(hit.recognized_at * 1000).toISOString()">
              {{ new Date(hit.recognized_at * 1000).toLocaleString() }}
            </time>
          </div>
        </li>
      </ol>
    </section>
  </Teleport>
</template>
