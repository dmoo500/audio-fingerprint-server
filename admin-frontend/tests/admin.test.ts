// @vitest-environment happy-dom
import { mount, flushPromises } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../src/App.vue";
import Credentials from "../src/components/Credentials.vue";
import DataTable from "../src/components/DataTable.vue";
import HistoryCharts from "../src/components/HistoryCharts.vue";
import Reviews from "../src/components/Reviews.vue";

const response = (data: unknown, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => data });
const rows = { items: [{ id: "one", title: "AC/DC", created_at: 1700000000 }], total: 1, fields: ["id", "title", "created_at"], sortable: ["title", "created_at"], searchable: ["title"] };
const snapshot = {
  overview: { worker_available: true, database_backend: "sqlite", database_bytes: 0, format_version: 1, references: { published: 1, stored: 1 }, submissions: { queued: 1 }, manual_review: 0 },
  revisions: { submissions: "one" }, updated_at: new Date().toISOString(), failed: false,
};

class MockEventSource {
  static instance: MockEventSource;
  listeners: Record<string, (event: MessageEvent) => void> = {};
  onerror: (() => void) | null = null;
  closed = false;
  constructor(public url: string) { MockEventSource.instance = this; }
  addEventListener(name: string, handler: (event: MessageEvent) => void) { this.listeners[name] = handler; }
  close() { this.closed = true; }
  send(name: string, data: unknown = {}) { this.listeners[name]?.({ data: JSON.stringify(data) } as MessageEvent); }
}

