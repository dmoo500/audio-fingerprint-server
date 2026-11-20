import { onMounted, onUnmounted, ref } from "vue";

export function useServiceStatus() {
  const available = ref(false);
  const availabilityMessage = ref("Checking recognition availability…");
  const count = ref("—");
  const countMessage = ref("Checking the collection…");
  const countPending = ref(true);
  let active = true;
  let generation = 0;
  let countLoading = false;
  let countRetries = 0;
  let countRetryAt = 0;
  let availabilityLoading = false;
  let countTimer: ReturnType<typeof setTimeout> | undefined;
  let availabilityTimer: ReturnType<typeof setTimeout> | undefined;

  function setAvailable(value: boolean) {
    available.value = value;
    availabilityMessage.value = value
      ? ""
      : "Recognition is temporarily unavailable. We may be doing maintenance; please try again shortly.";
  }

  async function loadCount() {
    clearTimeout(countTimer);
    if (!active || document.hidden || countLoading) return;
    if (Date.now() < countRetryAt) {
      countPending.value = true;
      countTimer = setTimeout(loadCount, countRetryAt - Date.now());
      return;
    }
    countLoading = true;
    countPending.value = true;
    countMessage.value = "Checking the collection…";
    const requestGeneration = generation;
    let retryDelay: number | undefined;
    try {
      const response = await fetch("/api/statistics", {
        credentials: "omit",
        cache: "no-store",
        signal: AbortSignal.timeout(10000),
      });
      if (requestGeneration !== generation) return;
      const retryAfter = Number(response.headers.get("Retry-After"));
      if (
        (response.status === 429 || response.status === 503) &&
        Number.isInteger(retryAfter) &&
        retryAfter >= 1 &&
        retryAfter <= (response.status === 429 ? 60 : 5) &&
        countRetries < 5
      ) {
        countRetries++;
        retryDelay = retryAfter * 1000;
        countRetryAt = Date.now() + retryDelay;
        countMessage.value =
          response.status === 429
            ? "Too many requests. Waiting before checking the collection again…"
            : "The collection count is refreshing. Trying again shortly…";
        return;
      }
      const data: unknown = await response.json();
      if (requestGeneration !== generation) return;
      if (!response.ok || !isCount(data))
        throw new Error("Invalid collection count.");
      count.value = data.recordings.toLocaleString();
      countMessage.value =
        "Only searchable recordings are counted. Not every song or passage is covered.";
    } catch (error) {
      if (requestGeneration !== generation) return;
      countMessage.value =
        count.value === "—"
          ? "Checking the collection…"
          : "Showing the last known count. The update is temporarily unavailable.";
      console.error(
        "Collection count failed",
        error instanceof Error ? error.name : "Unknown error",
      );
    } finally {
      countLoading = false;
      if (retryDelay === undefined) {
        countRetries = 0;
        countPending.value = count.value === "—" && active && !document.hidden;
      }
      if (active && !document.hidden) {
        if (requestGeneration !== generation) void loadCount();
        else countTimer = setTimeout(loadCount, retryDelay ?? 60000);
      }
    }
  }

  async function loadAvailability() {
    clearTimeout(availabilityTimer);
    if (!active || document.hidden || availabilityLoading) return;
    availabilityLoading = true;
    const requestGeneration = generation;
    try {
      const response = await fetch("/api/availability", {
        credentials: "omit",
        signal: AbortSignal.timeout(10000),
      });
      const data: unknown = await response.json();
      if (!response.ok || !isAvailability(data))
        throw new Error("Invalid recognition availability response.");
      if (requestGeneration === generation) setAvailable(data.available);
    } catch (error) {
      if (requestGeneration === generation) setAvailable(false);
      console.error(
        "Recognition availability check failed",
        error instanceof Error ? error.name : "Unknown error",
      );
    } finally {
      availabilityLoading = false;
      if (active && !document.hidden) {
        if (requestGeneration !== generation) void loadAvailability();
        else availabilityTimer = setTimeout(loadAvailability, 10000);
      }
    }
  }

  function refresh() {
    generation++;
    countRetries = 0;
    if (document.hidden) countPending.value = false;
    setAvailable(false);
    void loadCount();
    void loadAvailability();
  }

  function hide() {
    active = false;
    countRetries = 0;
    countPending.value = false;
    generation++;
    setAvailable(false);
    clearTimeout(countTimer);
    clearTimeout(availabilityTimer);
  }

  function show() {
    active = true;
    refresh();
  }

  onMounted(() => {
    window.addEventListener("pagehide", hide);
    window.addEventListener("pageshow", show);
    document.addEventListener("visibilitychange", refresh);
    void loadCount();
    void loadAvailability();
  });
  onUnmounted(() => {
    hide();
    window.removeEventListener("pagehide", hide);
    window.removeEventListener("pageshow", show);
    document.removeEventListener("visibilitychange", refresh);
  });

  return {
    available,
    availabilityMessage,
    count,
    countMessage,
    countPending,
    setAvailable,
  };
}

function isCount(value: unknown): value is { recordings: number } {
  if (typeof value !== "object" || value === null || !("recordings" in value))
    return false;
  return (
    typeof value.recordings === "number" &&
    Number.isSafeInteger(value.recordings) &&
    value.recordings >= 0
  );
}

function isAvailability(value: unknown): value is { available: boolean } {
  return (
    typeof value === "object" &&
    value !== null &&
    "available" in value &&
    typeof value.available === "boolean"
  );
}
