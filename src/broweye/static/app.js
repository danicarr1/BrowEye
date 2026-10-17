"use strict";
// BrowEye UI. Plain JS, no build step. All server data is inserted with
// textContent (never innerHTML): captured traffic is untrusted.

const $ = (sel, root = document) => root.querySelector(sel);
const MAX_ROWS = 5000;

const state = {
  token: "", session: "", lastSeq: 0, selectedFlow: null, draft: null,
  execution: null, fetching: false, pending: false,
};

// ---- helpers --------------------------------------------------------------
function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    e.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return e;
}

// replaceChildren() does not flatten arrays; this does, and skips empty slots.
function fill(node, ...children) {
  node.replaceChildren(...children.flat(Infinity).filter(c => c !== null && c !== undefined && c !== false)
    .map(c => (c instanceof Node ? c : document.createTextNode(String(c)))));
}

function showError(msg) {
  const s = $("#status");
  s.textContent = msg || "";
  if (msg) setTimeout(() => { if (s.textContent === msg) s.textContent = ""; }, 8000);
}

async function api(path, { method = "GET", body } = {}) {
  const opts = { method, headers: { "X-BrowEye-Token": state.token } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

function guard(fn) {
  return async (...args) => {
    try { return await fn(...args); } catch (e) { showError(e.message); }
  };
}

function statusClass(s) { return s ? `s${String(s)[0]}` : "err"; }
function fmtSize(n) {
  if (n === null || n === undefined) return "";
  return n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`;
}
function b64ToBytes(b64) { return Uint8Array.from(atob(b64), c => c.charCodeAt(0)); }
function hexdump(bytes, max = 4096) {
  const lines = [];
  for (let i = 0; i < Math.min(bytes.length, max); i += 16) {
    const chunk = Array.from(bytes.slice(i, i + 16));
    const hex = chunk.map(b => b.toString(16).padStart(2, "0")).join(" ");
    const asc = chunk.map(b => (b >= 32 && b < 127 ? String.fromCharCode(b) : ".")).join("");
    lines.push(`${i.toString(16).padStart(8, "0")}  ${hex.padEnd(48)}  ${asc}`);
  }
  if (bytes.length > max) lines.push(`… ${bytes.length - max} more bytes`);
  return lines.join("\n");
}
function header(headers, name) {
  const h = (headers || []).find(([k]) => k.toLowerCase() === name.toLowerCase());
  return h ? h[1] : null;
}

// ---- rendering of messages ------------------------------------------------
function bodyView(msg) {
  if (msg.body_b64 === null || msg.body_b64 === undefined) {
    return el("p", { class: "muted" }, msg.body_note || "(no body)");
  }
  if (msg.body_text === null) return el("pre", {}, hexdump(b64ToBytes(msg.body_b64)));
  let text = msg.body_text;
  const ct = header(msg.headers, "content-type") || "";
  if (ct.includes("json") || /^\s*[\[{]/.test(text)) {
    try { text = JSON.stringify(JSON.parse(text), null, 2); } catch { /* not JSON, show raw */ }
  } else if (ct.includes("x-www-form-urlencoded")) {
    try { text = [...new URLSearchParams(text)].map(([k, v]) => `${k} = ${v}`).join("\n"); } catch { /* raw */ }
  }
  return el("pre", {}, text.length > 500000 ? text.slice(0, 500000) + "\n… (truncated in view)" : text);
}

function headersTable(headers) {
  if (!headers || !headers.length) return el("p", { class: "muted" }, "(no headers)");
  return el("table", { class: "kv" }, el("tbody", {}, headers.map(([k, v]) => el("tr", {}, el("td", {}, k), el("td", {}, v)))));
}

function queryTable(url) {
  let params;
  try { params = [...new URL(url).searchParams]; } catch { return null; }
  if (!params.length) return null;
  return [el("h3", {}, "Query"), headersTable(params)];
}

function requestView(req, note) {
  return [
    el("pre", {}, `${req.method} ${req.url}`),
    queryTable(req.url),
    el("h3", {}, "Request headers"), headersTable(req.headers),
    el("h3", {}, "Request body"), note ? el("p", { class: "note" }, note) : null, bodyView(req),
  ];
}

function responseView(resp) {
  if (!resp) return el("p", { class: "err" }, "No response.");
  return [
    el("h3", {}, "Response"),
    el("p", {}, el("strong", { class: statusClass(resp.status) }, `${resp.status} ${resp.reason || ""}`),
      resp.http_version ? el("span", { class: "muted" }, `  ${resp.http_version}`) : null),
    headersTable(resp.headers),
    el("h3", {}, "Response body"), bodyView(resp),
  ];
}

// ---- tabs -----------------------------------------------------------------
function showTab(name) {
  document.querySelectorAll("#tabs button").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.id === `tab-${name}`));
  if (name === "sitemap") loadSitemap();
  if (name === "repeater") loadDrafts();
}

// ---- sessions -------------------------------------------------------------
async function loadState() {
  const st = await api("/api/state");
  const sel = $("#session");
  fill(sel, ...st.sessions.filter(s => s.live).map(s => el("option", { value: s.id }, s.name)));
  if (!st.browser) fill(sel, el("option", { value: "" }, "(browser disabled)"));
  if (state.session && [...sel.options].some(o => o.value === state.session)) sel.value = state.session;
  state.session = sel.value;
  return st;
}

const newSession = guard(async () => {
  const name = prompt("Session name (identity), e.g. admin, user-a:", "session");
  if (!name) return;
  const reuse = confirm("Reuse saved cookies/storage for this name if available?");
  const s = await api("/api/sessions", { method: "POST", body: { name, reuse_state: reuse } });
  state.session = s.id;
  await loadState();
});

const navigate = guard(async ev => {
  ev.preventDefault();
  if (!state.session) throw new Error("Open a browser session first (+ Session).");
  let url = $("#nav-url").value.trim();
  if (url && !/^https?:\/\//i.test(url)) url = "http://" + url;
  const r = await api(`/api/sessions/${state.session}/navigate`, { method: "POST", body: { url } });
  showError("");
  return r;
});

// ---- traffic --------------------------------------------------------------
function filterParams() {
  const f = new FormData($("#filters"));
  const p = new URLSearchParams();
  for (const k of ["text", "method", "status", "host", "body"]) if (f.get(k)) p.set(k, f.get(k));
  if (f.get("mine") && state.session) p.set("session_id", state.session);
  return p;
}

function flowRow(r) {
  const tr = el("tr", { "data-id": r.id, onclick: () => selectFlow(r.id, tr) },
    el("td", { class: "muted" }, r.seq), el("td", {}, r.method), el("td", {}, r.host),
    el("td", { title: r.url }, r.path + (r.url.includes("?") ? "?" + r.url.split("?")[1] : "")),
    el("td", { class: statusClass(r.status) }, r.status ?? (r.error ? "ERR" : "")),
    el("td", { class: "muted" }, r.mime || r.resource_type || ""),
    el("td", {}, fmtSize(r.resp_size)), el("td", {}, r.duration_ms ? Math.round(r.duration_ms) : ""));
  return tr;
}

async function fetchFlows(reset = false) {
  if (state.fetching) { state.pending = true; return; }
  state.fetching = true;
  try {
    const tbody = $("#flows tbody");
    if (reset) { state.lastSeq = 0; fill(tbody); }
    for (;;) {
      const p = filterParams();
      p.set("after_seq", state.lastSeq); p.set("limit", 1000);
      const rows = await api(`/api/flows?${p}`);
      if (!rows.length) break;
      const frag = document.createDocumentFragment();
      rows.forEach(r => frag.append(flowRow(r)));
      tbody.append(frag);
      state.lastSeq = rows[rows.length - 1].seq;
      while (tbody.rows.length > MAX_ROWS) tbody.deleteRow(0);
      if (rows.length < 1000) break;
    }
    $("#count").textContent = `${tbody.rows.length} shown`;
    const har = new URLSearchParams({ token: state.token });
    if (state.session && $("#filters").mine.checked) har.set("session_id", state.session);
    $("#har").href = `/api/export/har?${har}`;
  } catch (e) { showError(e.message); }
  finally {
    state.fetching = false;
    if (state.pending) { state.pending = false; fetchFlows(); }
  }
}

const selectFlow = guard(async (id, tr) => {
  document.querySelectorAll("#flows tr.sel").forEach(r => r.classList.remove("sel"));
  tr?.classList.add("sel");
  const f = await api(`/api/flows/${id}`);
  state.selectedFlow = f;
  const box = $("#flow-detail");
  fill(box, 
    el("div", { class: "row", style: "padding:0 0 8px" },
      el("button", { class: "primary", onclick: () => sendToRepeater(f.id) }, "Send to Repeater"),
      el("button", { onclick: () => copy(f.curl) }, "Copy cURL"),
      el("button", { onclick: () => setCompare("a", `flow:${f.id}`) }, "Compare as A"),
      el("button", { onclick: () => setCompare("b", `flow:${f.id}`) }, "as B"),
      el("span", { class: "badge" }, `flow:${f.id}`),
      f.resource_type ? el("span", { class: "badge" }, f.resource_type) : null),
    f.error ? el("p", { class: "err" }, `Failed: ${f.error}`) : null,
    requestView(f.request, f.request_note),
    responseView(f.response),
    f.findings.length ? [el("h3", {}, "Passive findings"),
      el("ul", { class: "findings" }, f.findings.map(x => el("li", { class: `sev-${x.severity}` }, `[${x.severity}] ${x.detail}`)))] : null,
  );
});

async function copy(text) {
  try { await navigator.clipboard.writeText(text); }
  catch { prompt("Copy:", text); }
}

// ---- repeater -------------------------------------------------------------
const sendToRepeater = guard(async flowId => {
  const d = await api("/api/drafts", { method: "POST", body: { from_flow: flowId } });
  showTab("repeater");
  await loadDrafts();
  openDraft(d.id);
});

async function loadDrafts() {
  try {
    const list = await api("/api/drafts");
    fill($("#draft-list"), ...list.map(d =>
      el("li", { class: state.draft?.id === d.id ? "sel" : "", title: d.url, onclick: () => openDraft(d.id) },
        el("strong", {}, d.method), " ", d.name)));
  } catch (e) { showError(e.message); }
}

const newDraft = guard(async () => {
  const d = await api("/api/drafts", { method: "POST", body: { fields: { method: "GET", url: "http://127.0.0.1:8081/echo" } } });
  await loadDrafts(); openDraft(d.id);
});

const importCurl = guard(async () => {
  const curl = prompt("Paste a curl command:");
  if (!curl) return;
  const d = await api("/api/drafts", { method: "POST", body: { curl } });
  await loadDrafts(); openDraft(d.id);
});

const openDraft = guard(async id => {
  const d = await api(`/api/drafts/${id}`);
  state.draft = d;
  renderEditor(d);
  await loadDrafts();
  const execs = await api(`/api/executions?draft_id=${id}`);
  if (execs.length) showExecution(execs[0].id); else fill($("#result"), execTable([]));
});

function renderEditor(d) {
  const binary = d.body_b64 && d.body_text === null;
  const sessions = [...$("#session").options].map(o => [o.value, o.textContent]);
  const form = el("form", { id: "draft-form", onsubmit: e => { e.preventDefault(); sendDraft(); } },
    el("div", { class: "line" },
      el("input", { name: "name", value: d.name, title: "Name" }),
      d.source_flow_id ? el("span", { class: "badge", title: "Original capture is kept unchanged" }, `from flow:${d.source_flow_id}`) : el("span", { class: "badge" }, "new request")),
    el("div", { class: "line" },
      el("input", { name: "method", value: d.method, size: 8, list: "methods" }),
      el("datalist", { id: "methods" }, ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"].map(m => el("option", { value: m }))),
      el("input", { name: "url", value: d.url, placeholder: "https://…" })),
    el("h3", {}, "Headers — one per line, Name: value (order and duplicates kept)"),
    el("textarea", { name: "headers", rows: 8, spellcheck: "false" }, d.headers.map(([k, v]) => `${k}: ${v}`).join("\n")),
    el("h3", {}, "Body"),
    el("div", { class: "line" },
      el("select", { name: "body_mode" }, el("option", { value: "text", selected: !binary }, "text"), el("option", { value: "base64", selected: binary }, "base64 (binary)")),
      el("button", { type: "button", onclick: formatJson }, "Format JSON"),
      el("input", { type: "file", onchange: loadBodyFile, title: "Load body from a file" }),
      el("span", { class: "muted" }, "Content-Length is always recomputed.")),
    el("textarea", { name: "body", rows: 10, spellcheck: "false" }, binary ? d.body_b64 : (d.body_text ?? "")),
    el("h3", {}, "Options"),
    el("div", { class: "opts" },
      el("label", {}, "Cookies",
        el("select", { name: "cookie_mode" },
          [["as_is", "as written in headers"], ["session", "from browser session jar"], ["none", "strip all cookies"]]
            .map(([v, t]) => el("option", { value: v, selected: d.cookie_mode === v }, t)))),
      el("label", {}, "Browser session",
        el("select", { name: "session_id" }, el("option", { value: "" }, "(none)"),
          sessions.map(([v, t]) => el("option", { value: v, selected: d.session_id === v }, t)))),
      el("label", {}, "Timeout (s)", el("input", { name: "timeout_s", type: "number", min: 1, step: "any", value: d.timeout_s })),
      el("label", {}, "Max redirects", el("input", { name: "max_redirects", type: "number", min: 0, value: d.max_redirects })),
      el("label", { class: "inline" }, el("input", { type: "checkbox", name: "follow_redirects", checked: d.follow_redirects }), "Follow redirects"),
      el("label", { class: "inline" }, el("input", { type: "checkbox", name: "verify_tls", checked: d.verify_tls }), "Verify TLS"),
      el("label", {}, "Auth",
        el("select", { name: "auth_type" }, [["", "none"], ["basic", "Basic"], ["bearer", "Bearer"]]
          .map(([v, t]) => el("option", { value: v, selected: (d.auth_type || "") === v }, t)))),
      el("label", {}, "Auth user", el("input", { name: "auth_user", value: d.auth_user, autocomplete: "off" })),
      el("label", {}, "Auth secret (memory only)", el("input", { name: "auth_secret", type: "password", value: d.auth_secret, autocomplete: "off" }))),
    el("p", { class: "muted" }, "Configured auth and Cookie/Authorization headers are only sent to the original origin; cross-origin redirects drop them."),
    el("div", { class: "line" },
      el("button", { class: "primary" }, "Send"),
      el("button", { type: "button", onclick: () => saveDraft().then(() => showError("")) }, "Save"),
      el("button", { type: "button", onclick: () => copy(curlFromForm()) }, "Copy as cURL"),
      el("button", { type: "button", onclick: deleteDraft }, "Delete")),
  );
  fill($("#editor"), form);
}

function parseHeaders(text) {
  const out = [];
  for (const line of text.split("\n")) {
    if (!line.trim()) continue;
    const i = line.indexOf(":");
    if (i <= 0) throw new Error(`Invalid header line: "${line}"`);
    out.push([line.slice(0, i).trim(), line.slice(i + 1).trim()]);
  }
  return out;
}

function formFields() {
  const f = $("#draft-form");
  const fields = {
    name: f.name.value, method: f.method.value, url: f.url.value.trim(),
    headers: parseHeaders(f.headers.value), cookie_mode: f.cookie_mode.value,
    session_id: f.session_id.value || null, timeout_s: Number(f.timeout_s.value) || 30,
    max_redirects: Number(f.max_redirects.value) || 0, follow_redirects: f.follow_redirects.checked,
    verify_tls: f.verify_tls.checked, auth_type: f.auth_type.value || null,
    auth_user: f.auth_user.value, auth_secret: f.auth_secret.value,
  };
  if (f.body_mode.value === "base64") fields.body_b64 = f.body.value.replace(/\s+/g, "") || null;
  else fields.body_text = f.body.value;
  return fields;
}

function curlFromForm() {
  const q = s => `'${String(s).replace(/'/g, `'\\''`)}'`;
  const f = formFields();
  const parts = ["curl", "-X", q(f.method), q(f.url)];
  f.headers.filter(([k]) => !["content-length", "host"].includes(k.toLowerCase())).forEach(([k, v]) => parts.push("-H", q(`${k}: ${v}`)));
  if (f.body_text) parts.push("--data-raw", q(f.body_text));
  return parts.join(" ");
}

