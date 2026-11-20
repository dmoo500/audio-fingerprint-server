<script setup lang="ts">
import { Monitor, Moon, Sun, X } from "@lucide/vue";
import { computed, onUnmounted, ref, watch } from "vue";
import ListenCard from "./ListenCard.vue";
import RecognitionAccount from "./RecognitionAccount.vue";
import { useServiceStatus } from "./useServiceStatus";
import {
  nextColorTheme,
  readColorTheme,
  saveColorTheme,
  watchColorTheme,
} from "../../frontend-shared/colorTheme";

const themeKey = "fingerprint-recognition-theme";
const theme = ref(readColorTheme(themeKey));
const nextTheme = computed(() => nextColorTheme[theme.value]);
const themeNotice = ref("");
const historyRevision = ref(0);
const historyTarget = ref<HTMLDivElement | null>(null);
const accountsEnabled = ref(false);
const historyInfo = ref<HTMLDialogElement | null>(null);
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

const {
  available,
  availabilityMessage,
  count,
  countMessage,
  countPending,
  setAvailable,
} = useServiceStatus();
</script>

<template>
  <div class="shell">
    <header>
      <a class="brand" href="/" aria-label="What's That Song? home">
        <svg viewBox="0 0 40 40" width="36" height="36" aria-hidden="true">
          <path
            d="M8 16v8m8-16v24m8-28v32m8-20v8"
            fill="none"
            stroke="currentColor"
            stroke-width="4"
            stroke-linecap="round"
          />
        </svg>
        What's That Song?
      </a>
      <p class="header-note">
        <span class="dot" aria-hidden="true"></span>
        <span>Find songs without an account.</span>
        <button
          v-if="accountsEnabled"
          type="button"
          class="history-info-link"
          aria-haspopup="dialog"
          @click="historyInfo?.showModal()"
        >
          Save your finds with an account
        </button>
      </p>
      <div class="header-actions">
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
        <RecognitionAccount
          :revision="historyRevision"
          :history-target="historyTarget"
          @accounts-enabled="accountsEnabled = $event"
        />
      </div>
    </header>
    <dialog
      v-if="accountsEnabled"
      ref="historyInfo"
      class="recognition-dialog history-info"
      aria-labelledby="history-info-heading"
    >
      <div class="account-heading">
        <h2 id="history-info-heading">Keep the songs you find</h2>
        <button
          type="button"
          class="header-icon"
          aria-label="Close history information"
          autofocus
          @click="historyInfo?.close()"
        >
          <X :size="18" aria-hidden="true" />
        </button>
      </div>
      <p>
        You only need an account to save your personal recognition history. When
        you start a search while signed in, successful matches are saved
        automatically so you can find them again later.
      </p>
      <p>
        Your history is private and keeps up to 500 matches. You can clear it
        anytime from the account menu. Audio recordings are never saved.
      </p>
      <p>
        Have an invitation? Open the account icon and choose
        <strong>Register with an invitation</strong>.
      </p>
    </dialog>
    <p v-if="themeNotice" class="fine" role="status">{{ themeNotice }}</p>
    <main>
      <section class="hero" aria-labelledby="headline">
        <div class="intro">
          <p class="eyebrow">Song recognition for Music Assistant</p>
          <h1 id="headline">
            Know the song.<br /><span>Keep the moment.</span>
          </h1>
          <p class="lead">
            That track playing in the room? Let’s put a name to it. Give us ten
            seconds of sound, and we’ll look for a match.
          </p>
          <div class="collection">
            <span class="collection-icon" aria-hidden="true">
              <svg
                viewBox="0 0 24 24"
                width="23"
                height="23"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
              >
                <path d="M5 19 19 5M8 5h11v11" />
              </svg>
            </span>
            <div>
              <strong id="recordings" :aria-busy="countPending">
                <template v-if="countPending && count === '—'">
                  <span class="count-loading" aria-hidden="true">
                    <span v-for="offset in 10" :key="offset">
                      <span v-for="digit in 10" :key="digit">{{
                        (offset * 3 + digit - 1) % 10
                      }}</span>
                    </span>
                  </span>
                  <span class="count-loading-static" aria-hidden="true"
                    >Loading…</span
                  >
                </template>
                <template v-else>{{ count }}</template> </strong
              ><span>searchable recordings, and growing</span>
            </div>
          </div>
          <p id="count-status" class="fine" role="status">
            {{ countMessage }}
          </p>
        </div>
        <ListenCard
          :available="available"
          :availability-message="availabilityMessage"
          @unavailable="setAvailable(false)"
          @matched="historyRevision++"
        />
      </section>
      <div ref="historyTarget" class="history-slot"></div>
      <section class="how" aria-label="How it works">
        <div>
          <span class="step">01</span>
          <h2>Hear it.</h2>
          <p>A familiar hook. A new favorite. Keep it playing.</p>
        </div>
        <div>
          <span class="step">02</span>
          <h2>Tap it.</h2>
          <p>Allow your microphone. We listen for ten seconds.</p>
        </div>
        <div>
          <span class="step">03</span>
          <h2>Know it.</h2>
          <p>If it’s in our collection, we’ll try to find it.</p>
        </div>
      </section>
    </main>
    <footer>
      <span>Built for the curious listener.</span>
      <a
        href="https://github.com/dmoo500/audio-fingerprint-server/blob/main/docs/operations/recognition.md"
      >
        Recognition guide on GitHub
      </a>
      <p>
        Experimental recognition. Results depend on the recording and indexed
        song sections. Collection counts group verified recording IDs;
        unverified references may count separately.
      </p>
      <span
        >Independent project · Not affiliated with Music Assistant or the Open
        Home Foundation.</span
      >
    </footer>
  </div>
</template>
