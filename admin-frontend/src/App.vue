<script setup lang="ts">
import { LogOut, Monitor, Moon, Sun } from "@lucide/vue";
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { api, errorMessage, SessionExpired, type Snapshot } from "./api";
import AdminNavigation from "./components/AdminNavigation.vue";
import Overview from "./components/Overview.vue";
import DataTable from "./components/DataTable.vue";
import type { SubmissionNavigation } from "./submissionNavigation";
import Reviews from "./components/Reviews.vue";
import Credentials from "./components/Credentials.vue";
import RecognitionStatistics from "./components/RecognitionStatistics.vue";
import { provideToast } from "./composables/useToast";
import {
  nextColorTheme,
  readColorTheme,
  saveColorTheme,
  watchColorTheme,
} from "../../frontend-shared/colorTheme";

const themeKey = "fingerprint-admin-theme";
const theme = ref(readColorTheme(themeKey));
const nextTheme = computed(() => nextColorTheme[theme.value]);
const themeNotice = ref("");
const stopTheme = watch(
  theme,
  (_, __, cleanup) => cleanup(watchColorTheme(() => theme.value)),
  { immediate: true, flush: "sync" },
);
onUnmounted(stopTheme);
function toggleTheme() {
  theme.value = nextTheme.value;
  themeNotice.value = saveColorTheme(themeKey, theme.value)
    ? ""
    : "Theme preference could not be saved in this browser.";
}

const route = ref(location.pathname);
const routeSearch = ref(location.search);
const submissionNavigation = ref<SubmissionNavigation | null>(
  history.state?.submissionNavigation ?? null,
);
const authenticated = ref(false);
const { toast, dismiss } = provideToast(() => authenticated.value);
watch(authenticated, (signedIn) => {
  if (!signedIn) dismiss();
});
const checking = ref(true);
const error = ref("");
const username = ref("");
const password = ref("");
const busy = ref(false);
const content = ref<HTMLElement | null>(null);
const snapshot = ref<Snapshot | null>(null);
const liveStatus = ref("Connecting to live updates…");
const liveState = ref<"Connecting" | "Live" | "Delayed" | "Reconnecting">(
  "Connecting",
);
const sessionNotice = ref("");
let sessionTimer: ReturnType<typeof setInterval> | undefined;
let sessionCheck: AbortController | undefined;
let source: EventSource | undefined;
let watchdog: ReturnType<typeof setInterval> | undefined;
let lastSnapshot = 0;

