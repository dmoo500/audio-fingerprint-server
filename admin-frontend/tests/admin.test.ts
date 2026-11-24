// @vitest-environment happy-dom
import { mount, flushPromises } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../src/App.vue";
import Credentials from "../src/components/Credentials.vue";
import DataTable from "../src/components/DataTable.vue";
import HistoryCharts from "../src/components/HistoryCharts.vue";
import Overview from "../src/components/Overview.vue";
import RecognitionStatistics from "../src/components/RecognitionStatistics.vue";
import Reviews from "../src/components/Reviews.vue";

const response = (data: unknown, status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  headers: new Headers({ "Content-Type": "application/json" }),
  json: async () => data,
});
const rows = {
  items: [{ id: "one", title: "AC/DC", created_at: 1700000000 }],
  total: 1,
  fields: ["id", "title", "created_at"],
  sortable: ["title", "created_at"],
  searchable: ["title"],
};
const snapshot = {
  overview: {
    worker_available: true,
    database_backend: "sqlite",
    database_bytes: 0,
    format_version: 1,
    references: { published: 3, stored: 4 },
    searchable_recordings: 2,
    submissions: { queued: 1 },
    manual_review: 0,
    artwork: {
      eligible: 10,
      ready: 3,
      missing: 1,
      pending: 1,
      unavailable: 1,
      expired: 1,
      unqueried: 3,
    },
  },
  revisions: { submissions: "one" },
  updated_at: new Date().toISOString(),
  failed: false,
};

class MockEventSource {
  static instance: MockEventSource;
  listeners: Record<string, (event: MessageEvent) => void> = {};
  onerror: (() => void) | null = null;
  closed = false;
  constructor(public url: string) {
    MockEventSource.instance = this;
  }
  addEventListener(name: string, handler: (event: MessageEvent) => void) {
    this.listeners[name] = handler;
  }
  close() {
    this.closed = true;
  }
  send(name: string, data: unknown = {}) {
    this.listeners[name]?.({ data: JSON.stringify(data) } as MessageEvent);
  }
}

