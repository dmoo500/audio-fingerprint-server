const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

const script = readFileSync(
  join(__dirname, "../fingerprint_server/admin/templates/admin_live.html"), "utf8"
).replace("<script>", "").replace("</script>", "");

function browser(dataset, counts) {
  const elements = new Map();
  for (const id of [
    "admin-live", "live-status", "live-status-icon", "worker", "database", "processed",
    "queue-queued", "queue-processing", "queue-pending_metadata", "manual_review",
  ]) {
    elements.set(id, {
      dataset: {}, textContent: "", handlers: {},
      setAttribute(name, value) { this[name] = value; },
    });
  }
  elements.get("admin-live").dataset = { dataset, revision: "initial", eventsUrl: "/admin/events" };
  if (counts) {
    elements.set("submission-chart", {
      dataset: { counts: JSON.stringify(counts) }, style: {},
      setAttribute(name, value) { this[name] = value; },
    });
    elements.set("submission-chart-total", { textContent: "" });
  }
  if (dataset) elements.set("live-reload", {
    disabled: true, textContent: "", handlers: {},
    addEventListener(event, handler) { this.handlers[event] = handler; },
  });
  const events = {};
  const handlers = {};
  let source;
  let closed = false;
  let watchdog;
  let deselected = false;
  let reloaded = false;
  const table = {
    column() { return { checkboxes: { deselectAll() { deselected = true; } } }; },
    ajax: { reload(callback, resetPaging) {
      assert.equal(resetPaging, false);
      reloaded = true;
      handlers["preXhr.dt"]();
      handlers["draw.dt"]();
    } },
  };
  const jquery = (value) => {
    if (typeof value === "function") return value();
    return {
      on(event, handler) { handlers[event] = handler; return this; },
      DataTable() { return table; },
      scrollTop() { return 180; },
    };
  };
  vm.runInNewContext(script, {
    $: jquery,
    document: {
      getElementById(id) { return elements.get(id); },
      querySelector(selector) {
        return elements.get(selector.match(/"([^"]+)"/)[1]);
      },
    },
    window: { addEventListener() {} },
    EventSource: class {
      constructor(url) { assert.equal(url, "/admin/events"); source = this; }
      addEventListener(name, callback) { events[name] = callback; }
      close() { closed = true; }
    },
    setInterval(callback) { watchdog = callback; },
    clearInterval() {},
    model: { apiUrl: "/admin/api/submissions" },
  });
  function snapshot(revision, age = 0, counts = { processed: 9 }, manualReview) {
    events.snapshot({ data: JSON.stringify({
      updated_at: new Date(Date.now() - age).toISOString(),
      revisions: { [dataset]: revision },
      overview: {
        worker_available: true, database_bytes: 1073741824,
        references: { published: 3, stored: 4 }, format_version: 1,
        submissions: counts, manual_review: manualReview,
      },
    }) });
  }
  return {
    elements, events, snapshot, watchdog, handlers,
    connectionError: () => source.onerror(),
    closed: () => closed, deselected: () => deselected, reloaded: () => reloaded,
  };
}

test("submission donut renders all statuses, handles empty data and follows fresh snapshots", () => {
  const page = browser(undefined, {
    processed: 70, pending_metadata: 20, failed: 5, queued: 3, processing: 2,
  });
  const chart = page.elements.get("submission-chart");
  assert.equal(page.elements.get("submission-chart-total").textContent, "100");
  assert.match(chart.style.background, /#2f9e44 0% 70%/);
  assert.match(chart.style.background, /#ae3ec9 98% 100%/);
  assert.match(chart["aria-label"], /pending_metadata: 20/);
  page.snapshot();
  assert.equal(page.elements.get("submission-chart-total").textContent, "9");
  assert.equal(chart.style.background, "conic-gradient(#2f9e44 0% 100%)");
  page.snapshot(undefined, 30000);
  assert.equal(page.elements.get("submission-chart-total").textContent, "9");
  const empty = browser(undefined, { processed: 0 });
  assert.equal(empty.elements.get("submission-chart")["aria-label"], "No submissions yet");
  assert.equal(empty.elements.get("submission-chart").style.background, "#868e96");
});

test("dashboard updates only fresh values and exposes outages and session expiry", () => {
  const page = browser();
  page.snapshot();
  assert.equal(page.elements.get("processed").textContent, 9);
  assert.equal(page.elements.get("database").textContent, "1.00 GiB");
  assert.match(page.elements.get("live-status").textContent, /connected/);
  assert.equal(page.elements.get("live-status-icon").dataset.state, "connected");
  page.events.unavailable();
  assert.match(page.elements.get("live-status").textContent, /failed/);
  assert.equal(page.elements.get("live-status-icon").dataset.state, "error");
  page.snapshot(undefined, 30000);
  assert.match(page.elements.get("live-status").textContent, /outdated/);
  page.events["session-expired"]();
  assert.equal(page.closed(), true);
  page.watchdog();
  assert.match(page.elements.get("live-status").textContent, /Session ended/);
});

test("queue cards follow the authenticated snapshot and do not display stale values as fresh", () => {
  const page = browser();
  page.snapshot(undefined, 0, { queued: 8, processing: 2, pending_metadata: 4 }, 3);
  assert.equal(page.elements.get("queue-queued").textContent, 8);
  assert.equal(page.elements.get("queue-processing").textContent, 2);
  assert.equal(page.elements.get("queue-pending_metadata").textContent, 4);
  assert.equal(page.elements.get("manual_review").textContent, 3);
  page.snapshot(undefined, 30000, { queued: 0 }, 0);
  assert.equal(page.elements.get("queue-queued").textContent, 8);
  assert.equal(page.elements.get("manual_review").textContent, 3);
});

test("reconnecting is yellow and fresh data restores green with accessible details", () => {
  const page = browser();
  page.watchdog();
  assert.equal(page.elements.get("live-status").textContent, "");
  page.connectionError();
  const icon = page.elements.get("live-status-icon");
  assert.equal(icon.dataset.state, "connecting");
  assert.match(icon["aria-label"], /Reconnecting/);
  page.snapshot();
  assert.equal(icon.dataset.state, "connected");
  page.events["session-expired"]();
  page.connectionError();
  assert.equal(icon.dataset.state, "error");
  assert.match(icon["aria-label"], /Session ended/);
});

test("table updates enable refresh without touching selection or reloading rows", () => {
  const page = browser("submissions");
  const button = page.elements.get("live-reload");
  page.snapshot("initial");
  assert.equal(button.disabled, true);
  page.snapshot("changed");
  assert.equal(button.disabled, false);
  assert.equal(page.reloaded(), false);
  assert.equal(page.deselected(), false);
  button.handlers.click();
  assert.equal(page.reloaded(), true);
  assert.equal(page.deselected(), true);
  assert.equal(button.disabled, true);
  page.snapshot("changed-again");
  assert.equal(button.disabled, false);
});

test("an update arriving during a table request is not cleared by its response", () => {
  const page = browser("submissions");
  page.handlers["preXhr.dt"]();
  page.snapshot("arrived-during-request");
  page.handlers["draw.dt"]();
  assert.equal(page.elements.get("live-reload").disabled, false);
});

test("fresh count events do not hide table reload failures", () => {
  const page = browser("submissions");
  page.handlers["ajaxError.adminLive"](null, null, { url: "/admin/api/submissions" });
  page.snapshot("initial");
  assert.match(page.elements.get("live-status").textContent, /Table refresh failed/);
  assert.equal(page.elements.get("live-reload").disabled, false);
});
