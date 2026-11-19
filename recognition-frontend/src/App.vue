<script setup lang="ts">
import ListenCard from "./ListenCard.vue";
import { useServiceStatus } from "./useServiceStatus";

const { available, availabilityMessage, count, countMessage, setAvailable } = useServiceStatus();
</script>

<template>
  <div class="shell">
    <header>
      <a class="brand" href="/" aria-label="What's That Song? home">
        <svg viewBox="0 0 40 40" width="36" height="36" aria-hidden="true">
          <path d="M8 16v8m8-16v24m8-28v32m8-20v8" fill="none" stroke="currentColor" stroke-width="4" stroke-linecap="round"/>
        </svg>
        What's That Song?
      </a>
      <span class="header-note"><span class="dot"></span>No account. Just music.</span>
    </header>
    <main>
      <section class="hero" aria-labelledby="headline">
        <div class="intro">
          <p class="eyebrow">Song recognition for Music Assistant</p>
          <h1 id="headline">Know the song.<br><span>Keep the moment.</span></h1>
          <p class="lead">That track playing in the room? Let’s put a name to it.
            Give us ten seconds of sound, and we’ll look for a match.</p>
          <div class="collection">
            <span class="collection-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24" width="23" height="23" fill="none" stroke="currentColor"
                stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M5 19 19 5M8 5h11v11"/>
              </svg>
            </span>
            <div><strong id="recordings">{{ count }}</strong><span>searchable recordings, and growing</span></div>
          </div>
          <p id="count-status" class="fine" role="status">{{ countMessage }}</p>
        </div>
        <ListenCard :available="available" :availability-message="availabilityMessage" @unavailable="setAvailable(false)" />
      </section>
      <section class="how" aria-label="How it works">
        <div><span class="step">01</span><h2>Hear it.</h2><p>A familiar hook. A new favorite. Keep it playing.</p></div>
        <div><span class="step">02</span><h2>Tap it.</h2><p>Allow your microphone. We listen for ten seconds.</p></div>
        <div><span class="step">03</span><h2>Know it.</h2><p>If it’s in our collection, we’ll try to find it.</p></div>
      </section>
    </main>
    <footer>
      <span>Built for the curious listener.</span>
      <p>Experimental recognition. Results depend on the recording and indexed song sections.
        Collection counts group verified recording IDs; unverified references may count separately.</p>
      <span>Independent project · Not affiliated with Music Assistant or the Open Home Foundation.</span>
    </footer>
  </div>
</template>
