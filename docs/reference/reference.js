"use strict";

async function loadReference() {
  const response = await fetch("openapi.json", { credentials: "omit" });
  if (!response.ok) throw new Error(`OpenAPI request failed: HTTP ${response.status}`);
  const schema = await response.json();
  const root = document.getElementById("reference");
  function section(title, content) {
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = title;
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(content, null, 2);
    details.append(summary, pre);
    details.dataset.search = `${title} ${pre.textContent}`.toLowerCase();
    root.append(details);
  }
  for (const [path, methods] of Object.entries(schema.paths)) {
    for (const [method, operation] of Object.entries(methods)) {
      section(`${method.toUpperCase()} ${path} — ${operation.operationId}`, operation);
    }
  }
  for (const [name, definition] of Object.entries(schema.components.schemas)) {
    section(`Schema: ${name}`, definition);
  }
  document.getElementById("status").textContent =
    `OpenAPI ${schema.openapi} · API ${schema.info.version} · Expand an entry to inspect its contract.`;
  document.getElementById("filter").addEventListener("input", (event) => {
    const query = event.target.value.toLowerCase();
    for (const entry of root.children) entry.hidden = !entry.dataset.search.includes(query);
  });
}

loadReference().catch((error) => {
  document.getElementById("status").textContent = `Unable to load API reference: ${error.message}`;
});
