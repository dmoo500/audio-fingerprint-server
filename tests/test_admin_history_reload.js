const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

const script = readFileSync(
  join(__dirname, "../fingerprint_server/admin/templates/admin_history_reload.html"), "utf8"
).replace("<script>", "").replace("</script>", "");

test("refresh updates only the charts and preserves them on failure or session expiry", async () => {
  let click;
  const button = {
    disabled: false,
    addEventListener(event, callback) {
      assert.equal(event, "click");
      click = callback;
    },
  };
  const charts = { innerHTML: "old chart" };
  const status = { textContent: "" };
  const nodes = { "history-reload": button, "history-charts": charts, "history-status": status };
  let result = { ok: true, text: async () => "new chart" };
  vm.runInNewContext(script, {
    $: (callback) => callback(),
    document: { getElementById: (id) => nodes[id] },
    fetch: async (url) => {
      assert.equal(url, "?view=history");
      assert.equal(button.disabled, true);
      return result;
    },
  });
  await click();
  assert.equal(charts.innerHTML, "new chart");
  assert.equal(status.textContent, "Charts refreshed.");
  result = { ok: false };
  await click();
  assert.equal(charts.innerHTML, "new chart");
  assert.match(status.textContent, /Could not refresh/);
  result = { ok: true, redirected: true };
  await click();
  assert.equal(charts.innerHTML, "new chart");
  assert.match(status.textContent, /Sign in again/);
  assert.equal(button.disabled, false);
});