const tableRoute = computed(() =>
  route.value.match(
    /^\/admin\/(submissions|unresolved|references)\/(list|detail\/(.+))\/?$/,
  ),
);
const activePath = computed(() => {
  if (tableRoute.value) return `/admin/${tableRoute.value[1]}/list`;
  return route.value === "/admin" ? "/admin/" : route.value;
});
const pendingMetadataOnly = computed(
  () =>
    tableRoute.value?.[1] === "submissions" &&
    tableRoute.value[2] === "list" &&
    new URLSearchParams(routeSearch.value).get("state") === "pending_metadata",
);
const wantsLive = computed(
  () =>
    authenticated.value && (route.value === "/admin/" || !!tableRoute.value),
);
const detailId = computed(() => {
  const encoded = tableRoute.value?.[3];
  if (!encoded) return undefined;
  try {
    return decodeURIComponent(encoded);
  } catch {
    return undefined;
  }
});
function navigate(path: string, replace = false) {
  if (replace) history.replaceState(null, "", path);
  else history.pushState(null, "", path);
  popstate();
  window.scrollTo(0, 0);
}
function browse(path: string, context: SubmissionNavigation) {
  if (
    `${route.value}${routeSearch.value}` === context.listPath &&
    path !== context.listPath
  )
    history.replaceState(
      { submissionNavigation: context },
      "",
      context.listPath,
    );
  history.pushState({ submissionNavigation: context }, "", path);
  popstate();
  window.scrollTo(0, 0);
}
async function navigateFromMenu(path: string) {
  navigate(path);
  await nextTick();
  content.value?.focus({ preventScroll: true });
}
function stopLive() {
  source?.close();
  source = undefined;
  if (watchdog) clearInterval(watchdog);
  watchdog = undefined;
  snapshot.value = null;
}
function closeLivePopover(event: KeyboardEvent) {
  const details = event.currentTarget;
  if (details instanceof HTMLDetailsElement) details.open = false;
}
function expired() {
  if (!authenticated.value) return;
  stopLive();
  authenticated.value = false;
  password.value = "";
  error.value = "Session ended. Sign in again.";
  navigate("/admin/login", true);
}
async function checkSession() {
  if (!authenticated.value || document.hidden || sessionCheck) return;
  const check = new AbortController();
  sessionCheck = check;
  try {
    const { authenticated: valid } = await api<{ authenticated: boolean }>(
      "/session",
      undefined,
      AbortSignal.any([check.signal, AbortSignal.timeout(8000)]),
    );
    if (check.signal.aborted) return;
    if (valid === false) expired();
    else if (valid === true) sessionNotice.value = "";
    else
      sessionNotice.value =
        "Could not verify the session. Invalid server response.";
  } catch (reason) {
    if (check.signal.aborted) return;
    if (reason instanceof SessionExpired) expired();
    else sessionNotice.value = "Could not verify the session. Retrying…";
  } finally {
    if (sessionCheck === check) sessionCheck = undefined;
  }
}
function stopSessionChecks() {
  clearInterval(sessionTimer);
  sessionTimer = undefined;
  sessionCheck?.abort();
  sessionCheck = undefined;
}
watch(
  authenticated,
  (signedIn) => {
    if (signedIn) sessionTimer = setInterval(checkSession, 15000);
    else {
      stopSessionChecks();
      sessionNotice.value = "";
    }
  },
  { flush: "sync" },
);
function startLive() {
  stopLive();
  liveStatus.value = "Connecting to live updates…";
  liveState.value = "Connecting";
  lastSnapshot = Date.now();
  source = new EventSource("/admin/events");
  source.addEventListener("snapshot", (event) => {
    try {
      const next = JSON.parse((event as MessageEvent).data) as Snapshot;
      const timestamp = Date.parse(next.updated_at);
      if (
        next.failed ||
        !Number.isFinite(timestamp) ||
        Date.now() - timestamp > 20000
      ) {
        liveState.value = "Delayed";
        liveStatus.value =
          "Waiting for fresh data; displayed values may be outdated.";
        return;
      }
      lastSnapshot = timestamp;
      snapshot.value = next;
      liveState.value = "Live";
      liveStatus.value = `Live updates connected — checked ${new Date(timestamp).toLocaleTimeString()}`;
    } catch {
      liveState.value = "Delayed";
      liveStatus.value =
        "Invalid live update; displayed values may be outdated.";
    }
  });
  source.addEventListener("unavailable", () => {
    liveState.value = "Delayed";
    liveStatus.value =
      "Live data unavailable; displayed values may be outdated. Retrying…";
  });
  source.addEventListener("session-expired", expired);
  source.onerror = () => {
    liveState.value = "Reconnecting";
    liveStatus.value = "Live connection interrupted; reconnecting…";
    void checkSession();
  };
  watchdog = setInterval(() => {
    if (
      Date.now() - lastSnapshot > 20000 &&
      liveState.value !== "Reconnecting"
    ) {
      liveState.value = "Delayed";
      liveStatus.value =
        "No fresh live data; displayed values may be outdated.";
    }
  }, 5000);
}
watch(wantsLive, (enabled) => {
  if (enabled) startLive();
  else stopLive();
});
async function login() {
  busy.value = true;
  error.value = "";
  try {
    await api("/login", { username: username.value, password: password.value });
    password.value = "";
    authenticated.value = true;
    navigate("/admin/", true);
  } catch (e) {
    error.value = errorMessage(e);
  } finally {
    busy.value = false;
  }
}
async function logout() {
  busy.value = true;
  try {
    await api("/logout", {});
    stopLive();
    authenticated.value = false;
    error.value = "";
    navigate("/admin/login", true);
  } catch (e) {
    error.value = errorMessage(e);
  } finally {
    busy.value = false;
  }
}
function popstate() {
  route.value = location.pathname;
  routeSearch.value = location.search;
  submissionNavigation.value = history.state?.submissionNavigation ?? null;
}
function pagehide() {
  stopLive();
}
function pageshow(event: PageTransitionEvent) {
  if (event.persisted) location.reload();
}
onMounted(async () => {
  window.addEventListener("popstate", popstate);
  window.addEventListener("admin-session-expired", expired);
  window.addEventListener("pagehide", pagehide);
  window.addEventListener("pageshow", pageshow);
  document.addEventListener("visibilitychange", checkSession);
  try {
    const session = await api<{ authenticated: boolean }>("/session");
    authenticated.value = session.authenticated;
    if (!session.authenticated) navigate("/admin/login", true);
    else {
      if (route.value === "/admin/login") navigate("/admin/", true);
    }
  } catch (e) {
    error.value = errorMessage(e);
  } finally {
    checking.value = false;
  }
});
onUnmounted(() => {
  stopLive();
  stopSessionChecks();
  document.removeEventListener("visibilitychange", checkSession);
  window.removeEventListener("popstate", popstate);
  window.removeEventListener("admin-session-expired", expired);
  window.removeEventListener("pagehide", pagehide);
  window.removeEventListener("pageshow", pageshow);
});
</script>

