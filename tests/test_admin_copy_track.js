const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

const script = readFileSync(
  join(__dirname, "../fingerprint_server/admin/templates/admin_copy_track.html"), "utf8"
).replace("<script>", "").replace("</script>", "");

test("copy uses the selected track and offers manual selection if copying fails", async () => {
  const tracks = ["Song - Artist", "Other title - Other artist"].map((value) => {
    let click;
    const input = {
      value, focused: false, selected: false,
      focus() { this.focused = true; },
      select() { this.selected = true; },
    };
    const button = { disabled: false, addEventListener(_event, fn) { click = fn; } };
    const status = { textContent: "" };
    return {
      input, button, status, click: () => click(),
      querySelector: (selector) => selector === "input" ? input
        : selector === "button" ? button : status,
    };
  });
  const copied = [];
  let reject = false;
  vm.runInNewContext(script, {
    document: { querySelectorAll: () => tracks },
    navigator: { clipboard: { async writeText(value) {
      if (reject) throw new Error("Permission denied");
      copied.push(value);
    } } },
  });
  await tracks[1].click();
  assert.deepEqual(copied, ["Other title - Other artist"]);
  assert.equal(tracks[1].status.textContent, "Copied.");
  assert.equal(tracks[0].status.textContent, "");
  reject = true;
  await tracks[0].click();
  assert.equal(tracks[0].input.focused, true);
  assert.equal(tracks[0].input.selected, true);
  assert.equal(tracks[0].input.hidden, false);
  assert.match(tracks[0].status.textContent, /Press Ctrl\+C or Command\+C/);
  assert.equal(tracks[0].button.disabled, false);
});

test("manual copy works when clipboard access is unavailable", async () => {
  let click;
  const input = { value: "Title", focus() {}, select() { this.selected = true; } };
  const button = { addEventListener(_event, fn) { click = fn; } };
  const status = {};
  const group = {
    querySelector: (selector) => selector === "input" ? input
      : selector === "button" ? button : status,
  };
  vm.runInNewContext(script, {
    document: { querySelectorAll: () => [group] },
    navigator: {},
  });
  await click();
  assert.equal(input.selected, true);
  assert.match(status.textContent, /Could not copy automatically/);
  assert.equal(button.disabled, false);
});
