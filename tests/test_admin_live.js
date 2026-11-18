const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

const script = readFileSync(
  join(__dirname, "../fingerprint_server/admin/templates/admin_live.html"), "utf8"
).replace("<script>", "").replace("</script>", "");

function browser(dataset) {
  const elements = new Map();
  for (const id of ["admin-live", "live-status", "worker", "database", "processed"]) {
    elements.set(id, { dataset: {}, textContent: "", handlers: {} });
  }
  elements.get("admin-live").dataset = { dataset, revision: "initial", eventsUrl: "/admin/events" };
  if (dataset) elements.set("live-reload", {
    disabled: true, textContent: "", handlers: {},
    addEventListener(event, handler) { this.handlers[event] = handler; },
  });
  const events = {};
  const handlers = {};
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
      constructor(url) { assert.equal(url, "/admin/events"); }
      addEventListener(name, callback) { events[name] = callback; }
      close() { closed = true; }
    },
    setInterval(callback) { watchdog = callback; },
    clearInterval() {},
    model: { apiUrl: "/admin/api/submissions" },
  });
  function snapshot(revision, age = 0) {
    events.snapshot({ data: JSON.stringify({
      updated_at: new Date(Date.now() - age).toISOString(),
      revisions: { [dataset]: revision },
      overview: {
        worker_available: true, database_bytes: 1073741824,
        references: { published: 3, stored: 4 }, format_version: 1,
        submissions: { processed: 9 },
      },
    }) });
  }
  return {
    elements, events, snapshot, watchdog, handlers,
    closed: () => closed, deselected: () => deselected, reloaded: () => reloaded,
  };
}

test("dashboard updates only fresh values and exposes outages and session expiry", () => {
  const page = browser();
  page.snapshot();
  assert.equal(page.elements.get("processed").textContent, 9);
  assert.equal(page.elements.get("database").textContent, "1.00 GiB");
  assert.match(page.elements.get("live-status").textContent, /connected/);
  page.events.unavailable();
  assert.match(page.elements.get("live-status").textContent, /failed/);
  page.snapshot(undefined, 30000);
  assert.match(page.elements.get("live-status").textContent, /outdated/);
  page.events["session-expired"]();
  assert.equal(page.closed(), true);
  page.watchdog();
  assert.match(page.elements.get("live-status").textContent, /Session ended/);
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
