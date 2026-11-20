import { onMounted, onUnmounted, ref, watch } from "vue";

export function useArtwork(
  path: () => string | undefined,
  credentials: RequestCredentials,
) {
  const cover = ref("");
  const coverVisible = ref(false);
  const coverStatus = ref("");
  let controller: AbortController | undefined;

  watch(
    path,
    (url, _, onCleanup) => {
      const request = new AbortController();
      controller = request;
      onCleanup(() => request.abort());
      cover.value = "";
      coverVisible.value = false;
      coverStatus.value = "";
      if (url) void loadCover(url, request.signal);
    },
    { immediate: true },
  );

  async function loadCover(path: string, signal: AbortSignal) {
    coverStatus.value = "Looking for album artwork…";
    try {
      for (let attempt = 0; attempt < 20; attempt++) {
        const response = await fetch(path, {
          credentials,
          signal: AbortSignal.any([signal, AbortSignal.timeout(10000)]),
        });
        const data: unknown = await response.json();
        if (
          !response.ok ||
          !data ||
          typeof data !== "object" ||
          !("status" in data)
        ) {
          throw new Error("Album artwork is temporarily unavailable.");
        }
        signal.throwIfAborted();
        if (
          data.status === "ready" &&
          "url" in data &&
          typeof data.url === "string"
        ) {
          const url = new URL(data.url);
          if (
            url.protocol !== "https:" ||
            url.username ||
            url.password ||
            !(
              url.hostname === "archive.org" ||
              url.hostname.endsWith(".archive.org") ||
              url.hostname === "coverartarchive.org"
            )
          )
            throw new Error("Invalid album artwork address.");
          cover.value = url.href;
          return;
        }
        if (data.status === "missing") {
          coverStatus.value = "No matching album artwork available.";
          return;
        }
        if (data.status !== "pending")
          throw new Error("Album artwork is temporarily unavailable.");
        await new Promise<void>((resolve, reject) => {
          const abort = () => {
            clearTimeout(timer);
            reject(signal.reason);
          };
          const timer = setTimeout(
            () => {
              signal.removeEventListener("abort", abort);
              resolve();
            },
            attempt < 3 ? 1000 : 3000,
          );
          signal.addEventListener("abort", abort, { once: true });
          if (signal.aborted) abort();
        });
      }
      throw new Error(
        "Album artwork is still being prepared. Try again later.",
      );
    } catch (error) {
      if (signal.aborted) return;
      coverStatus.value =
        error instanceof Error
          ? error.message
          : "Album artwork is temporarily unavailable.";
      console.error(
        "Album artwork failed",
        error instanceof Error ? error.name : "Unknown error",
      );
    }
  }

  function stopArtwork() {
    controller?.abort();
  }
  function coverLoaded() {
    if (controller?.signal.aborted) return;
    coverVisible.value = true;
    coverStatus.value = "Cover art via Cover Art Archive.";
  }
  function coverFailed() {
    if (controller?.signal.aborted) return;
    coverVisible.value = false;
    coverStatus.value = "Album artwork could not be loaded.";
  }
  onMounted(() => window.addEventListener("pagehide", stopArtwork));
  onUnmounted(() => window.removeEventListener("pagehide", stopArtwork));
  return { cover, coverVisible, coverStatus, coverLoaded, coverFailed };
}
