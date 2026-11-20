import { onMounted, onUnmounted, ref } from "vue";

export function useServiceStatus() {
  const available = ref(false);
  const availabilityMessage = ref("Checking recognition availability…");
  const count = ref("—");
  const countMessage = ref("Checking the collection…");
  const countPending = ref(false);
  let active = true;
  let generation = 0;
  let countLoading = false;
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
    countLoading = true;
    countPending.value = true;
    const requestGeneration = generation;
    try {
      const response = await fetch("/api/statistics", {
        credentials: "omit",
        cache: "no-store",
        signal: AbortSignal.timeout(10000),
      });
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
          ? "Collection count unavailable right now."
          : "Showing the last known count. The update is temporarily unavailable.";
      console.error(
        "Collection count failed",
        error instanceof Error ? error.name : "Unknown error",
      );
    } finally {
      countLoading = false;
      countPending.value = false;
      if (active && !document.hidden) {
        if (requestGeneration !== generation) void loadCount();
        else countTimer = setTimeout(loadCount, 60000);
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
    if (document.hidden) countPending.value = false;
    setAvailable(false);
    void loadCount();
    void loadAvailability();
  }

  function hide() {
    active = false;
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
