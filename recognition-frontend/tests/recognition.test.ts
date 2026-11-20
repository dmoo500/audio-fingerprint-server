// @vitest-environment happy-dom
import { inflateRawSync } from "node:zlib";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { h } from "vue";
import App from "../src/App.vue";
import HistoryArtwork from "../src/HistoryArtwork.vue";
import SongShareLink from "../src/SongShareLink.vue";

const json = (body: unknown, status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  headers: new Headers({ "Content-Type": "application/json" }),
  json: async () => body,
});
let wrapper: VueWrapper | undefined;
let fetchMock: ReturnType<typeof vi.fn>;
let capture: ReturnType<typeof vi.fn>;
let recognitionResult: unknown;

function sharePayload(href: string) {
  const url = new URL(href);
  expect(url.origin).toBe("https://share.music-assistant.io");
  expect(url.pathname + url.search).toBe("/");
  expect(url.hash).toMatch(/^#1[A-Za-z0-9_-]+$/);
  return JSON.parse(
    inflateRawSync(Buffer.from(url.hash.slice(2), "base64url")).toString(
      "utf8",
    ),
  );
}

beforeEach(() => {
  recognitionResult = {
    status: "matched",
    match: {
      title: "<img onerror=alert(1)>",
      artists: ["Example artist"],
      album: "Example album",
    },
  };
  capture = vi.fn(
    async ({
      onStart,
    }: {
      onStart: (info: { microphone: string }) => void;
    }) => {
      onStart({ microphone: "Test microphone" });
      return { bytes: new ArrayBuffer(16), sampleRate: 48000 };
    },
  );
  fetchMock = vi.fn(async (url: string) => {
    if (url === "/api/availability") return json({ available: true });
    if (url === "/api/statistics") return json({ recordings: 1234 });
    if (url === "/api/account/session")
      return json({ enabled: false, username: null });
    if (url.startsWith("/api/artwork/"))
      return json({ status: "missing", url: null });
    return json(recognitionResult);
  });
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("FingerprintCapture", { record: capture });
  vi.stubGlobal("AudioWorkletNode", class {});
  vi.stubGlobal("isSecureContext", true);
  vi.stubGlobal("navigator", { mediaDevices: { getUserMedia: vi.fn() } });
  vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  wrapper?.unmount();
  wrapper = undefined;
  localStorage.removeItem("fingerprint-recognition-theme");
  delete document.documentElement.dataset.theme;
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

async function open() {
  wrapper = mount(App);
  await flushPromises();
  return wrapper;
}

it("loads private artwork only when its thumbnail becomes visible", async () => {
  let intersect: (() => void) | undefined;
  vi.stubGlobal(
    "IntersectionObserver",
    class {
      constructor(callback: (entries: { isIntersecting: boolean }[]) => void) {
        intersect = () => callback([{ isIntersecting: true }]);
      }
      observe() {}
      disconnect() {}
    },
  );
  const url = "https://coverartarchive.org/fixture";
  fetchMock.mockResolvedValue(json({ status: "ready", url }));
  wrapper = mount(HistoryArtwork, {
    props: { hitId: 7 },
    slots: {
      default: ({ coverUrl }) =>
        h(SongShareLink, { title: "Test track", artists: [], coverUrl }),
    },
  });
  expect(fetchMock).not.toHaveBeenCalled();
  intersect?.();
  await flushPromises();
  expect(fetchMock).toHaveBeenCalledWith(
    "/api/account/history/7/artwork",
    expect.objectContaining({ credentials: "same-origin" }),
  );
  const image = wrapper.get("img");
  expect(image.attributes("src")).toBe(url);
  expect(image.attributes("hidden")).toBeDefined();
  await image.trigger("load");
  expect(image.attributes("hidden")).toBeUndefined();
  await vi.waitFor(() =>
    expect(
      sharePayload(wrapper!.get(".song-share a").attributes("href")),
    ).toEqual({
      type: "track",
      title: "Test track",
      art_url: url,
    }),
  );
});

describe("recognition page", () => {
  it("starts in system mode and remembers a manual choice without blocking recognition", async () => {
    const first = await open();
    const toggle = first.get(".theme-toggle");
    expect(toggle.attributes("aria-label")).toContain("Theme: system");
    expect(document.documentElement.dataset.theme).toBe(
      window.matchMedia("(prefers-color-scheme: dark)").matches
        ? "dark"
        : "light",
    );
    await toggle.trigger("click");
    expect(toggle.text()).toBe("");
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(first.get("#listen").attributes("disabled")).toBeUndefined();
    first.unmount();

    const second = await open();
    expect(second.get(".theme-toggle").attributes("aria-label")).toContain(
      "Theme: light",
    );
    expect(document.documentElement.dataset.theme).toBe("light");
    await second.get(".theme-toggle").trigger("click");
    expect(document.documentElement.dataset.theme).toBe("dark");
    await second.get(".theme-toggle").trigger("click");
    expect(second.get(".theme-toggle").attributes("aria-label")).toContain(
      "Theme: system",
    );
  });

  it("shows an anonymous, accessible page with a consistent SVG icon", async () => {
    const view = await open();
    expect(view.get(".header-note").text()).toBe(
      "Find songs without an account.",
    );
    expect(view.find(".history-info-link").exists()).toBe(false);
    expect(view.text()).toContain(
      "Independent project · Not affiliated with Music Assistant",
    );
    expect(view.get(".collection-icon svg").attributes("viewBox")).toBe(
      "0 0 24 24",
    );
    expect(view.get("#recordings").text()).toBe((1234).toLocaleString());
    expect(view.get("#listen").attributes("disabled")).toBeUndefined();
    expect(view.get("#listen").attributes("aria-describedby")).toBe(
      "privacy availability",
    );
  });

  it("renders escaped metadata and sends only the transient recording", async () => {
    const view = await open();
    await view.get("#listen").trigger("click");
    await flushPromises();
    expect(view.get("#song-title").text()).toBe("<img onerror=alert(1)>");
    expect(view.find("#song-title img").exists()).toBe(false);
    expect(view.get("#song-artists").text()).toBe("Example artist");
    expect(view.get("#microphone").text()).toContain("Test microphone");
    const [url, options] = fetchMock.mock.calls.find(([path]: [string]) =>
      path.startsWith("/api/recognize"),
    );
    expect(url).toBe("/api/recognize?sample_rate=48000");
    expect(options.credentials).toBe("same-origin");
    expect(options.body.byteLength).toBe(16);
    expect(options.headers["X-Recognition-Request"]).toBe("microphone");
    const share = await vi.waitFor(() => view.get("#result .song-share a"));
    expect(share.attributes("target")).toBe("_blank");
    expect(share.attributes("rel")).toBe("noopener noreferrer");
    expect(share.attributes("title")).toBe(
      "Open in Music Assistant Share (new tab)",
    );
    expect(share.attributes("aria-label")).toBe(
      "<img onerror=alert(1)>: Open in Music Assistant Share (new tab)",
    );
    expect(share.text()).toBe("Open in Music Assistant Share");
    expect(share.get(".ma-mark").attributes("aria-hidden")).toBe("true");
    expect(sharePayload(share.attributes("href"))).toEqual({
      type: "track",
      title: "<img onerror=alert(1)>",
      artist: "Example artist",
      artists: ["Example artist"],
      album: "Example album",
    });
  });

  it("explains optional accounts without opening registration", async () => {
    const original = fetchMock.getMockImplementation();
    fetchMock.mockImplementation(async (url: string, options?: RequestInit) =>
      url === "/api/account/session"
        ? json({ enabled: true, username: null })
        : original?.(url, options),
    );
    const view = await open();
    const info = view.get("dialog.history-info");
    const account = view.get("dialog.recognition-account");
    expect(info.attributes("open")).toBeUndefined();
    expect(view.get("#listen").attributes("disabled")).toBeUndefined();
    await view.get(".history-info-link").trigger("click");
    expect(info.attributes("open")).toBeDefined();
    expect(info.text()).toContain("only need an account to save");
    expect(info.text()).toContain("Register with an invitation");
    expect(account.attributes("open")).toBeUndefined();
    await info
      .get('button[aria-label="Close history information"]')
      .trigger("click");
    expect(info.attributes("open")).toBeUndefined();
  });

  it("starts recognition anonymously and refreshes private history after a signed-in match", async () => {
    vi.stubGlobal("IntersectionObserver", undefined);
    const original = fetchMock.getMockImplementation();
    let items: object[] = [];
    let recognizedAt = 1699999999;
    fetchMock.mockImplementation(async (url: string, options?: RequestInit) => {
      if (url === "/api/account/session")
        return json({ enabled: true, username: null });
      if (url === "/api/account/login") return json({ username: "listener" });
      if (url === "/api/account/history") return json({ items });
      if (url === "/api/account/history/clear") {
        items = [];
        return json({});
      }
      if (url === "/api/account/logout") return json({});
      if (url.startsWith("/api/recognize"))
        items = [
          {
            id: 1,
            has_artwork: false,
            title: "Test track",
            artists: ["Beyoncé"],
            recognized_at: ++recognizedAt,
          },
        ];
      return original?.(url, options);
    });
    const view = await open();
    expect(view.get("#listen").attributes("disabled")).toBeUndefined();
    const account = view.get("header .account-trigger");
    const panel = view.get("dialog.recognition-account");
    const history = "main > .hero + .history-slot > .recognition-history";
    expect(account.find(".account-name").exists()).toBe(false);
    expect(view.find(history).exists()).toBe(false);
    expect(panel.attributes("open")).toBeUndefined();
    await account.trigger("click");
    expect(panel.attributes("open")).toBeDefined();
    expect(panel.find('input[autocomplete="off"]').exists()).toBe(false);
    await panel.get("button.account-control[type='button']").trigger("click");
    expect(panel.get("h2").text()).toBe("Register");
    expect(
      panel.get('input[autocomplete="off"]').attributes("required"),
    ).toBeDefined();
    await panel.get("button.account-control[type='button']").trigger("click");
    await view.get('input[autocomplete="username"]').setValue("listener");
    await view.get('input[type="password"]').setValue("example passphrase");
    await view.get(".recognition-account form").trigger("submit");
    await flushPromises();
    expect(view.get(".recognition-account").text()).toContain(
      "No matches saved yet.",
    );
    expect(account.text()).toBe("listener");
    expect(view.find(history).exists()).toBe(false);
    await panel.get('button[aria-label="Close account"]').trigger("click");
    expect(panel.attributes("open")).toBeUndefined();
    await view.get("#listen").trigger("click");
    await flushPromises();
    expect(view.get(`${history} li`).text()).toContain("Test track");
    const share = await vi.waitFor(() => view.get(`${history} .song-share a`));
    expect(view.get(`${history} li`).element.lastElementChild).toBe(
      share.element.parentElement,
    );
    expect(share.attributes("target")).toBe("_blank");
    expect(share.text()).toBe("");
    expect(share.find(".ma-mark").exists()).toBe(true);
    expect(share.attributes("aria-label")).toBe(
      "Test track: Open in Music Assistant Share (new tab)",
    );
    expect(sharePayload(share.attributes("href"))).toEqual({
      type: "track",
      title: "Test track",
      artist: "Beyoncé",
      artists: ["Beyoncé"],
    });
    expect(view.get(`${history} .history-date`).text()).toContain(
      "Recognized on",
    );
    expect(view.get(`${history} .history-thumbnail`).find("img").exists()).toBe(
      false,
    );
    expect(view.get(`${history} time`).attributes("datetime")).toBe(
      new Date(1700000000000).toISOString(),
    );
    await view.get("#listen").trigger("click");
    await flushPromises();
    expect(view.findAll(`${history} li`)).toHaveLength(1);
    expect(view.get(`${history} time`).attributes("datetime")).toBe(
      new Date(1700000001000).toISOString(),
    );
    expect(fetchMock).toHaveBeenLastCalledWith(
      "/api/account/history",
      expect.objectContaining({ cache: "no-store" }),
    );
    await account.trigger("click");
    expect(panel.find("ol").exists()).toBe(false);
    await panel.findAll("button.account-control")[1]!.trigger("click");
    await panel.findAll("button.account-control")[1]!.trigger("click");
    await flushPromises();
    expect(view.find(history).exists()).toBe(false);
    await panel.get('button[aria-label="Close account"]').trigger("click");
    await view.get("#listen").trigger("click");
    await flushPromises();
    expect(view.find(history).exists()).toBe(true);
    await account.trigger("click");
    await panel.get("button.account-control").trigger("click");
    await flushPromises();
    expect(view.find(history).exists()).toBe(false);
    expect(account.find(".account-name").exists()).toBe(false);
  });

  it.each([
    ["no_match", "Not in tune this time."],
    ["ambiguous", "A few possibilities. No sure answer."],
    ["invalid", "We couldn’t catch that."],
  ])("handles %s without inventing a match", async (status, message) => {
    recognitionResult = { status, match: null };
    const view = await open();
    await view.get("#listen").trigger("click");
    await flushPromises();
    expect(view.get("#status-title").text()).toBe(message);
    expect(view.find("#result").exists()).toBe(false);
  });

  it("shows microphone denial without sending audio", async () => {
    capture.mockRejectedValue(new DOMException("Denied", "NotAllowedError"));
    const view = await open();
    await view.get("#listen").trigger("click");
    await flushPromises();
    expect(view.get("#status").text()).toContain(
      "Microphone access was denied",
    );
    expect(
      fetchMock.mock.calls.some(([url]: [string]) =>
        url.startsWith("/api/recognize"),
      ),
    ).toBe(false);
    expect(view.get("#listen").attributes("disabled")).toBeUndefined();
  });

  it("aborts an active recording on cancellation and page hide", async () => {
    let signal: AbortSignal | undefined;
    capture.mockImplementation(({ onStart, signal: givenSignal }) => {
      signal = givenSignal;
      onStart({ microphone: "Test microphone" });
      return new Promise((_resolve, reject) => {
        signal?.addEventListener("abort", () =>
          reject(new DOMException("Cancelled", "AbortError")),
        );
      });
    });
    const view = await open();
    await view.get("#listen").trigger("click");
    await view.get("#cancel").trigger("click");
    await flushPromises();
    expect(signal?.aborted).toBe(true);
    expect(view.get("#status-title").text()).toBe("Listening cancelled.");
    await view.get("#listen").trigger("click");
    window.dispatchEvent(new Event("pagehide"));
    await flushPromises();
    expect(signal?.aborted).toBe(true);
  });

  it("disables recognition on outage and recovers after the next check", async () => {
    vi.useFakeTimers();
    let available = false;
    fetchMock.mockImplementation(async (url: string) =>
      url === "/api/availability"
        ? json({ available })
        : json({ recordings: 1234 }),
    );
    wrapper = mount(App);
    await vi.waitFor(() =>
      expect(wrapper?.get("#availability").text()).toContain(
        "temporarily unavailable",
      ),
    );
    expect(wrapper.get("#listen").attributes("disabled")).toBeDefined();
    available = true;
    await vi.advanceTimersByTimeAsync(10000);
    expect(wrapper.get("#listen").attributes("disabled")).toBeUndefined();
    expect(wrapper.get("#availability").attributes("hidden")).toBeDefined();
  });

  it("does not reuse an availability response from before a visibility change", async () => {
    let finish: ((value: ReturnType<typeof json>) => void) | undefined;
    let calls = 0;
    fetchMock.mockImplementation(async (url: string) => {
      if (url !== "/api/availability") return json({ recordings: 1234 });
      calls++;
      if (calls === 1)
        return new Promise((resolve) => {
          finish = resolve;
        });
      return json({ available: false });
    });
    wrapper = mount(App);
    document.dispatchEvent(new Event("visibilitychange"));
    finish?.(json({ available: true }));
    await flushPromises();
    expect(calls).toBe(2);
    expect(wrapper.get("#listen").attributes("disabled")).toBeDefined();
  });

  it("reports invalid counts instead of showing zero", async () => {
    fetchMock.mockImplementation(async (url: string) =>
      url === "/api/statistics"
        ? json({ recordings: -1 })
        : json({ available: true }),
    );
    const view = await open();
    expect(view.get("#recordings").text()).toBe("—");
    expect(view.get("#count-status").text()).toContain("unavailable");
  });

  it("shows a decorative loader and refreshes after an obsolete request finishes", async () => {
    let finishInitialCount: (() => void) | undefined;
    let countRequests = 0;
    fetchMock.mockImplementation(async (url: string) => {
      if (url !== "/api/statistics") return json({ available: true });
      if (++countRequests > 1) return json({ recordings: 1250 });
      await new Promise<void>((resolve) => {
        finishInitialCount = resolve;
      });
      return json({ recordings: 1234 });
    });
    wrapper = mount(App);
    await flushPromises();
    expect(wrapper.get("#recordings").attributes("aria-busy")).toBe("true");
    expect(wrapper.get(".count-loading").attributes("aria-hidden")).toBe(
      "true",
    );
    vi.spyOn(document, "hidden", "get").mockReturnValue(true);
    document.dispatchEvent(new Event("visibilitychange"));
    await flushPromises();
    expect(wrapper.find(".count-loading").exists()).toBe(false);
    vi.spyOn(document, "hidden", "get").mockReturnValue(false);
    document.dispatchEvent(new Event("visibilitychange"));
    expect(countRequests).toBe(1);
    finishInitialCount?.();
    await flushPromises();
    expect(countRequests).toBe(2);
    expect(wrapper.get("#recordings").text()).toBe((1250).toLocaleString());
    expect(wrapper.find(".count-loading").exists()).toBe(false);
  });

  it("refreshes the count once a minute and stops while hidden", async () => {
    vi.useFakeTimers();
    let recordings = 1234;
    fetchMock.mockImplementation(async (url: string) =>
      url === "/api/statistics"
        ? json({ recordings })
        : json({ available: true }),
    );
    wrapper = mount(App);
    await vi.waitFor(() =>
      expect(wrapper?.get("#recordings").text()).toBe((1234).toLocaleString()),
    );
    recordings = 1250;
    await vi.advanceTimersByTimeAsync(60000);
    expect(wrapper.get("#recordings").text()).toBe((1250).toLocaleString());
    const calls = fetchMock.mock.calls.filter(
      ([url]: [string]) => url === "/api/statistics",
    ).length;
    vi.spyOn(document, "hidden", "get").mockReturnValue(true);
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(120000);
    expect(
      fetchMock.mock.calls.filter(
        ([url]: [string]) => url === "/api/statistics",
      ),
    ).toHaveLength(calls);
    vi.spyOn(document, "hidden", "get").mockReturnValue(false);
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.waitFor(() =>
      expect(
        fetchMock.mock.calls.filter(
          ([url]: [string]) => url === "/api/statistics",
        ),
      ).toHaveLength(calls + 1),
    );
  });

  it("keeps the last valid count through a failed refresh and replaces it on recovery", async () => {
    vi.useFakeTimers();
    let recordings = 1234;
    fetchMock.mockImplementation(async (url: string) =>
      url === "/api/statistics"
        ? json({ recordings }, recordings < 0 ? 503 : 200)
        : json({ available: true }),
    );
    wrapper = mount(App);
    await vi.waitFor(() =>
      expect(wrapper?.get("#recordings").text()).toBe((1234).toLocaleString()),
    );
    recordings = -1;
    await vi.advanceTimersByTimeAsync(60000);
    expect(wrapper.get("#recordings").text()).toBe((1234).toLocaleString());
    expect(wrapper.get("#count-status").text()).toContain("last known count");
    recordings = 1250;
    await vi.advanceTimersByTimeAsync(60000);
    expect(wrapper.get("#recordings").text()).toBe((1250).toLocaleString());
    expect(wrapper.get("#count-status").text()).not.toContain(
      "last known count",
    );
  });

  it.each([429, 503])("shows a recognition error for HTTP %s", async (code) => {
    fetchMock.mockImplementation(async (url: string) => {
      if (url === "/api/availability") return json({ available: true });
      if (url === "/api/statistics") return json({ recordings: 1234 });
      return json({ detail: "Recognition is busy. Please try again." }, code);
    });
    const view = await open();
    await view.get("#listen").trigger("click");
    await flushPromises();
    expect(view.get("#status").text()).toContain("Recognition is busy");
    expect(view.find("#result").exists()).toBe(false);
    if (code === 503)
      expect(view.get("#listen").attributes("disabled")).toBeDefined();
  });

  it("loads trusted artwork without hiding the song and handles image errors", async () => {
    recognitionResult = {
      status: "matched",
      match: {
        title: "Song",
        artists: ["Artist"],
        album: "Album",
        artwork: "/api/artwork/opaque",
      },
    };
    fetchMock.mockImplementation(async (url: string) => {
      if (url === "/api/availability") return json({ available: true });
      if (url === "/api/statistics") return json({ recordings: 1234 });
      if (url.startsWith("/api/artwork/"))
        return json({ status: "ready", url: "https://archive.org/cover.jpg" });
      return json(recognitionResult);
    });
    const view = await open();
    await view.get("#listen").trigger("click");
    await flushPromises();
    expect(view.get("#song-title").text()).toBe("Song");
    expect(view.get("#result").attributes("aria-labelledby")).toBe(
      "song-title",
    );
    const image = view.get("#song-cover");
    expect(image.attributes("src")).toBe("https://archive.org/cover.jpg");
    expect(image.attributes("referrerpolicy")).toBe("no-referrer");
    expect(image.attributes("hidden")).toBeDefined();
    const share = () =>
      sharePayload(view.get("#result .song-share a").attributes("href"));
    await vi.waitFor(() => expect(share()).not.toHaveProperty("art_url"));
    await image.trigger("load");
    expect(image.attributes("hidden")).toBeUndefined();
    await vi.waitFor(() =>
      expect(share()).toEqual({
        type: "track",
        title: "Song",
        artist: "Artist",
        artists: ["Artist"],
        album: "Album",
        art_url: "https://archive.org/cover.jpg",
      }),
    );
    await image.trigger("error");
    expect(view.get("#cover-status").text()).toContain("could not be loaded");
    expect(image.attributes("hidden")).toBeDefined();
    await vi.waitFor(() => expect(share()).not.toHaveProperty("art_url"));
    window.dispatchEvent(new Event("pagehide"));
    await image.trigger("load");
    expect(image.attributes("hidden")).toBeDefined();
    expect(view.get("#cover-status").text()).toContain("could not be loaded");
  });

  it("polls pending artwork with the existing backoff and aborts on a new capture", async () => {
    vi.useFakeTimers();
    recognitionResult = {
      status: "matched",
      match: {
        title: "Song",
        artists: ["Artist"],
        album: "Album",
        artwork: "/api/artwork/opaque",
      },
    };
    fetchMock.mockImplementation(async (url: string) => {
      if (url === "/api/availability") return json({ available: true });
      if (url === "/api/statistics") return json({ recordings: 1234 });
      if (url.startsWith("/api/artwork/"))
        return json({ status: "pending", url: null });
      return json(recognitionResult);
    });
    wrapper = mount(App);
    await vi.waitFor(() =>
      expect(wrapper?.get("#listen").attributes("disabled")).toBeUndefined(),
    );
    await wrapper.get("#listen").trigger("click");
    await vi.waitFor(() =>
      expect(
        fetchMock.mock.calls.filter(([url]: [string]) =>
          url.startsWith("/api/artwork/"),
        ),
      ).toHaveLength(1),
    );
    for (const [delay, expected] of [
      [1000, 2],
      [1000, 3],
      [1000, 4],
      [3000, 5],
    ]) {
      await vi.advanceTimersByTimeAsync(delay);
      expect(
        fetchMock.mock.calls.filter(([url]: [string]) =>
          url.startsWith("/api/artwork/"),
        ),
      ).toHaveLength(expected);
    }
    recognitionResult = { status: "no_match", match: null };
    await wrapper.get("#listen").trigger("click");
    await vi.advanceTimersByTimeAsync(5000);
    expect(
      fetchMock.mock.calls.filter(([url]: [string]) =>
        url.startsWith("/api/artwork/"),
      ),
    ).toHaveLength(5);
    expect(wrapper.find("#result").exists()).toBe(false);
  });

  it("rejects untrusted artwork and keeps the song visible", async () => {
    recognitionResult = {
      status: "matched",
      match: {
        title: "Song",
        artists: ["Artist"],
        album: "Album",
        artwork: "/api/artwork/opaque",
      },
    };
    fetchMock.mockImplementation(async (url: string) => {
      if (url === "/api/availability") return json({ available: true });
      if (url === "/api/statistics") return json({ recordings: 1234 });
      if (url.startsWith("/api/artwork/"))
        return json({ status: "ready", url: "https://evil.example/cover.jpg" });
      return json(recognitionResult);
    });
    const view = await open();
    await view.get("#listen").trigger("click");
    await flushPromises();
    expect(view.get("#song-title").text()).toBe("Song");
    expect(view.find("#song-cover").exists()).toBe(false);
    expect(view.get("#cover-status").text()).toContain("Invalid");
  });
});