<template>
  <div v-if="authenticated" class="toast-region" :data-kind="toast?.kind">
    <div role="status">
      {{ toast?.kind === "success" ? toast.message : "" }}
    </div>
    <div role="alert">{{ toast?.kind === "error" ? toast.message : "" }}</div>
    <button
      v-if="toast"
      class="secondary small"
      type="button"
      aria-label="Dismiss notification"
      @click="dismiss"
    >
      ×
    </button>
  </div>
  <header class="topbar" :class="{ 'topbar-with-sidebar': authenticated }">
    <a href="/admin/" @click.prevent="navigate('/admin/')"
      >Fingerprint administration</a
    >
    <div class="topbar-actions">
      <button
        type="button"
        class="theme-toggle"
        :aria-label="`Theme: ${theme}. Switch to ${nextTheme} mode`"
        :title="`Theme: ${theme}. Switch to ${nextTheme} mode`"
        @click="toggleTheme"
      >
        <Monitor v-if="theme === 'system'" :size="18" aria-hidden="true" />
        <Sun v-else-if="theme === 'light'" :size="18" aria-hidden="true" />
        <Moon v-else :size="18" aria-hidden="true" />
      </button>
      <button
        v-if="authenticated"
        type="button"
        class="topbar-icon"
        aria-label="Sign out"
        title="Sign out"
        :disabled="busy"
        @click="logout"
      >
        <LogOut :size="22" aria-hidden="true" />
      </button>
    </div>
  </header>
  <p v-if="themeNotice" class="theme-notice" role="status">{{ themeNotice }}</p>
  <div :class="{ 'admin-layout': authenticated }">
    <AdminNavigation
      v-if="authenticated"
      :active-path="activePath"
      @navigate="navigateFromMenu"
    />
    <main ref="content" tabindex="-1">
      <p v-if="checking" role="status">Checking session…</p>
      <template v-else-if="!authenticated">
        <section class="card login">
          <h1>Sign in</h1>
          <p v-if="error" class="alert error" role="alert">{{ error }}</p>
          <form @submit.prevent="login">
            <label
              >Username
              <input v-model="username" autocomplete="username" required
            /></label>
            <label
              >Password
              <input
                v-model="password"
                type="password"
                autocomplete="current-password"
                required
            /></label>
            <button :disabled="busy">Sign in</button>
          </form>
        </section>
      </template>
      <template v-else>
        <details
          v-if="wantsLive"
          class="live-status"
          @keydown.esc="closeLivePopover"
        >
          <summary
            class="live-badge"
            :data-state="liveState"
            :title="liveStatus"
          >
            <span class="live-dot" aria-hidden="true"></span>
            <span role="status">{{ liveState }}</span>
          </summary>
          <p class="live-popover">{{ liveStatus }}</p>
        </details>
        <p v-if="sessionNotice" class="alert warning" role="status">
          {{ sessionNotice }}
        </p>
        <p v-if="error" class="alert error" role="alert">{{ error }}</p>
        <Overview
          v-if="route === '/admin/' || route === '/admin'"
          :snapshot="snapshot"
          @navigate="navigate"
        />
        <DataTable
          v-else-if="tableRoute && (!tableRoute[3] || detailId)"
          :id="detailId"
          :key="`${route}:${pendingMetadataOnly}`"
          :dataset="tableRoute[1]!"
          :pending-metadata-only="pendingMetadataOnly"
          :list-path="`${route}${routeSearch}`"
          :submission-navigation="submissionNavigation"
          :snapshot="snapshot"
          @navigate="navigate"
          @browse="browse"
        />
        <Reviews v-else-if="route === '/admin/musicbrainz-reviews'" />
        <RecognitionStatistics
          v-else-if="route === '/admin/recognition'"
          detail
        />
        <Credentials
          v-else-if="route === '/admin/keys' || route === '/admin/applications'"
          :key="route"
          :kind="route.endsWith('/keys') ? 'keys' : 'applications'"
        />
        <section v-else>
          <h1>Page not found</h1>
          <a href="/admin/" @click.prevent="navigate('/admin/')"
            >Return to overview</a
          >
        </section>
      </template>
    </main>
  </div>
</template>