async function saveDraft() {
  const d = await api(`/api/drafts/${state.draft.id}`, { method: "PUT", body: formFields() });
  state.draft = d;
  loadDrafts();
  return d;
}

const sendDraft = guard(async () => {
  await saveDraft();
  const btn = $("#draft-form button.primary");
  btn.disabled = true; btn.textContent = "Sending…";
  try {
    const ex = await api(`/api/drafts/${state.draft.id}/send`, { method: "POST" });
    await showExecution(ex.id, ex);
  } finally { btn.disabled = false; btn.textContent = "Send"; }
});

const deleteDraft = guard(async () => {
  if (!confirm("Delete this draft? Its execution history stays in the project.")) return;
  await api(`/api/drafts/${state.draft.id}`, { method: "DELETE" });
  state.draft = null;
  fill($("#editor"), el("p", { class: "muted" }, "Draft deleted."));
  fill($("#result"), );
  loadDrafts();
});

function formatJson() {
  const t = $("#draft-form").body;
  try { t.value = JSON.stringify(JSON.parse(t.value), null, 2); } catch (e) { showError(`Not valid JSON: ${e.message}`); }
}

function loadBodyFile(ev) {
  const file = ev.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    const f = $("#draft-form");
    f.body_mode.value = "base64";
    f.body.value = String(reader.result).split(",")[1] || "";
  };
  reader.readAsDataURL(file);
}