beforeEach(() => {
  history.replaceState(null, "", "/admin/");
  vi.stubGlobal("EventSource", MockEventSource);
  vi.stubGlobal("fetch", vi.fn());
  vi.stubGlobal("scrollTo", vi.fn());
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("table", () => {
  it("searches all records, applies literal filters and sends repeated sort parameters", async () => {
    const fetch = vi.mocked(globalThis.fetch);
    fetch.mockResolvedValue(response(rows) as Response);
    const wrapper = mount(DataTable, { props: { dataset: "submissions", snapshot } });
    await flushPromises();
    expect(wrapper.text()).toContain("2023-11-14 22:13:20 UTC");
    const search = wrapper.get('input[type="search"]');
    await search.setValue("ACDC");
    await search.trigger("keydown.enter");
    await flushPromises();
    let url = new URL(String(fetch.mock.lastCall?.[0]), "http://localhost");
    expect(JSON.parse(url.searchParams.get("where")!)).toBe("ACDC");
    const selects = wrapper.findAll("select");
    await selects[0]!.setValue("title");
    await selects[1]!.setValue("eq");
    await wrapper.get('.filter input').setValue("AC/DC");
    await wrapper.get(".filter form").trigger("submit");
    await flushPromises();
    url = new URL(String(fetch.mock.lastCall?.[0]), "http://localhost");
    expect(JSON.parse(url.searchParams.get("where")!)).toEqual({ and: ["ACDC", { title: { eq: "AC/DC" } }] });
    const sort = wrapper.findAll("button.sort");
    await sort[0]!.trigger("click");
    await flushPromises();
    await sort[1]!.trigger("click", { shiftKey: true });
    await flushPromises();
    url = new URL(String(fetch.mock.lastCall?.[0]), "http://localhost");
    expect(url.searchParams.getAll("order_by")).toEqual(["title:asc", "created_at:asc"]);
    wrapper.unmount();
  });

  it("requires confirmation for bulk actions and never replaces rows on a live revision", async () => {
    const fetch = vi.mocked(globalThis.fetch);
    fetch.mockResolvedValue(response(rows) as Response);
    const confirm = vi.fn().mockReturnValueOnce(false).mockReturnValueOnce(true);
    vi.stubGlobal("confirm", confirm);
    const wrapper = mount(DataTable, { props: { dataset: "submissions", snapshot } });
    await flushPromises();
    await wrapper.setProps({ snapshot: { ...snapshot, revisions: { submissions: "two" } } });
    expect(wrapper.text()).toContain("Newer data available");
    expect(fetch).toHaveBeenCalledTimes(1);
    await wrapper.get('input[aria-label="Select one"]').setValue(true);
    const retry = wrapper.findAll("button").find(button => button.text() === "Retry selected")!;
    await retry.trigger("click");
    expect(fetch).toHaveBeenCalledTimes(1);
    await retry.trigger("click");
    await flushPromises();
    expect(confirm).toHaveBeenCalledTimes(2);
    expect(JSON.parse(String(fetch.mock.calls[1]?.[1]?.body))).toEqual({ operation: "retry", ids: ["one"], confirmed: true });
    wrapper.unmount();
  });

  it("shows failed refreshes without discarding previous results", async () => {
    const fetch = vi.mocked(globalThis.fetch);
    fetch.mockResolvedValueOnce(response(rows) as Response).mockResolvedValueOnce(response({ detail: "Database unavailable" }, 503) as Response);
    const wrapper = mount(DataTable, { props: { dataset: "submissions", snapshot } });
    await flushPromises();
    await wrapper.get('input[aria-label="Select one"]').setValue(true);
    await wrapper.get('input[type="search"]').setValue("x");
    await wrapper.get('input[type="search"]').trigger("keydown.enter");
    await flushPromises();
    expect(wrapper.text()).toContain("Database unavailable");
    expect(wrapper.text()).toContain("AC/DC");
    expect(wrapper.text()).toContain("Retry table refresh");
    await wrapper.get('input[aria-label="Select one"]').setValue(true);
    expect(wrapper.findAll("button").find(button => button.text() === "Delete selected")?.attributes("disabled")).toBeDefined();
    wrapper.unmount();
  });

  it("loads detail metadata and returns to the list after confirmed deletion", async () => {
    const fetch = vi.mocked(globalThis.fetch);
    fetch.mockResolvedValueOnce(response({ id: "a/b", metadata: { album: "Album" } }) as Response).mockResolvedValueOnce(response({}) as Response);
    vi.stubGlobal("confirm", vi.fn().mockReturnValue(true));
    const wrapper = mount(DataTable, { props: { dataset: "submissions", id: "a/b", snapshot } });
    await flushPromises();
    expect(fetch.mock.calls[0]?.[0]).toBe("/admin/api/tables/submissions/a%2Fb");
    expect(wrapper.text()).toContain("Album");
    await wrapper.findAll("button").find(button => button.text() === "Delete submission")!.trigger("click");
    await flushPromises();
    expect(JSON.parse(String(fetch.mock.calls[1]?.[1]?.body))).toEqual({ operation: "delete", ids: ["a/b"], confirmed: true });
    expect(wrapper.emitted("navigate")?.[0]).toEqual(["/admin/submissions/list"]);
    wrapper.unmount();
  });
});

describe("reviews and history", () => {
  it("retries the failed page without allowing changes to stale reviews", async () => {
    const fetch = vi.mocked(globalThis.fetch);
    const review = { submission_id: "s1", title: "First page", candidates: [{ id: "mbid" }] };
    fetch.mockResolvedValueOnce(response({ items: [review], has_more: true }) as Response)
      .mockResolvedValueOnce(response({ detail: "Database unavailable" }, 503) as Response)
      .mockResolvedValueOnce(response({ items: [{ ...review, title: "Second page" }], has_more: false }) as Response);
    const wrapper = mount(Reviews);
    await flushPromises();
    await wrapper.findAll("button").find(button => button.text() === "Next reviews")!.trigger("click");
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
    const review = { submission_id: "s1", title: "Song", metadata: { artists: ["Artist"] }, candidates: [{ id: "mbid", title: "Song", artists: ["Artist"], length: 3661000, earliest_album: { title: "Album", date: "2000" }, albums: ["Album"] }] };
    fetch.mockResolvedValue(response({ items: [review], has_more: false }) as Response);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
    const wrapper = mount(Reviews, { attachTo: document.body });
    await flushPromises();
    expect(wrapper.text()).toContain("01:01:01");
    expect(wrapper.text()).toContain("Earliest known official album");
    await wrapper.get('[aria-label="Copy title and artist"]').trigger("click");
    await flushPromises();
    const input = wrapper.get('[aria-label="Title and artist to copy"]').element as HTMLInputElement;
    expect(input.value).toBe("Song - Artist");
    expect(input.selectionStart).toBe(0);
    expect(input.selectionEnd).toBe(input.value.length);
    await wrapper.get('input[type="radio"]').setValue(true);
    await wrapper.get("form").trigger("submit");
    await flushPromises();
    expect(JSON.parse(String(fetch.mock.calls[1]?.[1]?.body))).toEqual({ submission_id: "s1", selected_mbid: "mbid" });
    wrapper.unmount();
  });

  it("renders server paths without bridging missing hours", () => {
    const wrapper = mount(HistoryCharts, { props: { charts: [["Queue", { maximum: 3, start: "start", end: "end", last_at: "last", series: [{ label: "Queued", color: "#123456", path: "M0,1 L1,2 M3,4", dots: [[0, 1], [3, 4]], last: 2 }] }]] } });
    expect(wrapper.get('path[stroke="#123456"]').attributes("d")).toBe("M0,1 L1,2 M3,4");
    expect(wrapper.findAll("circle")).toHaveLength(2);
    wrapper.unmount();
  });
});

describe("credentials", () => {
  it.each(["keys", "applications"] as const)("shows %s secrets once and requires revocation confirmation", async kind => {
    const fetch = vi.mocked(globalThis.fetch);
    const item = kind === "keys" ? { username: "test-1", role: "read" } : { id: "1", name: "test", revoked_at: null };
    fetch.mockImplementation(async (_input, init) => response(init?.method === "POST" && JSON.parse(String(init.body)).operation === "issue" ? { new_key: "example-secret" } : { items: [item], legacy_configured: true }) as Response);
    const wrapper = mount(Credentials, { props: { kind } });
    await flushPromises();
    await wrapper.get('form input:not([type="checkbox"])').setValue("test");
    await wrapper.findAll("form")[0]!.trigger("submit");
    await flushPromises();
    expect(wrapper.text()).toContain("example-secret");
    expect(wrapper.text()).toContain("only once");
    await wrapper.findAll("form")[1]!.get('input[type="checkbox"]').setValue(true);
    await wrapper.findAll("form")[1]!.trigger("submit");
    await flushPromises();
    const call = fetch.mock.calls.find(([, init]) => init?.method === "POST" && JSON.parse(String(init.body)).operation === "revoke")!;
    expect(JSON.parse(String(call[1]?.body)).confirmed).toBe(true);
    await wrapper.findAll("button").find(b => b.text() === "Dismiss key")!.trigger("click");
    expect(wrapper.text()).not.toContain("example-secret");
    wrapper.unmount();
  });
});

describe("session", () => {
  it("loads an authenticated session and closes events on expiry", async () => {
    const fetch = vi.mocked(globalThis.fetch);
    fetch.mockImplementation(async input => response(String(input).endsWith("/session") ? { authenticated: true } : String(input).endsWith("/overview") ? snapshot : { charts: [] }) as Response);
    const wrapper = mount(App);
    await flushPromises();
    expect(MockEventSource.instance.url).toBe("/admin/events");
    expect(wrapper.text()).toContain("Fingerprint administration");
    MockEventSource.instance.send("session-expired");
    await flushPromises();
    expect(MockEventSource.instance.closed).toBe(true);
    expect(wrapper.text()).toContain("Session ended");
    expect(location.pathname).toBe("/admin/login");
    wrapper.unmount();
  });

  it("keeps login errors visible and starts events after signing in", async () => {
    const fetch = vi.mocked(globalThis.fetch);
    fetch.mockImplementation(async (input, init) => {
      if (String(input).endsWith("/session")) return response({ authenticated: false }) as Response;
      if (String(input).endsWith("/login")) {
        const accepted = JSON.parse(String(init?.body)).password === "correct"; // pragma: allowlist secret
        return response(accepted ? {} : { detail: "Invalid credentials" }, accepted ? 200 : 401) as Response;
      }
      return response(String(input).endsWith("/overview") ? snapshot : { charts: [] }) as Response;
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
    fetch.mockImplementation(async input => response(String(input).endsWith("/session") ? { authenticated: true } : String(input).endsWith("/overview") ? snapshot : String(input).endsWith("/history") ? { charts: [] } : {}) as Response);
    const wrapper = mount(App);
    await flushPromises();
    await wrapper.findAll("button").find(button => button.text() === "Sign out")!.trigger("click");
    await flushPromises();
    const call = fetch.mock.calls.find(([input]) => String(input).endsWith("/logout"))!;
    expect(JSON.parse(String(call[1]?.body))).toEqual({});
    expect(MockEventSource.instance.closed).toBe(true);
    expect(location.pathname).toBe("/admin/login");
    wrapper.unmount();
  });

  it("expires the session on a protected API 401", async () => {
    const fetch = vi.mocked(globalThis.fetch);
    fetch.mockImplementation(async input => String(input).endsWith("/session")
      ? response({ authenticated: true }) as Response
      : String(input).endsWith("/overview")
        ? response({ detail: "Expired" }, 401) as Response
        : response({ charts: [] }) as Response);
    const wrapper = mount(App);
    await flushPromises();
    expect(wrapper.text()).toContain("Session ended");
    expect(location.pathname).toBe("/admin/login");
    expect(MockEventSource.instance.closed).toBe(true);
    wrapper.unmount();
  });
});
