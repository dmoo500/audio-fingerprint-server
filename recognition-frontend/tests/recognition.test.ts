// @vitest-environment happy-dom
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../src/App.vue";

const json = (body: unknown, status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => body,
});
let wrapper: VueWrapper | undefined;
let fetchMock: ReturnType<typeof vi.fn>;
let capture: ReturnType<typeof vi.fn>;
let recognitionResult: unknown;

beforeEach(() => {
  recognitionResult = {
    status: "matched",
    match: { title: "<img onerror=alert(1)>", artists: ["Example artist"], album: "Example album" },
  };
  capture = vi.fn(async ({ onStart }: { onStart: (info: { microphone: string }) => void }) => {
    onStart({ microphone: "Test microphone" });
    return { bytes: new ArrayBuffer(16), sampleRate: 48000 };
  });
  fetchMock = vi.fn(async (url: string) => {
    if (url === "/api/availability") return json({ available: true });
    if (url === "/api/statistics") return json({ recordings: 1234 });
    if (url.startsWith("/api/artwork/")) return json({ status: "missing", url: null });
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
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

async function open() {
  wrapper = mount(App);
  await flushPromises();
  return wrapper;
}

describe("recognition page", () => {
  it("shows an anonymous, accessible page with a consistent SVG icon", async () => {
    const view = await open();
    expect(view.text()).toContain("Independent project · Not affiliated with Music Assistant");
    expect(view.get(".collection-icon svg").attributes("viewBox")).toBe("0 0 24 24");
    expect(view.get("#recordings").text()).toBe((1234).toLocaleString());
    expect(view.get("#listen").attributes("disabled")).toBeUndefined();
    expect(view.get("#listen").attributes("aria-describedby")).toBe("privacy availability");
  });

  it("renders escaped metadata and sends only the transient recording", async () => {
    const view = await open();
    await view.get("#listen").trigger("click");
    await flushPromises();
    expect(view.get("#song-title").text()).toBe("<img onerror=alert(1)>");
    expect(view.find("#song-title img").exists()).toBe(false);
    expect(view.get("#song-artists").text()).toBe("Example artist");
    expect(view.get("#microphone").text()).toContain("Test microphone");
    const [url, options] = fetchMock.mock.calls.find(([path]: [string]) => path.startsWith("/api/recognize"));
    expect(url).toBe("/api/recognize?sample_rate=48000");
    expect(options.credentials).toBe("omit");
    expect(options.body.byteLength).toBe(16);
    expect(options.headers["X-Recognition-Request"]).toBe("microphone");
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
    expect(view.get("#status").text()).toContain("Microphone access was denied");
    expect(fetchMock.mock.calls.some(([url]: [string]) => url.startsWith("/api/recognize"))).toBe(false);
    expect(view.get("#listen").attributes("disabled")).toBeUndefined();
  });

  it("aborts an active recording on cancellation and page hide", async () => {
    let signal: AbortSignal | undefined;
    capture.mockImplementation(({ onStart, signal: givenSignal }) => {
      signal = givenSignal;
      onStart({ microphone: "Test microphone" });
      return new Promise((_resolve, reject) => {
        signal?.addEventListener("abort", () => reject(new DOMException("Cancelled", "AbortError")));
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
    fetchMock.mockImplementation(async (url: string) => url === "/api/availability"
      ? json({ available }) : json({ recordings: 1234 }));
    wrapper = mount(App);
    await vi.waitFor(() => expect(wrapper?.get("#availability").text()).toContain("temporarily unavailable"));
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
      if (calls === 1) return new Promise((resolve) => { finish = resolve; });
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
    fetchMock.mockImplementation(async (url: string) => url === "/api/statistics"
      ? json({ recordings: -1 }) : json({ available: true }));
    const view = await open();
    expect(view.get("#recordings").text()).toBe("—");
    expect(view.get("#count-status").text()).toContain("unavailable");
  });

  it("refreshes the count once a minute and stops while hidden", async () => {
    vi.useFakeTimers();
    let recordings = 1234;
    fetchMock.mockImplementation(async (url: string) => url === "/api/statistics"
      ? json({ recordings }) : json({ available: true }));
    wrapper = mount(App);
    await vi.waitFor(() => expect(wrapper?.get("#recordings").text()).toBe((1234).toLocaleString()));
    recordings = 1250;
    await vi.advanceTimersByTimeAsync(60000);
    expect(wrapper.get("#recordings").text()).toBe((1250).toLocaleString());
    const calls = fetchMock.mock.calls.filter(([url]: [string]) => url === "/api/statistics").length;
    vi.spyOn(document, "hidden", "get").mockReturnValue(true);
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(120000);
    expect(fetchMock.mock.calls.filter(([url]: [string]) => url === "/api/statistics")).toHaveLength(calls);
    vi.spyOn(document, "hidden", "get").mockReturnValue(false);
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.waitFor(() => expect(fetchMock.mock.calls.filter(([url]: [string]) => url === "/api/statistics")).toHaveLength(calls + 1));
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
    if (code === 503) expect(view.get("#listen").attributes("disabled")).toBeDefined();
  });

  it("loads trusted artwork without hiding the song and handles image errors", async () => {
    recognitionResult = {
      status: "matched",
      match: { title: "Song", artists: ["Artist"], album: "Album", artwork: "/api/artwork/opaque" },
    };
    fetchMock.mockImplementation(async (url: string) => {
      if (url === "/api/availability") return json({ available: true });
      if (url === "/api/statistics") return json({ recordings: 1234 });
      if (url.startsWith("/api/artwork/")) return json({ status: "ready", url: "https://archive.org/cover.jpg" });
      return json(recognitionResult);
    });
    const view = await open();
    await view.get("#listen").trigger("click");
    await flushPromises();
    expect(view.get("#song-title").text()).toBe("Song");
    const image = view.get("#song-cover");
    expect(image.attributes("src")).toBe("https://archive.org/cover.jpg");
    expect(image.attributes("referrerpolicy")).toBe("no-referrer");
    expect(image.attributes("hidden")).toBeDefined();
    await image.trigger("load");
    expect(image.attributes("hidden")).toBeUndefined();
    await image.trigger("error");
    expect(view.get("#cover-status").text()).toContain("could not be loaded");
    expect(image.attributes("hidden")).toBeDefined();
    window.dispatchEvent(new Event("pagehide"));
    await image.trigger("load");
    expect(image.attributes("hidden")).toBeDefined();
    expect(view.get("#cover-status").text()).toContain("could not be loaded");
  });

  it("polls pending artwork with the existing backoff and aborts on a new capture", async () => {
    vi.useFakeTimers();
    recognitionResult = {
      status: "matched",
      match: { title: "Song", artists: ["Artist"], album: "Album", artwork: "/api/artwork/opaque" },
    };
    fetchMock.mockImplementation(async (url: string) => {
      if (url === "/api/availability") return json({ available: true });
      if (url === "/api/statistics") return json({ recordings: 1234 });
      if (url.startsWith("/api/artwork/")) return json({ status: "pending", url: null });
      return json(recognitionResult);
    });
    wrapper = mount(App);
    await vi.waitFor(() => expect(wrapper?.get("#listen").attributes("disabled")).toBeUndefined());
    await wrapper.get("#listen").trigger("click");
    await vi.waitFor(() => expect(fetchMock.mock.calls.filter(([url]: [string]) => url.startsWith("/api/artwork/"))).toHaveLength(1));
    for (const [delay, expected] of [[1000, 2], [1000, 3], [1000, 4], [3000, 5]]) {
      await vi.advanceTimersByTimeAsync(delay);
      expect(fetchMock.mock.calls.filter(([url]: [string]) => url.startsWith("/api/artwork/"))).toHaveLength(expected);
    }
    recognitionResult = { status: "no_match", match: null };
    await wrapper.get("#listen").trigger("click");
    await vi.advanceTimersByTimeAsync(5000);
    expect(fetchMock.mock.calls.filter(([url]: [string]) => url.startsWith("/api/artwork/"))).toHaveLength(5);
    expect(wrapper.find("#result").exists()).toBe(false);
  });

  it("rejects untrusted artwork and keeps the song visible", async () => {
    recognitionResult = {
      status: "matched",
      match: { title: "Song", artists: ["Artist"], album: "Album", artwork: "/api/artwork/opaque" },
    };
    fetchMock.mockImplementation(async (url: string) => {
      if (url === "/api/availability") return json({ available: true });
      if (url === "/api/statistics") return json({ recordings: 1234 });
      if (url.startsWith("/api/artwork/")) return json({ status: "ready", url: "https://evil.example/cover.jpg" });
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