function execTable(execs) {
  return [el("h3", {}, "Executions"),
    execs.length ? el("table", { class: "execs" }, el("tbody", {}, execs.map(x =>
      el("tr", { class: state.execution?.id === x.id ? "sel" : "", onclick: () => showExecution(x.id) },
        el("td", { class: "muted" }, new Date(x.started_at * 1000).toLocaleTimeString()),
        el("td", { class: statusClass(x.status) }, x.status ?? "ERR"),
        el("td", {}, `${Math.round(x.duration_ms)} ms`),
        el("td", {}, el("button", { onclick: e => { e.stopPropagation(); setCompare("a", `exec:${x.id}`); } }, "A"),
          el("button", { onclick: e => { e.stopPropagation(); setCompare("b", `exec:${x.id}`); } }, "B")))))) :
      el("p", { class: "muted" }, "Not sent yet.")];
}

const showExecution = guard(async (id, ex) => {
  ex = ex || await api(`/api/executions/${id}`);
  state.execution = ex;
  const execs = await api(`/api/executions?draft_id=${ex.draft_id}`);
  const hops = ex.hops.map((h, i) => el("details", { open: i === ex.hops.length - 1 },
    el("summary", {}, `Hop ${i + 1}: ${h.request.method} ${h.request.url} → `,
      el("span", { class: statusClass(h.response?.status) }, h.response ? h.response.status : "no response"),
      ` (${Math.round(h.duration_ms)} ms)`),
    h.note ? el("p", { class: "note" }, h.note) : null,
    el("h3", {}, "Request actually sent"), requestView(h.request),
    responseView(h.response)));
  fill($("#result"), 
    el("div", { class: "row", style: "padding:0 0 8px" },
      el("span", { class: "badge" }, `exec:${ex.id}`),
      ex.draft.source_flow_id ? el("button", { onclick: () => { setCompare("a", `flow:${ex.draft.source_flow_id}`); setCompare("b", `exec:${ex.id}`); runCompare(); } }, "Compare with original") : null,
      el("button", { onclick: () => copy(ex.curl || "") }, "Copy sent as cURL")),
    ex.error ? el("p", { class: "err" }, ex.error) : null,
    hops, execTable(execs));
});

