const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

test("search handles pasted input, clearing and Enter without duplicate keyup requests", () => {
  const handlers = {};
  const calls = [];
  let pending;
  let current = "";
  let keyupRemoved = false;
  const table = {
    page: { info() { return { recordsDisplay: 2 }; } },
    search(value) {
      if (value === undefined) return current;
      current = value;
      calls.push(value);
      return this;
    },
    draw() {},
  };
  const input = {
    attr(name, value) { assert.equal(name, "maxlength"); assert.equal(value, "200"); },
    off(name) { keyupRemoved = name === "keyup"; return this; },
    on(name, callback) { handlers[name] = callback; return this; },
  };
  const status = { textContent: "", className: "" };
  const root = { setAttribute() {} };
  const document = { getElementById(id) { return id === "dt" ? root : status; } };
  const script = readFileSync(
    join(__dirname, "../fingerprint_server/admin/templates/admin_table.html"), "utf8"
  ).replace("<script>", "").replace("</script>", "");
  vm.runInNewContext(script, {
    $(value) {
      if (typeof value === "function") return value();
      return value === "#dt" ? {
        DataTable() { return table; },
        on(name, callback) { handlers[name] = callback; return this; },
      } : value === document ? {
        on(name, callback) { handlers[name] = callback; return this; },
      } : input;
    },
    document,
    model: { apiUrl: "/admin/api/submissions" },
    setTimeout(callback, delay) { assert.equal(delay, 350); pending = callback; },
    clearTimeout() { pending = undefined; },
    window: { addEventListener() {} },
  });
  assert.equal(keyupRemoved, true);
  handlers.input.call({ value: "ACDC" });
  assert.equal(status.textContent, "Preparing search...");
  assert.deepEqual(calls, []);
  pending();
  assert.deepEqual(calls, ["ACDC"]);
  handlers["preXhr.dt"]();
  assert.match(status.textContent, /Searching all records/);
  handlers["draw.dt"]();
  assert.equal(status.textContent, "2 matching records.");
  handlers["ajaxError.adminTable"](null, null, { url: "/admin/api/submissions" });
  assert.match(status.textContent, /request failed/);
  handlers.input.call({ value: "" });
  pending();
  assert.deepEqual(calls, ["ACDC", ""]);
  handlers.input.call({ value: "Artist" });
  handlers.keydown.call({ value: "Artist" }, { key: "Enter", preventDefault() {} });
  assert.equal(pending, undefined);
  assert.deepEqual(calls, ["ACDC", "", "Artist"]);
});
