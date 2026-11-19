<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { api, errorMessage, type Snapshot } from "./api";
import Overview from "./components/Overview.vue";
import DataTable from "./components/DataTable.vue";
import Reviews from "./components/Reviews.vue";
import Credentials from "./components/Credentials.vue";

const route = ref(location.pathname);
const authenticated = ref(false);
const checking = ref(true);
const error = ref("");
const username = ref("");
const password = ref("");
const busy = ref(false);
const snapshot = ref<Snapshot | null>(null);
const liveStatus = ref("Connecting to live updates…");
let source: EventSource | undefined;
let watchdog: ReturnType<typeof setInterval> | undefined;
let lastSnapshot = 0;

const tableRoute = computed(() => route.value.match(/^\/admin\/(submissions|unresolved|references)\/(list|detail\/(.+))\/?$/));
const wantsLive = computed(() => authenticated.value && (route.value === "/admin/" || !!tableRoute.value));
const detailId = computed(() => {
  const encoded = tableRoute.value?.[3];
  if (!encoded) return undefined;
  try { return decodeURIComponent(encoded); }
  catch { return undefined; }
});
function navigate(path: string, replace = false) {
  if (replace) history.replaceState(null, "", path);
  else history.pushState(null, "", path);
  route.value = location.pathname;
  window.scrollTo(0, 0);
}
function stopLive() {
  source?.close();
  source = undefined;
  if (watchdog) clearInterval(watchdog);
  watchdog = undefined;
  snapshot.value = null;
}
function expired() {
  stopLive();
  authenticated.value = false;
  password.value = "";
  error.value = "Session ended. Sign in again.";
  navigate("/admin/login", true);
}
function startLive() {
  stopLive();
  liveStatus.value = "Connecting to live updates…";
  lastSnapshot = Date.now();
  source = new EventSource("/admin/events");
  source.addEventListener("snapshot", (event) => {
    try {
      const next = JSON.parse((event as MessageEvent).data) as Snapshot;
      const timestamp = Date.parse(next.updated_at);
      if (next.failed || !Number.isFinite(timestamp) || Date.now() - timestamp > 20000) {
        liveStatus.value = "Waiting for fresh data; displayed values may be outdated.";
        return;
      }
      lastSnapshot = timestamp;
      snapshot.value = next;
      liveStatus.value = `Live updates connected — checked ${new Date(timestamp).toLocaleTimeString()}`;
    } catch {
      liveStatus.value = "Invalid live update; displayed values may be outdated.";
    }
  });
  source.addEventListener("unavailable", () => { liveStatus.value = "Live data unavailable; displayed values may be outdated. Retrying…"; });
  source.addEventListener("session-expired", expired);
  source.onerror = () => {
    liveStatus.value = "Live connection interrupted; reconnecting…";
    void api<{ authenticated: boolean }>("/session").then(({ authenticated: valid }) => {
      if (!valid) expired();
    }).catch(() => {
      // The stream's connection error remains visible while the service is unreachable.
    });
  };
  watchdog = setInterval(() => {
    if (Date.now() - lastSnapshot > 20000) liveStatus.value = "No fresh live data; displayed values may be outdated.";
  }, 5000);
}
watch(wantsLive, enabled => { if (enabled) startLive(); else stopLive(); });
async function login() {
  busy.value = true;
  error.value = "";
  try {
    await api("/login", { username: username.value, password: password.value });
    password.value = "";
    authenticated.value = true;
    navigate("/admin/", true);
  } catch (e) { error.value = errorMessage(e); }
  finally { busy.value = false; }
}
async function logout() {
  busy.value = true;
  try {
    await api("/logout", {});
    stopLive();
    authenticated.value = false;
    error.value = "";
    navigate("/admin/login", true);
  } catch (e) { error.value = errorMessage(e); }
  finally { busy.value = false; }
}
function popstate() { route.value = location.pathname; }
function pagehide() { stopLive(); }
function pageshow(event: PageTransitionEvent) { if (event.persisted) location.reload(); }
onMounted(async () => {
  window.addEventListener("popstate", popstate);
  window.addEventListener("admin-session-expired", expired);
  window.addEventListener("pagehide", pagehide);
  window.addEventListener("pageshow", pageshow);
  try {
    const session = await api<{ authenticated: boolean }>("/session");
    authenticated.value = session.authenticated;
    if (!session.authenticated) navigate("/admin/login", true);
    else {
      if (route.value === "/admin/login") navigate("/admin/", true);
    }
  } catch (e) { error.value = errorMessage(e); }
  finally { checking.value = false; }
});
onUnmounted(() => {
  stopLive();
  window.removeEventListener("popstate", popstate);
  window.removeEventListener("admin-session-expired", expired);
  window.removeEventListener("pagehide", pagehide);
  window.removeEventListener("pageshow", pageshow);
});
</script>

<template>
  <header class="topbar">
    <a href="/admin/" @click.prevent="navigate('/admin/')">Fingerprint administration</a>
    <nav v-if="authenticated" aria-label="Administration">
      <a v-for="[label, path] in [['Overview','/admin/'],['Submissions','/admin/submissions/list'],['Unresolved ISRC','/admin/unresolved/list'],['References','/admin/references/list'],['MusicBrainz reviews','/admin/musicbrainz-reviews'],['Personal keys','/admin/keys'],['Applications','/admin/applications']]" :key="path" :href="path" @click.prevent="navigate(path)">{{ label }}</a>
      <button class="link-button" :disabled="busy" @click="logout">Sign out</button>
    </nav>
  </header>
  <main>
    <p v-if="checking" role="status">Checking session…</p>
    <template v-else-if="!authenticated">
      <section class="card login">
        <h1>Sign in</h1>
        <p v-if="error" class="alert error" role="alert">{{ error }}</p>
        <form @submit.prevent="login">
          <label>Username <input v-model="username" autocomplete="username" required></label>
          <label>Password <input v-model="password" type="password" autocomplete="current-password" required></label>
          <button :disabled="busy">Sign in</button>
        </form>
      </section>
    </template>
    <template v-else>
      <p v-if="wantsLive" class="live" role="status">{{ liveStatus }}</p>
      <p v-if="error" class="alert error" role="alert">{{ error }}</p>
      <Overview v-if="route === '/admin/' || route === '/admin'" :snapshot="snapshot" @navigate="navigate" />
      <DataTable v-else-if="tableRoute && (!tableRoute[3] || detailId)" :key="route" :dataset="tableRoute[1]!" :id="detailId" :snapshot="snapshot" @navigate="navigate" />
      <Reviews v-else-if="route === '/admin/musicbrainz-reviews'" />
      <Credentials v-else-if="route === '/admin/keys' || route === '/admin/applications'" :key="route" :kind="route.endsWith('/keys') ? 'keys' : 'applications'" />
      <section v-else><h1>Page not found</h1><a href="/admin/" @click.prevent="navigate('/admin/')">Return to overview</a></section>
    </template>
  </main>
</template>