beforeEach(() => {
  history.replaceState(null, "", "/admin/");
  vi.stubGlobal("EventSource", MockEventSource);
  vi.stubGlobal("fetch", vi.fn());
  vi.stubGlobal("scrollTo", vi.fn());
});
afterEach(() => {
  localStorage.removeItem("fingerprint-admin-theme");
  delete document.documentElement.dataset.theme;
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("shows recognition periods and handles unavailable statistics", async () => {
  const fetch = vi.mocked(globalThis.fetch);
  fetch.mockResolvedValueOnce(
    response({
      available: true,
      bucket_minutes: 60,
      buckets: [
        {
          bucket: 30000000,
          attempts: 5,
          matched: 2,
          no_match: 1,
          ambiguous: 1,
          failed: 1,
        },
      ],
    }) as Response,
  );
  fetch.mockResolvedValueOnce(
    response({ available: false, buckets: [] }) as Response,
  );
  const wrapper = mount(RecognitionStatistics, { props: { detail: true } });
  await flushPromises();
  expect(fetch.mock.calls[0]?.[0]).toBe(
    "/admin/api/recognition-statistics?period=day",
  );
  expect(wrapper.text()).toContain("5");
  expect(wrapper.text()).toContain("Times are UTC");
  expect(wrapper.findAll("tbody tr")).toHaveLength(1);
  await wrapper.get("#recognition-period").setValue("all");
  await flushPromises();
  expect(fetch.mock.calls[1]?.[0]).toBe(
    "/admin/api/recognition-statistics?period=all",
  );
  expect(wrapper.text()).toContain("not installed yet");
  wrapper.unmount();
});

it("shows recognition totals and a time-scaled chart on the overview", async () => {
  vi.mocked(globalThis.fetch).mockResolvedValue(
    response({
      available: true,
      bucket_minutes: 60,
      start_minute: 30000000,
      end_minute: 30001440,
      buckets: [
        {
          bucket: 30000000,
          attempts: 5,
          matched: 2,
          no_match: 1,
          ambiguous: 1,
          failed: 1,
        },
        {
          bucket: 30000120,
          attempts: 3,
          matched: 3,
          no_match: 0,
          ambiguous: 0,
          failed: 0,
        },
      ],
    }) as Response,
  );
  const wrapper = mount(RecognitionStatistics);
  await flushPromises();
  expect(wrapper.text()).toContain("8");
  expect(wrapper.findAll("tbody tr")).toHaveLength(0);
  const bars = wrapper.findAll("svg rect");
  expect(bars).toHaveLength(24);
  expect(Number(bars[2]!.attributes("x"))).toBeGreaterThan(
    Number(bars[0]!.attributes("width")),
  );
  expect(bars[23]!.attributes("height")).toBe("0");
  expect(Number(bars[23]!.attributes("x"))).toBe(575);
  expect(wrapper.get(".chart-dates").text()).toContain(
    new Date(30001440 * 60000).toISOString().replace("T", " ").slice(0, 16),
  );
  await wrapper.get('a[href="/admin/recognition"]').trigger("click");
  expect(wrapper.emitted("navigate")).toEqual([["/admin/recognition"]]);
  wrapper.unmount();
});

it("keeps an empty rolling chart and clips partial boundary hours", async () => {
  vi.mocked(globalThis.fetch).mockResolvedValue(
    response({
      available: true,
      bucket_minutes: 60,
      start_minute: 30000018,
      end_minute: 30001458,
      buckets: [],
    }) as Response,
  );
  const wrapper = mount(RecognitionStatistics);
  await flushPromises();
  const bars = wrapper.findAll("svg rect");
  expect(bars).toHaveLength(25);
  expect(bars.every((bar) => bar.attributes("height") === "0")).toBe(true);
  expect(Number(bars[0]!.attributes("width"))).toBeCloseTo((42 / 1440) * 600);
  const last = bars[24]!;
  expect(Number(last.attributes("width"))).toBeCloseTo((18 / 1440) * 600);
  expect(
    Number(last.attributes("x")) + Number(last.attributes("width")),
  ).toBeCloseTo(600);
  wrapper.unmount();
});

it("shows the full rolling week when the seven-day period is selected", async () => {
  const fetch = vi.mocked(globalThis.fetch);
  fetch.mockResolvedValue(
    response({
      available: true,
      bucket_minutes: 1440,
      start_minute: 29991378,
      end_minute: 30001458,
      buckets: [],
    }) as Response,
  );
  const wrapper = mount(RecognitionStatistics);
  await flushPromises();
  expect(wrapper.get('#recognition-period option[value="week"]').text()).toBe(
    "Last 7 days",
  );
  await wrapper.get("#recognition-period").setValue("week");
  await flushPromises();
  expect(fetch.mock.calls.at(-1)?.[0]).toBe(
    "/admin/api/recognition-statistics?period=week",
  );
  const bars = wrapper.findAll("svg rect");
  expect(bars).toHaveLength(8);
  expect(bars.every((bar) => bar.attributes("height") === "0")).toBe(true);
  expect(Number(bars[0]!.attributes("x"))).toBe(0);
  const last = bars[7]!;
  expect(
    Number(last.attributes("x")) + Number(last.attributes("width")),
  ).toBeCloseTo(600);
  wrapper.unmount();
});

it("refreshes recognition statistics when the live revision changes", async () => {
  const fetch = vi.mocked(globalThis.fetch);
  fetch.mockResolvedValue(
    response({
      available: true,
      bucket_minutes: 60,
      buckets: [
        {
          bucket: 30000000,
          attempts: 5,
          matched: 2,
          no_match: 1,
          ambiguous: 1,
          failed: 1,
        },
      ],
    }) as Response,
  );
  const wrapper = mount(RecognitionStatistics, {
    props: { revision: "1", detail: true },
  });
  await flushPromises();
  expect(fetch).toHaveBeenCalledTimes(1);
  await wrapper.setProps({ revision: "2" });
  await flushPromises();
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(wrapper.findAll("tbody tr")).toHaveLength(1);
  wrapper.unmount();
});

it("navigates to recognition details and loads the interval table", async () => {
  vi.mocked(globalThis.fetch).mockImplementation(async (input) => {
    const path = String(input);
    if (path.endsWith("/session"))
      return response({ authenticated: true }) as Response;
    if (path.endsWith("/overview")) return response(snapshot) as Response;
    if (path.endsWith("/history")) return response({ charts: [] }) as Response;
    return response({
      available: true,
      bucket_minutes: 60,
      start_minute: 30000000,
      end_minute: 30001440,
      buckets: [
        {
          bucket: 30000000,
          attempts: 5,
          matched: 2,
          no_match: 1,
          ambiguous: 1,
          failed: 1,
        },
      ],
    }) as Response;
  });
  const wrapper = mount(App);
  await flushPromises();
  await wrapper.get('nav a[href="/admin/recognition"]').trigger("click");
  await flushPromises();
  expect(location.pathname).toBe("/admin/recognition");
  expect(wrapper.get("h1").text()).toBe("Recognition activity");
  expect(wrapper.findAll("tbody tr")).toHaveLength(1);
  expect(
    wrapper.get('nav a[href="/admin/recognition"]').attributes("aria-current"),
  ).toBe("page");
  wrapper.unmount();
});

it("shows artwork for a published reference on its detail page only", async () => {
  const image = "https://archive.org/download/example/cover-500.jpg";
  const fetch = vi.mocked(globalThis.fetch);
  fetch.mockResolvedValueOnce(
    response({ id: "one", publication_ready: 1 }) as Response,
  );
  fetch.mockResolvedValueOnce(
    response({ status: "ready", url: image }) as Response,
  );
  const detail = mount(DataTable, {
    props: { dataset: "references", id: "one", snapshot },
  });
  await flushPromises();
  expect(fetch.mock.calls[1]?.[0]).toBe("/admin/api/references/one/artwork");
  expect(detail.get('img[alt="Album cover"]').attributes("src")).toBe(image);
  await detail.get('img[alt="Album cover"]').trigger("error");
  expect(detail.get('[role="status"]').text()).toContain("could not be loaded");
  detail.unmount();

  fetch.mockClear();
  fetch.mockResolvedValueOnce(
    response({ id: "one", publication_ready: 0 }) as Response,
  );
  const unpublished = mount(DataTable, {
    props: { dataset: "references", id: "one", snapshot },
  });
  await flushPromises();
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(unpublished.find('[aria-label="Album artwork"]').exists()).toBe(false);
  unpublished.unmount();
});

it("follows system changes and remembers a manual override on the login screen", async () => {
  const media = Object.assign(new EventTarget(), { matches: true });
  vi.spyOn(window, "matchMedia").mockImplementation(() =>
    Object.assign(media, {
      media: "(prefers-color-scheme: dark)",
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
    }),
  );
  vi.mocked(globalThis.fetch).mockResolvedValue(
    response({ authenticated: false }) as Response,
  );
  const first = mount(App);
  await flushPromises();
  const toggle = first.get(".theme-toggle");
  expect(toggle.attributes("aria-label")).toContain("Theme: system");
  expect(document.documentElement.dataset.theme).toBe("dark");
  media.matches = false;
  media.dispatchEvent(new Event("change"));
  expect(document.documentElement.dataset.theme).toBe("light");
  await toggle.trigger("click");
  expect(toggle.attributes("aria-label")).toContain("Theme: light");
  media.matches = true;
  media.dispatchEvent(new Event("change"));
  expect(document.documentElement.dataset.theme).toBe("light");
  first.unmount();

  const second = mount(App);
  await flushPromises();
  expect(second.get(".theme-toggle").attributes("aria-label")).toContain(
    "Theme: light",
  );
  await second.get(".theme-toggle").trigger("click");
  expect(document.documentElement.dataset.theme).toBe("dark");
  await second.get(".theme-toggle").trigger("click");
  expect(second.get(".theme-toggle").attributes("aria-label")).toContain(
    "Theme: system",
  );
  expect(document.documentElement.dataset.theme).toBe("dark");
  second.unmount();
});

describe("notifications", () => {
  it("only announces chart success after manual refresh and dismisses it automatically", async () => {
    const fetch = vi.mocked(globalThis.fetch);
    fetch.mockImplementation(
      async (input) =>
        response(
          String(input).endsWith("/session")
            ? { authenticated: true }
            : String(input).endsWith("/overview")
              ? snapshot
              : { charts: [] },
        ) as Response,
    );
    const wrapper = mount(App);
    await flushPromises();
    const toast = wrapper.get(".toast-region");
    expect(toast.text()).toBe("");
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    await wrapper
      .findAll("button")
      .find((button) => button.text() === "Refresh charts")!
      .trigger("click");
    await flushPromises();
    expect(toast.get('[role="status"]').text()).toBe("Charts updated.");
    await vi.advanceTimersByTimeAsync(3999);
    expect(toast.get('[role="status"]').text()).toBe("Charts updated.");
    await vi.advanceTimersByTimeAsync(1);
    expect(toast.text()).toBe("");
    wrapper.unmount();
  });

  it("announces successful clipboard copies and allows dismissing the notification", async () => {
    history.replaceState(null, "", "/admin/musicbrainz-reviews");
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.spyOn(navigator, "clipboard", "get").mockReturnValue({
      writeText,
    } as Clipboard);
    vi.mocked(globalThis.fetch).mockImplementation(
      async (input) =>
        response(
          String(input).endsWith("/session")
            ? { authenticated: true }
            : {
                items: [
                  {
                    submission_id: "one",
                    title: "Song",
                    metadata: { artists: ["Artist"] },
                    candidates: [],
                  },
                ],
                has_more: false,
              },
        ) as Response,
    );
    const wrapper = mount(App);
    await flushPromises();
    await wrapper.get('[aria-label="Copy title and artist"]').trigger("click");
    await flushPromises();
    expect(writeText).toHaveBeenCalledWith("Song - Artist");
    expect(wrapper.get('.toast-region [role="status"]').text()).toBe(
      "Title and artist copied.",
    );
    await wrapper.get('[aria-label="Dismiss notification"]').trigger("click");
    expect(wrapper.get(".toast-region").text()).toBe("");
    wrapper.unmount();
  });
});

describe("overview", () => {
  it("sorts submission counts numerically without changing the chart", async () => {
    vi.mocked(globalThis.fetch).mockResolvedValue(
      response({ charts: [] }) as Response,
    );
    const wrapper = mount(Overview, {
      props: {
        snapshot: {
          ...snapshot,
          overview: {
            ...snapshot.overview,
            submissions: { queued: 10, failed: 2 },
          },
        },
      },
    });
    await flushPromises();
    const table = wrapper.get('table[aria-label="Submission counts by state"]');
    const chart = wrapper.get(".donut");
    const style = chart.attributes("style");
    await table.findAll("th button")[1]!.trigger("click");
    expect(table.findAll("tbody tr")[0]!.text()).toContain("failed");
    expect(table.findAll("th")[1]!.attributes("aria-sort")).toBe("ascending");
    await table.findAll("th button")[1]!.trigger("click");
    expect(table.findAll("tbody tr")[0]!.text()).toContain("queued");
    expect(chart.attributes("style")).toBe(style);
    wrapper.unmount();
  });
  it("distinguishes cached lookup success from eligible reference coverage", async () => {
    vi.mocked(globalThis.fetch).mockResolvedValue(
      response({ charts: [] }) as Response,
    );
    const wrapper = mount(Overview, { props: { snapshot } });
    await flushPromises();
    expect(wrapper.text()).toContain(
      (0.75).toLocaleString(undefined, {
        style: "percent",
        maximumFractionDigits: 1,
      }),
    );
    expect(wrapper.text()).toContain("3 / 10");
    expect(wrapper.text()).toContain("Completed without a cover");
    expect(wrapper.text()).toContain("3 not yet looked up");
    expect(wrapper.text()).toContain("1 expired results awaiting a new lookup");
    expect(wrapper.text()).toContain("Not counted as completed lookups");
    wrapper.unmount();
  });

  it("does not show a success percentage before a completed lookup", async () => {
    vi.mocked(globalThis.fetch).mockResolvedValue(
      response({ charts: [] }) as Response,
    );
    const wrapper = mount(Overview, {
      props: {
        snapshot: {
          ...snapshot,
          overview: {
            ...snapshot.overview,
            artwork: { ...snapshot.overview.artwork, ready: 0, missing: 0 },
          },
        },
      },
    });
    await flushPromises();
    expect(wrapper.text()).toContain("Not measured");
    wrapper.unmount();
  });

  it.each(["de-DE", "de-CH", "en-US"])(
    "formats dashboard numbers with the browser default (%s)",
    async (locale) => {
      const nativeFormat = Number.prototype.toLocaleString;
      vi.spyOn(Number.prototype, "toLocaleString").mockImplementation(function (
        this: number,
        locales,
        options,
      ) {
        return nativeFormat.call(this, locales ?? locale, options);
      });
      vi.mocked(globalThis.fetch).mockResolvedValue(
        response({ charts: [] }) as Response,
      );
      const wrapper = mount(Overview, {
        props: {
          snapshot: {
            ...snapshot,
            overview: {
              ...snapshot.overview,
              database_bytes: 1234.5 * 1073741824,
              references: { published: 12345, stored: 23456 },
              searchable_recordings: 34567,
              submissions: { queued: 45678 },
              manual_review: 56789,
            },
          },
        },
      });
      await flushPromises();
      const format = (n: number) => nativeFormat.call(n, locale);
      for (const n of [12345, 23456, 34567, 45678, 56789]) {
        expect(wrapper.text()).toContain(format(n));
      }
      expect(wrapper.get(".records-table td:last-child").text()).toBe(
        format(45678),
      );
      expect(wrapper.get(".donut").attributes("aria-label")).toContain(
        format(45678),
      );
      expect(wrapper.text()).toContain(
        nativeFormat.call(1234.5, locale, {
          minimumFractionDigits: 2,
          maximumFractionDigits: 2,
        }),
      );
      wrapper.unmount();
    },
  );
  it("shows searchable recordings separately from acoustic references", async () => {
    vi.mocked(globalThis.fetch).mockImplementation(
      async (input) =>
        response(
          String(input).endsWith("/session")
            ? { authenticated: true }
            : String(input).endsWith("/overview")
              ? snapshot
              : { charts: [] },
        ) as Response,
    );
    const wrapper = mount(App);
    await flushPromises();
    const cards = wrapper.findAll(".stats .card");
    const recordings = cards.find(
      (card) => card.get(".subheader").text() === "Searchable recordings",
    );
    const references = cards.find(
      (card) => card.get(".subheader").text() === "Acoustic references",
    );
    expect(recordings?.get("strong").text()).toBe("2");
    expect(references?.get("strong").text()).toBe("3 published");
    expect(references?.get("small").text()).toBe("4 stored");
    wrapper.unmount();
  });
});

describe("table", () => {
  it("requires a refresh instead of appending after the data revision changes", async () => {
    const items = Array.from({ length: 60 }, (_, n) => ({
      id: String(n),
      title: `Title ${n}`,
    }));
    const fetch = vi.mocked(globalThis.fetch);
    fetch.mockResolvedValue(
      response({ ...rows, total: 60, items: items.slice(0, 50) }) as Response,
    );
    const wrapper = mount(DataTable, {
      props: { dataset: "submissions", snapshot },
    });
    await flushPromises();
    await wrapper.setProps({
      snapshot: { ...snapshot, revisions: { submissions: "two" } },
    });
    expect(
      wrapper.get(".table-footer button").attributes("disabled"),
    ).toBeDefined();
    expect(fetch).toHaveBeenCalledTimes(1);
    wrapper.unmount();
  });

  it("loads more on intersection and retries a failed batch only on request", async () => {
    let intersect: IntersectionObserverCallback;
    class MockObserver {
      constructor(callback: IntersectionObserverCallback) {
        intersect = callback;
      }
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    vi.stubGlobal("IntersectionObserver", MockObserver);
    const fetch = vi.mocked(globalThis.fetch);
    const items = Array.from({ length: 60 }, (_, n) => ({
      id: String(n),
      title: `Title ${n}`,
    }));
    fetch.mockImplementation(async (input) => {
      const skip = Number(
        new URL(String(input), location.origin).searchParams.get("skip"),
      );
      if (skip === 50 && fetch.mock.calls.length === 2)
        throw new Error("offline");
      return response({
        ...rows,
        total: items.length,
        items: items.slice(skip, skip + 50),
      }) as Response;
    });
    const wrapper = mount(DataTable, {
      attachTo: document.body,
      props: { dataset: "references", snapshot },
    });
    await flushPromises();
    intersect!(
      [{ isIntersecting: true } as IntersectionObserverEntry],
      {} as IntersectionObserver,
    );
    await flushPromises();
    expect(wrapper.text()).toContain("offline");
    intersect!(
      [{ isIntersecting: true } as IntersectionObserverEntry],
      {} as IntersectionObserver,
    );
    await flushPromises();
    expect(fetch).toHaveBeenCalledTimes(2);
    await wrapper.get(".table-footer button").trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("Showing 1 to 60 of 60");
    expect(wrapper.find('[aria-label="View 59"]').exists()).toBe(true);
    wrapper.unmount();
  });

  it("does not append a stale response after changing the sort", async () => {
    const fetch = vi.mocked(globalThis.fetch);
    let resolveMore: (value: Response) => void = () => {};
    const first = Array.from({ length: 50 }, (_, n) => ({
      id: String(n),
      title: `Title ${n}`,
    }));
    fetch.mockImplementation(async (input) => {
      const url = new URL(String(input), location.origin);
      if (url.searchParams.get("skip") === "50")
        return new Promise<Response>((resolve) => {
          resolveMore = resolve;
        });
      if (url.searchParams.has("order_by")) return response(rows) as Response;
      return response({ ...rows, items: first, total: 60 }) as Response;
    });
    const wrapper = mount(DataTable, {
      props: { dataset: "references", snapshot },
    });
    await flushPromises();
    await wrapper.get(".table-footer button").trigger("click");
    await wrapper.get("button.sort").trigger("click");
    await flushPromises();
    resolveMore(
      response({
        ...rows,
        total: 60,
        items: [{ id: "stale", title: "stale" }],
      }) as Response,
    );
    await flushPromises();
    expect(wrapper.text()).not.toContain("stale");
    expect(wrapper.text()).toContain("AC/DC");
    wrapper.unmount();
  });

  it("limits bulk actions to 100 loaded records", async () => {
    const items = Array.from({ length: 101 }, (_, n) => ({
      id: String(n),
      title: `Title ${n}`,
    }));
    vi.mocked(globalThis.fetch).mockImplementation(async (input) => {
      const skip = Number(
        new URL(String(input), location.origin).searchParams.get("skip"),
      );
      return response({
        ...rows,
        total: items.length,
        items: items.slice(skip, skip + 50),
      }) as Response;
    });
    const wrapper = mount(DataTable, {
      props: { dataset: "submissions", snapshot },
    });
    await flushPromises();
    await wrapper.get(".table-footer button").trigger("click");
    await flushPromises();
    await wrapper.get(".table-footer button").trigger("click");
    await flushPromises();
    await wrapper.get('[aria-label="Select all loaded rows"]').setValue(true);
    expect(wrapper.text()).toContain("select at most 100");
    expect(
      wrapper
        .findAll("button")
        .find((button) => button.text() === "Retry selected")!
        .attributes("disabled"),
    ).toBeDefined();
    wrapper.unmount();
  });

  it("restores loaded records when returning from a detail", async () => {
    history.replaceState(null, "", "/admin/submissions/list");
    const items = Array.from({ length: 60 }, (_, n) => ({
      id: String(n),
      title: `Title ${n}`,
    }));
    vi.mocked(globalThis.fetch).mockImplementation(async (input) => {
      const request = new URL(String(input), location.origin);
      if (request.pathname.endsWith("/session"))
        return response({ authenticated: true }) as Response;
      if (request.pathname.endsWith("/overview"))
        return response(snapshot) as Response;
      if (request.pathname === "/admin/api/tables/submissions") {
        const skip = Number(request.searchParams.get("skip"));
        return response({
          ...rows,
          total: items.length,
          items: items.slice(skip, skip + 50),
        }) as Response;
      }
      return response({ id: request.pathname.split("/").at(-1) }) as Response;
    });
    const wrapper = mount(App);
    await flushPromises();
    await wrapper.get(".table-footer button").trigger("click");
    await flushPromises();
    await wrapper.get('[aria-label="View 59"]').trigger("click");
    await flushPromises();
    await wrapper.get('nav[aria-label="Detail navigation"] a').trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("Showing 1 to 60 of 60");
    expect(wrapper.find('[aria-label="View 59"]').exists()).toBe(true);
    wrapper.unmount();
  });

  it("navigates submissions in list order across pages and restores the original list", async () => {
    history.replaceState(
      null,
      "",
      "/admin/submissions/list?state=pending_metadata",
    );
    const fetch = vi.mocked(globalThis.fetch);
    fetch.mockImplementation(async (input) => {
      const request = new URL(String(input), location.origin);
      if (request.pathname.endsWith("/session"))
        return response({ authenticated: true }) as Response;
      if (request.pathname.endsWith("/overview"))
        return response(snapshot) as Response;
      if (request.pathname === "/admin/api/tables/submissions") {
        expect(JSON.parse(request.searchParams.get("where")!)).toEqual({
          state: { eq: "pending_metadata" },
        });
        const skip = Number(request.searchParams.get("skip"));
        const limit = Number(request.searchParams.get("limit"));
        const items = Array.from({ length: 51 }, (_, n) => ({
          id: String(n + 1),
          title: `Title ${n + 1}`,
        }));
        return response({
          ...rows,
          total: items.length,
          items: items.slice(skip, skip + limit),
        }) as Response;
      }
      return response({ id: request.pathname.split("/").at(-1) }) as Response;
    });
    const wrapper = mount(App);
    await flushPromises();
    await wrapper.get('[aria-label="View 50"]').trigger("click");
    await flushPromises();
    expect(location.pathname).toBe("/admin/submissions/detail/50");
    const buttons = () =>
      wrapper.get('nav[aria-label="Detail navigation"]').findAll("button");
    await buttons()[1]!.trigger("click");
    await flushPromises();
    expect(location.pathname).toBe("/admin/submissions/detail/51");
    expect(buttons()[1]!.attributes("disabled")).toBeDefined();
    await buttons()[0]!.trigger("click");
    await flushPromises();
    expect(location.pathname).toBe("/admin/submissions/detail/50");
    await wrapper.get('nav[aria-label="Detail navigation"] a').trigger("click");
    await flushPromises();
    expect(location.search).toBe("?state=pending_metadata");
    expect(wrapper.get(".table-footer").text()).toContain("Showing 1 to 50");
    wrapper.unmount();
  });

  it("opens Pending metadata from the overview with an exact state filter", async () => {
    const fetch = vi.mocked(globalThis.fetch);
    fetch.mockImplementation(async (input) => {
      const path = String(input);
      if (path.endsWith("/session"))
        return response({ authenticated: true }) as Response;
      if (path.endsWith("/overview")) return response(snapshot) as Response;
      if (path.endsWith("/history"))
        return response({ charts: [] }) as Response;
      return response({ ...rows, searchable: ["title", "state"] }) as Response;
    });
    const wrapper = mount(App);
    await flushPromises();
    const shortcut = wrapper.get(
      'a[href="/admin/submissions/list?state=pending_metadata"]',
    );
    const review = wrapper.get('.stats a[href="/admin/musicbrainz-reviews"]');
    expect(review.text()).toBe("Review candidates");
    expect(shortcut.text()).toBe("View pending submissions");
    for (const [link, label] of [
      [shortcut, "Pending metadata"],
      [review, "Manual MusicBrainz review"],
    ] as const) {
      const card = link.element.closest(".card");
      expect(card?.firstElementChild?.matches("span.subheader")).toBe(true);
      expect(card?.firstElementChild?.textContent).toBe(label);
      expect(link.element.previousElementSibling?.tagName).toBe("STRONG");
    }
    await shortcut.trigger("click");
    await flushPromises();
    expect(location.search).toBe("?state=pending_metadata");
    const request = new URL(String(fetch.mock.lastCall?.[0]), location.origin);
    expect(request.pathname).toBe("/admin/api/tables/submissions");
    expect(JSON.parse(request.searchParams.get("where")!)).toEqual({
      state: { eq: "pending_metadata" },
    });
    expect(wrapper.get(".filter summary").text()).toContain("Filter (active)");
    wrapper.unmount();
  });

  it("shortens long values without changing the full value or record link", async () => {
    const id = "0123456789abcdef".repeat(4);
    const error =
      "An intentionally long error message that does not fit on one line.";
    vi.mocked(globalThis.fetch).mockResolvedValue(
      response({
        ...rows,
        fields: ["id", "title", "error"],
        items: [{ id, title: "Short title", error }],
      }) as Response,
    );
    const wrapper = mount(DataTable, {
      attachTo: document.body,
      props: { dataset: "unresolved", snapshot },
    });
    await flushPromises();
    const idCell = wrapper.get('[data-field="id"]');
    expect(idCell.attributes("title")).toBe(id);
    expect(idCell.get("a").text()).toBe("0123456789…bcdef");
    expect(idCell.get("a").attributes("href")).toBe(
      `/admin/unresolved/detail/${id}`,
    );
    expect(wrapper.get('[data-field="title"]').text()).toBe("Short title");
    const errorCell = wrapper.get('[data-field="error"]');
    expect(errorCell.attributes("title")).toBe(error);
    const button = errorCell.get("button");
    expect(button.text()).toBe(`${error.slice(0, 35)}…`);
    await button.trigger("click");
    expect(wrapper.get("dialog").attributes()).toHaveProperty("open");
    expect(wrapper.get("dialog p").text()).toBe(error);
    wrapper.unmount();
  });

  it("offers compact row actions without bypassing confirmation", async () => {
    const fetch = vi.mocked(globalThis.fetch);
    fetch.mockResolvedValue(response(rows) as Response);
    const confirm = vi
      .fn()
      .mockReturnValueOnce(false)
      .mockReturnValueOnce(true);
    vi.stubGlobal("confirm", confirm);
    const wrapper = mount(DataTable, {
      props: { dataset: "submissions", snapshot },
    });
    await flushPromises();
    expect(wrapper.get('[aria-label="View one"]').attributes("href")).toBe(
      "/admin/submissions/detail/one",
    );
    const remove = wrapper.get('[aria-label="Delete one"]');
    await remove.trigger("click");
    expect(fetch).toHaveBeenCalledTimes(1);
    await remove.trigger("click");
    await flushPromises();
    const action = fetch.mock.calls.find(([, init]) => init?.method === "POST");
    expect(JSON.parse(String(action?.[1]?.body))).toEqual({
      operation: "delete",
      ids: ["one"],
      confirmed: true,
    });
    wrapper.unmount();
  });

  it("can hide and restore columns without fetching or removing the detail link", async () => {
    const fetch = vi.mocked(globalThis.fetch);
    fetch.mockResolvedValue(response(rows) as Response);
    const wrapper = mount(DataTable, {
      props: { dataset: "references", snapshot },
    });
    await flushPromises();
    const options = wrapper.get(".column-options").findAll("input");
    expect(options[0]!.attributes("disabled")).toBeDefined();
    expect(wrapper.text()).toContain("Showing 1 to 1 of 1 entries");
    await options[1]!.setValue(false);
    expect(wrapper.get("table").text()).not.toContain("AC/DC");
    expect(wrapper.get("table a").attributes("href")).toBe(
      "/admin/references/detail/one",
    );
    await options[1]!.setValue(true);
    expect(wrapper.get("table").text()).toContain("AC/DC");
    expect(fetch).toHaveBeenCalledTimes(1);
    wrapper.unmount();
  });

  it("searches all records, applies literal filters and sends repeated sort parameters", async () => {
    const fetch = vi.mocked(globalThis.fetch);
    fetch.mockResolvedValue(response(rows) as Response);
    const wrapper = mount(DataTable, {
      props: { dataset: "submissions", snapshot },
    });
    await flushPromises();
    expect(wrapper.text()).toContain("2023-11-14 22:13:20 UTC");
    const search = wrapper.get('input[type="search"]');
    await search.setValue("ACDC");
    await search.trigger("keydown.enter");
    await flushPromises();
    let url = new URL(String(fetch.mock.lastCall?.[0]), "http://localhost");
    expect(JSON.parse(url.searchParams.get("where")!)).toBe("ACDC");
    const selects = wrapper.findAll(".filter select");
    await selects[0]!.setValue("title");
    await selects[1]!.setValue("eq");
    await wrapper.get(".filter input").setValue("AC/DC");
    await wrapper.get(".filter form").trigger("submit");
    await flushPromises();
    url = new URL(String(fetch.mock.lastCall?.[0]), "http://localhost");
    expect(JSON.parse(url.searchParams.get("where")!)).toEqual({
      and: ["ACDC", { title: { eq: "AC/DC" } }],
    });
    const sort = wrapper.findAll("button.sort");
    await sort[0]!.trigger("click");
    await flushPromises();
    await sort[1]!.trigger("click", { shiftKey: true });
    await flushPromises();
    url = new URL(String(fetch.mock.lastCall?.[0]), "http://localhost");
    expect(url.searchParams.getAll("order_by")).toEqual([
      "title:asc",
      "created_at:asc",
    ]);
    await sort[0]!.trigger("click", { shiftKey: true });
    await flushPromises();
    url = new URL(String(fetch.mock.lastCall?.[0]), "http://localhost");
    expect(url.searchParams.getAll("order_by")).toEqual([
      "title:desc",
      "created_at:asc",
    ]);
    await search.setValue("another search");
    const calls = fetch.mock.calls.length;
    await wrapper.get('button[aria-label="Clear search"]').trigger("click");
    await flushPromises();
    expect(search.element.value).toBe("");
    expect(wrapper.find('button[aria-label="Clear search"]').exists()).toBe(
      false,
    );
    expect(fetch).toHaveBeenCalledTimes(calls + 1);
    url = new URL(String(fetch.mock.lastCall?.[0]), "http://localhost");
    expect(JSON.parse(url.searchParams.get("where")!)).toEqual({
      title: { eq: "AC/DC" },
    });
    expect(url.searchParams.get("skip")).toBe("0");
    expect(url.searchParams.getAll("order_by")).toEqual([
      "title:desc",
      "created_at:asc",
    ]);
    wrapper.unmount();
  });

  it.each(["submissions", "references"])(
    "shows cached artwork in %s without extra requests",
    async (dataset) => {
      const fetch = vi.mocked(globalThis.fetch);
      const image = "https://archive.org/download/example/cover-500.jpg";
      fetch.mockResolvedValue(
        response({
          ...rows,
          items: [{ ...rows.items[0], artwork_url: image }],
        }) as Response,
      );
      const wrapper = mount(DataTable, {
        props: { dataset, snapshot },
      });

      await flushPromises();
      const heading = wrapper.get("th.artwork-cell");
      expect(heading.text()).toBe("Artwork");
      expect(heading.find("button").exists()).toBe(false);
      expect(wrapper.get("td.artwork-cell img").attributes("src")).toBe(image);
      expect(fetch).toHaveBeenCalledTimes(1);
      wrapper.unmount();
    },
  );

  it("shows the cached cover in submission details without an artwork request", async () => {
    const fetch = vi.mocked(globalThis.fetch);
    const image = "https://archive.org/download/example/cover-500.jpg";
    fetch.mockResolvedValue(
      response({ id: "one", artwork_url: image }) as Response,
    );
    const wrapper = mount(DataTable, {
      props: { dataset: "submissions", id: "one", snapshot },
    });
    await flushPromises();
    expect(wrapper.get("img.detail-cover").attributes("src")).toBe(image);
    expect(wrapper.text()).not.toContain("artwork_url");
    expect(fetch).toHaveBeenCalledTimes(1);
    wrapper.unmount();
  });

  it("requires confirmation for bulk actions and never replaces rows on a live revision", async () => {
    const fetch = vi.mocked(globalThis.fetch);
    fetch.mockResolvedValue(response(rows) as Response);
    const confirm = vi
      .fn()
      .mockReturnValueOnce(false)
      .mockReturnValueOnce(true);
    vi.stubGlobal("confirm", confirm);
    const wrapper = mount(DataTable, {
      props: { dataset: "submissions", snapshot },
    });
    await flushPromises();
    await wrapper.setProps({
      snapshot: { ...snapshot, revisions: { submissions: "two" } },
    });
    expect(wrapper.text()).toContain("Newer data available");
    expect(fetch).toHaveBeenCalledTimes(1);
    await wrapper.get('input[aria-label="Select one"]').setValue(true);
    const retry = wrapper
      .findAll("button")
      .find((button) => button.text() === "Retry selected")!;
    await retry.trigger("click");
    expect(fetch).toHaveBeenCalledTimes(1);
    await retry.trigger("click");
    await flushPromises();
    expect(confirm).toHaveBeenCalledTimes(2);
    expect(JSON.parse(String(fetch.mock.calls[1]?.[1]?.body))).toEqual({
      operation: "retry",
      ids: ["one"],
      confirmed: true,
    });
    wrapper.unmount();
  });

  it("shows failed refreshes without discarding previous results", async () => {
    const fetch = vi.mocked(globalThis.fetch);
    fetch
      .mockResolvedValueOnce(response(rows) as Response)
      .mockResolvedValueOnce(
        response({ detail: "Database unavailable" }, 503) as Response,
      );
    const wrapper = mount(DataTable, {
      props: { dataset: "submissions", snapshot },
    });
    await flushPromises();
    await wrapper.get('input[aria-label="Select one"]').setValue(true);
    await wrapper.get('input[type="search"]').setValue("x");
    await wrapper.get('input[type="search"]').trigger("keydown.enter");
    await flushPromises();
    expect(wrapper.text()).toContain("Database unavailable");
    expect(wrapper.text()).toContain("AC/DC");
    expect(wrapper.text()).toContain("Retry table refresh");
    await wrapper.get('input[aria-label="Select one"]').setValue(true);
    expect(
      wrapper
        .findAll("button")
        .find((button) => button.text() === "Delete selected")
        ?.attributes("disabled"),
    ).toBeDefined();
    wrapper.unmount();
  });

  it("loads detail metadata and returns to the list after confirmed deletion", async () => {
    const fetch = vi.mocked(globalThis.fetch);
    fetch
      .mockResolvedValueOnce(
        response({ id: "a/b", metadata: { album: "Album" } }) as Response,
      )
      .mockResolvedValueOnce(response({}) as Response);
    vi.stubGlobal("confirm", vi.fn().mockReturnValue(true));
    const wrapper = mount(DataTable, {
      props: { dataset: "submissions", id: "a/b", snapshot },
    });
    await flushPromises();
    expect(fetch.mock.calls[0]?.[0]).toBe(
      "/admin/api/tables/submissions/a%2Fb",
    );
    expect(wrapper.text()).toContain("Album");
    await wrapper
      .findAll("button")
      .find((button) => button.text() === "Delete submission")!
      .trigger("click");
    await flushPromises();
    expect(JSON.parse(String(fetch.mock.calls[1]?.[1]?.body))).toEqual({
      operation: "delete",
      ids: ["a/b"],
      confirmed: true,
    });
    expect(wrapper.emitted("navigate")?.[0]).toEqual([
      "/admin/submissions/list",
    ]);
    wrapper.unmount();
  });
});

describe("reviews and history", () => {
  it("retries the failed page without allowing changes to stale reviews", async () => {
    const fetch = vi.mocked(globalThis.fetch);
    const review = {
      submission_id: "s1",
      title: "First page",
      candidates: [{ id: "mbid" }],
    };
    fetch
      .mockResolvedValueOnce(
        response({ items: [review], has_more: true }) as Response,
      )
      .mockResolvedValueOnce(
        response({ detail: "Database unavailable" }, 503) as Response,
      )
      .mockResolvedValueOnce(
        response({
          items: [{ ...review, title: "Second page" }],
          has_more: false,
        }) as Response,
      );
    const wrapper = mount(Reviews);
    await flushPromises();
    await wrapper
      .findAll("button")
      .find((button) => button.text() === "Next reviews")!
      .trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("First page");
    expect(wrapper.get("form button").attributes("disabled")).toBeDefined();
    await wrapper.get('[role="alert"] button').trigger("click");
    await flushPromises();
    expect(fetch.mock.lastCall?.[0]).toBe("/admin/api/reviews?skip=50");
    expect(wrapper.text()).toContain("Second page");
    expect(wrapper.text()).not.toContain("Database unavailable");
    wrapper.unmount();
  });

  it("offers a selected manual copy fallback and submits the chosen recording", async () => {
    const fetch = vi.mocked(globalThis.fetch);
    const review = {
      submission_id: "s1",
      title: "Song",
      metadata: { artists: ["Artist"] },
      candidates: [
        {
          id: "mbid",
          title: "Song",
          artists: ["Artist"],
          length: 3661000,
          earliest_album: { title: "Album", date: "2000" },
          albums: ["Album"],
        },
      ],
    };
    fetch.mockResolvedValue(
      response({ items: [review], has_more: false }) as Response,
    );
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: undefined,
    });
    const wrapper = mount(Reviews, { attachTo: document.body });
    await flushPromises();
    expect(wrapper.text()).toContain("01:01:01");
    expect(wrapper.text()).toContain("Earliest known official album");
    await wrapper.get('[aria-label="Copy title and artist"]').trigger("click");
    await flushPromises();
    const input = wrapper.get('[aria-label="Title and artist to copy"]')
      .element as HTMLInputElement;
    expect(input.value).toBe("Song - Artist");
    expect(input.selectionStart).toBe(0);
    expect(input.selectionEnd).toBe(input.value.length);
    await wrapper.get('input[type="radio"]').setValue(true);
    await wrapper.get("form").trigger("submit");
    await flushPromises();
    expect(JSON.parse(String(fetch.mock.calls[1]?.[1]?.body))).toEqual({
      submission_id: "s1",
      selected_mbid: "mbid",
    });
    wrapper.unmount();
  });

  it("does not show an empty album credits list", async () => {
    vi.mocked(globalThis.fetch).mockResolvedValue(
      response({
        items: [
          {
            submission_id: "s1",
            title: "Song",
            candidates: [{ id: "mbid", title: "Song", albums: [] }],
          },
        ],
        has_more: false,
      }) as Response,
    );
    const wrapper = mount(Reviews);
    await flushPromises();
    expect(wrapper.text()).not.toContain("Show album credits");
    expect(wrapper.find('input[type="radio"]').exists()).toBe(true);
    wrapper.unmount();
  });

  it("shows other and unclassified releases separately from album credits", async () => {
    vi.mocked(globalThis.fetch).mockResolvedValue(
      response({
        items: [
          {
            submission_id: "s1",
            candidates: [
              {
                id: "mbid",
                albums: ["Studio"],
                other_releases: [
                  { title: "Single title", type: "single" },
                  { title: "Sampler", type: "compilation" },
                  { title: "Unden!able", type: "unknown" },
                ],
              },
            ],
          },
        ],
        has_more: false,
      }) as Response,
    );
    const wrapper = mount(Reviews);
    await flushPromises();
    expect(wrapper.text()).toContain("Show album credits");
    expect(wrapper.text()).toContain("Show other releases");
    expect(wrapper.text()).toContain("Single title — single");
    expect(wrapper.text()).toContain("Sampler — compilation");
    expect(wrapper.text()).toContain("Unden!able — type unknown");
    wrapper.unmount();
  });

  it("renders server paths without bridging missing hours", () => {
    const wrapper = mount(HistoryCharts, {
      props: {
        charts: [
          [
            "Queue",
            {
              maximum: 12345,
              start: "start",
              end: "end",
              last_at: "last",
              series: [
                {
                  label: "Queued",
                  color: "#123456",
                  path: "M0,1 L1,2 M3,4",
                  dots: [
                    [0, 1],
                    [3, 4],
                  ],
                  last: 1234,
                },
              ],
            },
          ],
        ],
      },
    });
    expect(wrapper.get('path[stroke="#123456"]').attributes("d")).toBe(
      "M0,1 L1,2 M3,4",
    );
    expect(wrapper.findAll("circle")).toHaveLength(2);
    expect(wrapper.get(".chart-axis").text()).toContain(
      (12345).toLocaleString(),
    );
    expect(wrapper.get(".legend strong").text()).toBe((1234).toLocaleString());
    wrapper.unmount();
  });

  it("shows cached covers against a separately labelled right axis", () => {
    const wrapper = mount(HistoryCharts, {
      props: {
        charts: [
          [
            "Submission backlog",
            {
              maximum: 3,
              secondary_maximum: 1000,
              start: "start",
              end: "end",
              last_at: "last",
              series: [
                {
                  label: "Cached covers",
                  color: "#0c8599",
                  path: "M0,20",
                  dots: [[0, 20]],
                  last: 800,
                  axis: "right",
                },
              ],
            },
          ],
        ],
      },
    });
    expect(wrapper.get('[aria-label="Cached covers scale"]').text()).toContain(
      (1000).toLocaleString(),
    );
    expect(wrapper.get(".legend").text()).toContain("Cached covers");
    expect(wrapper.get(".legend").text()).toContain("right axis");
    expect(wrapper.get('path[stroke="#0c8599"]').attributes("d")).toBe("M0,20");
    wrapper.unmount();
  });
});

describe("credentials", () => {
  it("sorts credentials without changing their actions", async () => {
    vi.mocked(globalThis.fetch).mockResolvedValue(
      response({
        items: [
          { username: "zulu", role: "read" },
          { username: "alpha", role: "upload" },
        ],
      }) as Response,
    );
    const wrapper = mount(Credentials, { props: { kind: "keys" } });
    await flushPromises();
    const rows = () => wrapper.findAll("tbody tr").map((row) => row.text());
    expect(rows()[0]).toContain("alpha");
    await wrapper.get("th[aria-sort] button").trigger("click");
    expect(rows()[0]).toContain("zulu");
    expect(wrapper.get("th[aria-sort]").attributes("aria-sort")).toBe(
      "descending",
    );
    wrapper.unmount();
  });
  it.each(["keys", "applications"] as const)(
    "shows %s secrets once and requires revocation confirmation",
    async (kind) => {
      const fetch = vi.mocked(globalThis.fetch);
      const item =
        kind === "keys"
          ? { username: "test-1", role: "read" }
          : { id: "1", name: "test", revoked_at: null };
      fetch.mockImplementation(
        async (_input, init) =>
          response(
            init?.method === "POST" &&
              JSON.parse(String(init.body)).operation === "issue"
              ? { new_key: "example-secret" }
              : { items: [item], legacy_configured: true },
          ) as Response,
      );
      const wrapper = mount(Credentials, { props: { kind } });
      await flushPromises();
      await wrapper.get('form input:not([type="checkbox"])').setValue("test");
      await wrapper.findAll("form")[0]!.trigger("submit");
      await flushPromises();
      expect(wrapper.text()).toContain("example-secret");
      expect(wrapper.text()).toContain("only once");
      const revokeForm = wrapper.get(
        ".table-wrap > .records-table .revoke-actions",
      );
      const callsBeforeConfirmation = fetch.mock.calls.length;
      await revokeForm.trigger("submit");
      expect(fetch.mock.calls).toHaveLength(callsBeforeConfirmation);
      await revokeForm.get('input[type="checkbox"]').setValue(true);
      await revokeForm.trigger("submit");
      await flushPromises();
      const call = fetch.mock.calls.find(
        ([, init]) =>
          init?.method === "POST" &&
          JSON.parse(String(init.body)).operation === "revoke",
      )!;
      expect(JSON.parse(String(call[1]?.body)).confirmed).toBe(true);
      await wrapper
        .findAll("button")
        .find((b) => b.text() === "Dismiss key")!
        .trigger("click");
      expect(wrapper.text()).not.toContain("example-secret");
      wrapper.unmount();
    },
  );
});

describe("navigation", () => {
  it("shows page links in the sidebar and keeps the parent section active on detail pages", async () => {
    history.replaceState(null, "", "/admin/references/detail/one");
    const fetch = vi.mocked(globalThis.fetch);
    fetch.mockImplementation(
      async (input) =>
        response(
          String(input).endsWith("/session")
            ? { authenticated: true }
            : String(input).endsWith("/reviews?skip=0")
              ? { items: [], has_more: false }
              : { id: "one" },
        ) as Response,
    );
    const wrapper = mount(App, { attachTo: document.body });
    await flushPromises();
    expect(wrapper.find("header nav").exists()).toBe(false);
    expect(wrapper.get(".topbar").classes()).toContain("topbar-with-sidebar");
    expect(wrapper.get(".sidebar-brand").text()).toBe(
      "Fingerprint administration",
    );
    const nav = wrapper.get('.sidebar nav[aria-label="Administration"]');
    expect(nav.findAll("a")).toHaveLength(8);
    for (const link of nav.findAll("a")) {
      expect(link.get("svg").attributes("aria-hidden")).toBe("true");
      expect(link.get("span").text()).not.toBe("");
    }
    expect(nav.get('[aria-current="page"]').text()).toBe("References");
    const toggle = wrapper.get('[aria-controls="admin-navigation"]');
    expect(toggle.attributes("aria-label")).toBe("Show menu");
    expect(toggle.text()).toBe("");
    const closedIcon = toggle.get("svg path").attributes("d");
    await toggle.trigger("click");
    expect(toggle.attributes("aria-expanded")).toBe("true");
    expect(toggle.attributes("aria-label")).toBe("Hide menu");
    expect(toggle.get("svg path").attributes("d")).not.toBe(closedIcon);
    await nav.get('a[href="/admin/musicbrainz-reviews"]').trigger("click");
    await flushPromises();
    expect(location.pathname).toBe("/admin/musicbrainz-reviews");
    expect(nav.get('[aria-current="page"]').text()).toBe("MusicBrainz reviews");
    expect(toggle.attributes("aria-expanded")).toBe("false");
    expect(document.activeElement).toBe(wrapper.get("main").element);
    wrapper.unmount();
  });
});

describe("session", () => {
  it("shows a compact live badge and keeps the last check time in its details", async () => {
    vi.mocked(globalThis.fetch).mockImplementation(
      async (input) =>
        response(
          String(input).endsWith("/session")
            ? { authenticated: true }
            : String(input).endsWith("/overview")
              ? snapshot
              : { charts: [] },
        ) as Response,
    );
    const wrapper = mount(App);
    await flushPromises();
    const badge = wrapper.get(".live-badge");
    const details = wrapper.get(".live-status");
    expect(badge.text()).toBe("Connecting");
    MockEventSource.instance.send("snapshot", {
      ...snapshot,
      updated_at: new Date().toISOString(),
    });
    await flushPromises();
    expect(badge.text()).toBe("Live");
    expect(badge.attributes("title")).toContain(
      "Live updates connected — checked",
    );
    expect(details.attributes()).not.toHaveProperty("open");
    await badge.trigger("click");
    expect(details.get(".live-popover").text()).toBe(badge.attributes("title"));
    (details.element as HTMLDetailsElement).open = true;
    await badge.trigger("keydown", { key: "Escape" });
    expect(details.attributes()).not.toHaveProperty("open");
    MockEventSource.instance.send("unavailable");
    await flushPromises();
    expect(badge.text()).toBe("Delayed");
    MockEventSource.instance.onerror?.();
    await flushPromises();
    expect(badge.text()).toBe("Reconnecting");
    wrapper.unmount();
  });

  it("explains when a frontend-only preview serves HTML instead of the session API", async () => {
    vi.mocked(globalThis.fetch).mockResolvedValue(
      new Response("<!doctype html><title>Frontend preview</title>", {
        headers: { "Content-Type": "text/html" },
      }),
    );
    const wrapper = mount(App);
    await flushPromises();
    expect(wrapper.get('[role="alert"]').text()).toContain("Admin service URL");
    expect(wrapper.text()).not.toContain("Unexpected token");
    wrapper.unmount();
  });

  it("loads an authenticated session and closes events on expiry", async () => {
    const fetch = vi.mocked(globalThis.fetch);
    fetch.mockImplementation(
      async (input) =>
        response(
          String(input).endsWith("/session")
            ? { authenticated: true }
            : String(input).endsWith("/overview")
              ? snapshot
              : { charts: [] },
        ) as Response,
    );
    const wrapper = mount(App);
    await flushPromises();
    expect(MockEventSource.instance.url).toBe("/admin/events");
    expect(wrapper.text()).toContain("Fingerprint administration");
    MockEventSource.instance.send("session-expired");
    await flushPromises();
    expect(MockEventSource.instance.closed).toBe(true);
    expect(wrapper.text()).toContain("Session ended");
    expect(location.pathname).toBe("/admin/login");
    expect(wrapper.find("#admin-navigation").exists()).toBe(false);
    wrapper.unmount();
  });

  it("keeps login errors visible and starts events after signing in", async () => {
    const fetch = vi.mocked(globalThis.fetch);
    fetch.mockImplementation(async (input, init) => {
      if (String(input).endsWith("/session"))
        return response({ authenticated: false }) as Response;
      if (String(input).endsWith("/login")) {
        const accepted = JSON.parse(String(init?.body)).password === "correct"; // pragma: allowlist secret
        return response(
          accepted ? {} : { detail: "Invalid credentials" },
          accepted ? 200 : 401,
        ) as Response;
      }
      return response(
        String(input).endsWith("/overview") ? snapshot : { charts: [] },
      ) as Response;
    });
    const wrapper = mount(App);
    await flushPromises();
    await wrapper.get('input[autocomplete="username"]').setValue("admin");
    await wrapper.get('input[type="password"]').setValue("wrong");
    await wrapper.get("form").trigger("submit");
    await flushPromises();
    expect(wrapper.text()).toContain("Invalid credentials");
    await wrapper.get('input[type="password"]').setValue("correct");
    await wrapper.get("form").trigger("submit");
    await flushPromises();
    expect(location.pathname).toBe("/admin/");
    expect(MockEventSource.instance.url).toBe("/admin/events");
    wrapper.unmount();
  });

  it("posts logout and tears down the live stream", async () => {
    const fetch = vi.mocked(globalThis.fetch);
    fetch.mockImplementation(
      async (input) =>
        response(
          String(input).endsWith("/session")
            ? { authenticated: true }
            : String(input).endsWith("/overview")
              ? snapshot
              : String(input).endsWith("/history")
                ? { charts: [] }
                : {},
        ) as Response,
    );
    const wrapper = mount(App);
    await flushPromises();
    const signOut = wrapper.get('button[aria-label="Sign out"]');
    expect(signOut.get("svg").attributes("aria-hidden")).toBe("true");
    expect(signOut.text()).toBe("");
    await signOut.trigger("click");
    await flushPromises();
    const call = fetch.mock.calls.find(([input]) =>
      String(input).endsWith("/logout"),
    )!;
    expect(JSON.parse(String(call[1]?.body))).toEqual({});
    expect(MockEventSource.instance.closed).toBe(true);
    expect(location.pathname).toBe("/admin/login");
    wrapper.unmount();
  });

  it("expires the session on a protected API 401", async () => {
    const fetch = vi.mocked(globalThis.fetch);
    fetch.mockImplementation(async (input) =>
      String(input).endsWith("/session")
        ? (response({ authenticated: true }) as Response)
        : String(input).endsWith("/overview")
          ? (response({ detail: "Expired" }, 401) as Response)
          : (response({ charts: [] }) as Response),
    );
    const wrapper = mount(App);
    await flushPromises();
    expect(wrapper.text()).toContain("Session ended");
    expect(location.pathname).toBe("/admin/login");
    expect(MockEventSource.instance.closed).toBe(true);
    wrapper.unmount();
  });

  it("checks idle pages without live updates and only signs out on confirmed expiry", async () => {
    vi.useFakeTimers();
    history.replaceState(null, "", "/admin/keys");
    const fetch = vi.mocked(globalThis.fetch);
    const sessions = vi
      .fn()
      .mockResolvedValueOnce(response({ authenticated: true }))
      .mockRejectedValueOnce(new TypeError("Network unavailable"))
      .mockResolvedValueOnce(response({ authenticated: false }));
    fetch.mockImplementation(async (input) =>
      String(input).endsWith("/session")
        ? sessions()
        : (response({ items: [] }) as Response),
    );
    const wrapper = mount(App);
    await flushPromises();
    expect(sessions).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(15000);
    expect(sessions).toHaveBeenCalledTimes(2);
    expect(location.pathname).toBe("/admin/keys");
    expect(wrapper.text()).toContain("Could not verify the session");

    vi.spyOn(document, "hidden", "get").mockReturnValue(true);
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(30000);
    expect(sessions).toHaveBeenCalledTimes(2);

    vi.spyOn(document, "hidden", "get").mockReturnValue(false);
    document.dispatchEvent(new Event("visibilitychange"));
    await flushPromises();
    expect(sessions).toHaveBeenCalledTimes(3);
    expect(wrapper.text()).toContain("Session ended");
    expect(location.pathname).toBe("/admin/login");
    expect(wrapper.find('[aria-label="Sign out"]').exists()).toBe(false);
    await vi.advanceTimersByTimeAsync(15000);
    expect(sessions).toHaveBeenCalledTimes(3);
    wrapper.unmount();
  });
});