// ---- compare --------------------------------------------------------------
function setCompare(which, ref) {
  $(`#cmp-${which}`).value = ref;
}

const runCompare = guard(async () => {
  const a = $("#cmp-a").value.trim(), b = $("#cmp-b").value.trim();
  if (!a || !b) throw new Error("Set both A and B (use the A/B buttons in Traffic or Repeater).");
  const c = await api(`/api/compare?${new URLSearchParams({ a, b })}`);
  showTab("compare");
  const out = $("#cmp-out");
  if (c.error) { fill(out, el("p", { class: "err" }, c.error)); return; }
  const showVol = $("#cmp-volatile").checked;
  const hdrs = c.headers.changes.filter(h => showVol || !h.volatile);
  const body = c.body;
  let bodyNode;
  if (body.kind === "identical") bodyNode = el("p", {}, "Bodies are byte-identical.");
  else if (body.kind === "json") bodyNode = body.changes.length
    ? el("table", { class: "kv" }, el("tbody", {}, body.changes.map(ch => el("tr", {},
        el("td", {}, ch.path), el("td", {}, ch.kind),
        el("td", { class: "diff-del" }, ch.a === undefined ? "" : JSON.stringify(ch.a)),
        el("td", { class: "diff-add" }, ch.b === undefined ? "" : JSON.stringify(ch.b))))))
    : el("p", {}, "JSON is semantically equal (only formatting / key order differ).");
  else if (body.kind === "text") bodyNode = el("pre", {}, body.diff.map(l =>
    el("div", { class: "diffline " + (l.startsWith("+") ? "diff-add" : l.startsWith("-") ? "diff-del" : "") }, l)));
  else bodyNode = el("p", {}, "Binary bodies differ.");
  fill(out, 
    el("h3", {}, "Summary"),
    el("p", {}, `Status: `, el("strong", { class: statusClass(c.status.a) }, c.status.a), " → ",
      el("strong", { class: statusClass(c.status.b) }, c.status.b), c.status.changed ? " (changed)" : " (same)",
      `   ·   size ${fmtSize(body.size_a)} → ${fmtSize(body.size_b)}`,
      c.timing_ms.a != null ? `   ·   ${Math.round(c.timing_ms.a)} ms → ${Math.round(c.timing_ms.b)} ms` : ""),
    el("h3", {}, `Headers (${hdrs.length} differences${showVol ? "" : ", volatile hidden"})`),
    hdrs.length ? el("table", { class: "kv" }, el("tbody", {}, hdrs.map(h => el("tr", {},
      el("td", {}, h.name), el("td", {}, h.kind),
      el("td", { class: "diff-del" }, (h.a || []).join("\n")), el("td", { class: "diff-add" }, (h.b || []).join("\n")))))) : el("p", { class: "muted" }, "No differences."),
    el("h3", {}, `Body (${body.kind})`), bodyNode,
    body.truncated ? el("p", { class: "note" }, "Diff truncated.") : null);
});

// ---- site map -------------------------------------------------------------
const loadSitemap = guard(async () => {
  const p = new URLSearchParams();
  if (state.session && $("#filters").mine.checked) p.set("session_id", state.session);
  const hosts = await api(`/api/sitemap?${p}`);
  fill($("#sitemap"), ...(hosts.length ? hosts.map(h => el("details", { class: "host", open: true },
    el("summary", {}, `${h.host} (${h.endpoints.length})`),
    h.endpoints.map(e => el("div", { class: "ep", onclick: () => filterTo(h.host, e.path) },
      el("span", { class: statusClass(e.status) }, String(e.status ?? "—").padEnd(4)), ` ${e.method.padEnd(7)} ${e.path}  `,
      el("span", { class: "muted" }, `×${e.count}`))))) : [el("p", { class: "muted" }, "Nothing captured yet.")]));
});

function filterTo(host, path) {
  const f = $("#filters");
  f.host.value = host; f.text.value = path;
  showTab("traffic");
  fetchFlows(true);
}

// ---- boot -----------------------------------------------------------------
function initToken() {
  const m = location.hash.match(/token=([^&]+)/);
  if (m) {
    state.token = decodeURIComponent(m[1]);
    try { sessionStorage.setItem("broweye-token", state.token); } catch { /* storage may be blocked */ }
    history.replaceState(null, "", location.pathname);
  } else {
    try { state.token = sessionStorage.getItem("broweye-token") || ""; } catch { state.token = ""; }
  }
}

function listen() {
  const es = new EventSource(`/api/events?token=${encodeURIComponent(state.token)}`);
  es.onmessage = ev => {
    const batch = JSON.parse(ev.data);
    if (batch.some(e => e.type === "flow")) fetchFlows();
  };
  es.onerror = () => { /* EventSource reconnects by itself */ };
}

async function boot() {
  initToken();
  document.querySelectorAll("#tabs button").forEach(b => b.addEventListener("click", () => showTab(b.dataset.tab)));
  $("#new-session").addEventListener("click", newSession);
  $("#nav").addEventListener("submit", navigate);
  $("#session").addEventListener("change", e => { state.session = e.target.value; fetchFlows(true); });
  $("#filters").addEventListener("submit", e => { e.preventDefault(); fetchFlows(true); });
  $("#draft-new").addEventListener("click", newDraft);
  $("#draft-curl").addEventListener("click", importCurl);
  $("#cmp-go").addEventListener("click", runCompare);
  $("#cmp-volatile").addEventListener("change", () => { if ($("#cmp-out").childElementCount) runCompare(); });
  try { await loadState(); } catch (e) {
    showError(`${e.message}. Open the URL printed by 'broweye' (it contains the access token).`);
    return;
  }
  await fetchFlows(true);
  listen();
}

boot();
