"use strict";

// Read-only triage view: load tracker rows from /api/jobs, render a
// sortable/filterable table, click a row to view its rendered report.

let JOBS = [];
let sortKey = "score_value";
let sortDir = -1; // -1 = descending (highest score first by default)
let selectedNum = null;
let view = "table"; // "table" | "board"
let pending = 0;

// Canonical kanban columns — mirror of data.CANONICAL_STATES.
const STATES = ["Evaluated", "Applied", "Responded", "Interview", "Offer", "Rejected", "Discarded", "SKIP"];

const els = {
  body: document.getElementById("jobs-body"),
  filter: document.getElementById("filter"),
  statusFilter: document.getElementById("status-filter"),
  count: document.getElementById("count"),
  empty: document.getElementById("empty"),
  tablePane: document.getElementById("table-pane"),
  boardPane: document.getElementById("board-pane"),
  reportPane: document.getElementById("report-pane"),
  reportBody: document.getElementById("report-body"),
  reportClose: document.getElementById("report-close"),
  reportLink: document.getElementById("report-link"),
  viewTable: document.getElementById("view-table"),
  viewBoard: document.getElementById("view-board"),
  pushBtn: document.getElementById("push-btn"),
};

async function loadJobs() {
  const resp = await fetch("/api/jobs");
  const payload = await resp.json();
  JOBS = payload.rows || [];
  pending = payload.pending || 0;
  showSourceBanner(payload.source);
  populateStatusFilter();
  updatePushButton();
  render();
}

// When showing raw tracker-additions (the merge into applications.md didn't
// run), tell the user — statuses won't reflect any edits they've made.
function showSourceBanner(source) {
  const existing = document.getElementById("source-banner");
  if (existing) existing.remove();
  if (source !== "tracker-additions") return;
  const banner = document.createElement("div");
  banner.id = "source-banner";
  banner.className = "banner";
  banner.textContent =
    "Showing raw evaluation output (tracker-additions). applications.md " +
    "wasn't found, so status edits aren't reflected — the merge step may not " +
    "have run.";
  document.querySelector("header").appendChild(banner);
}

function populateStatusFilter() {
  const statuses = [...new Set(JOBS.map((j) => j.status).filter(Boolean))].sort();
  for (const s of statuses) {
    const opt = document.createElement("option");
    opt.value = s;
    opt.textContent = s;
    els.statusFilter.appendChild(opt);
  }
}

function scoreClass(v) {
  if (v == null) return "score-none";
  if (v >= 4) return "score-good";
  if (v >= 3) return "score-mid";
  return "score-low";
}

function visibleRows() {
  const q = els.filter.value.trim().toLowerCase();
  const status = els.statusFilter.value;
  let rows = JOBS.filter((j) => {
    if (status && j.status !== status) return false;
    if (!q) return true;
    return [j.company, j.role, j.notes].some(
      (f) => (f || "").toLowerCase().includes(q)
    );
  });
  rows.sort((a, b) => {
    let av = a[sortKey], bv = b[sortKey];
    // Nulls always sort to the bottom regardless of direction.
    if (av == null && bv == null) return 0;
    if (av == null) return 1;
    if (bv == null) return -1;
    if (typeof av === "string") { av = av.toLowerCase(); bv = bv.toLowerCase(); }
    if (av < bv) return -sortDir;
    if (av > bv) return sortDir;
    return 0;
  });
  return rows;
}

function render() {
  els.tablePane.hidden = view !== "table";
  els.boardPane.hidden = view !== "board";
  els.viewTable.classList.toggle("active", view === "table");
  els.viewBoard.classList.toggle("active", view === "board");
  if (view === "board") renderBoard();
  else renderTable();
}

function renderTable() {
  const rows = visibleRows();
  els.body.innerHTML = "";
  for (const j of rows) {
    const tr = document.createElement("tr");
    if (j.report_num && j.report_num === selectedNum) tr.classList.add("selected");
    const scoreText = j.score_value == null ? "—" : j.score_value.toFixed(1);
    tr.innerHTML = `
      <td class="num"><span class="score-pill ${scoreClass(j.score_value)}">${scoreText}</span></td>
      <td title="${escapeAttr(j.company)}">${escapeHtml(j.company)}</td>
      <td title="${escapeAttr(j.role)}">${escapeHtml(j.role)}</td>
      <td><span class="status-badge">${escapeHtml(j.status)}</span></td>
      <td>${escapeHtml(j.date)}</td>`;
    tr.addEventListener("click", () => openReport(j));
    els.body.appendChild(tr);
  }
  els.count.textContent = `${rows.length} of ${JOBS.length} role${JOBS.length === 1 ? "" : "s"}`;
  els.empty.hidden = JOBS.length !== 0;
}

function renderBoard() {
  const rows = visibleRows();
  // Bucket by canonical status; anything unrecognized goes under Evaluated.
  const buckets = Object.fromEntries(STATES.map((s) => [s, []]));
  for (const j of rows) {
    const col = STATES.includes(j.status_canonical) ? j.status_canonical : "Evaluated";
    buckets[col].push(j);
  }
  els.boardPane.innerHTML = "";
  for (const state of STATES) {
    const col = document.createElement("div");
    col.className = "kanban-col";
    col.dataset.status = state;
    const cards = buckets[state];
    col.innerHTML = `<h3>${state} <span class="col-count">${cards.length}</span></h3>`;
    const list = document.createElement("div");
    list.className = "kanban-cards";
    for (const j of cards) list.appendChild(makeCard(j));
    col.appendChild(list);
    wireColumnDrop(col);
    els.boardPane.appendChild(col);
  }
  els.count.textContent = `${rows.length} of ${JOBS.length} role${JOBS.length === 1 ? "" : "s"}`;
}

function makeCard(j) {
  const card = document.createElement("div");
  card.className = "kanban-card" + (j.pending ? " pending" : "");
  card.draggable = true;
  card.dataset.num = j.num;
  const scoreText = j.score_value == null ? "—" : j.score_value.toFixed(1);
  card.innerHTML = `
    <div class="card-top">
      <span class="card-company">${escapeHtml(j.company)}</span>
      <span class="score-pill ${scoreClass(j.score_value)}">${scoreText}</span>
    </div>
    <div class="card-role">${escapeHtml(j.role)}</div>`;
  card.addEventListener("click", () => openReport(j));
  card.addEventListener("dragstart", (e) => {
    card.classList.add("dragging");
    e.dataTransfer.setData("text/plain", String(j.num));
    e.dataTransfer.effectAllowed = "move";
  });
  card.addEventListener("dragend", () => card.classList.remove("dragging"));
  return card;
}

function wireColumnDrop(col) {
  col.addEventListener("dragover", (e) => { e.preventDefault(); col.classList.add("dragover"); });
  col.addEventListener("dragleave", () => col.classList.remove("dragover"));
  col.addEventListener("drop", async (e) => {
    e.preventDefault();
    col.classList.remove("dragover");
    const num = e.dataTransfer.getData("text/plain");
    const newStatus = col.dataset.status;
    const job = JOBS.find((j) => String(j.num) === String(num));
    if (!job || job.status_canonical === newStatus) return;
    await changeStatus(job, newStatus);
  });
}

async function changeStatus(job, newStatus) {
  // Optimistic: update in-memory + re-render immediately.
  job.status = newStatus;
  job.status_canonical = newStatus;
  job.pending = true;
  render();
  try {
    const resp = await fetch("/api/status", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ num: String(job.num), status: newStatus }),
    });
    const body = await resp.json();
    if (!resp.ok) throw new Error(body.detail || "status update failed");
    pending = body.pending;
    updatePushButton();
  } catch (e) {
    showAction(String(e.message || e), "error");
    await loadJobs(); // resync on failure
  }
}

function updatePushButton() {
  if (pending > 0) {
    els.pushBtn.hidden = false;
    els.pushBtn.textContent = `⇧ Push ${pending} change${pending === 1 ? "" : "s"}`;
  } else {
    els.pushBtn.hidden = true;
  }
}

let selectedJob = null;

async function openReport(job) {
  if (!job.report_num) return;
  selectedNum = job.report_num;
  selectedJob = job;
  resetSkillPanel();
  render();
  els.reportLink.href = extractUrl(job) || "#";
  els.reportBody.innerHTML = "<p class='empty'>Loading…</p>";
  els.reportPane.hidden = false;
  try {
    const resp = await fetch(`/api/reports/${encodeURIComponent(job.report_num)}`);
    els.reportBody.innerHTML = resp.ok
      ? await resp.text()
      : "<p class='empty'>Report not found.</p>";
  } catch (e) {
    els.reportBody.innerHTML = `<p class='empty'>Error loading report: ${escapeHtml(String(e))}</p>`;
  }
}

// The report URL isn't a tracker column; pull the first http(s) link out of
// the notes cell if present. (The full URL also lives in the report header.)
function extractUrl(job) {
  const m = (job.notes || "").match(/https?:\/\/\S+/);
  return m ? m[0] : null;
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}
function escapeAttr(s) {
  return escapeHtml(s).replace(/"/g, "&quot;");
}

// Sorting: click a header to sort; click again to flip direction.
document.querySelectorAll("th[data-sort]").forEach((th) => {
  th.addEventListener("click", () => {
    const key = th.dataset.sort;
    if (sortKey === key) {
      sortDir = -sortDir;
    } else {
      sortKey = key;
      sortDir = key === "score_value" ? -1 : 1; // scores default high→low, text A→Z
    }
    document.querySelectorAll("th[data-sort]").forEach((h) => {
      h.classList.remove("sorted-asc", "sorted-desc");
    });
    th.classList.add(sortDir === 1 ? "sorted-asc" : "sorted-desc");
    render();
  });
});

els.filter.addEventListener("input", render);
els.statusFilter.addEventListener("change", render);
els.reportClose.addEventListener("click", () => {
  els.reportPane.hidden = true;
  selectedNum = null;
  render();
});

els.viewTable.addEventListener("click", () => { view = "table"; render(); });
els.viewBoard.addEventListener("click", () => { view = "board"; render(); });

// ── Cloud actions (gh-backed) ────────────────────────────────────────────

const refreshBtn = document.getElementById("refresh-btn");
const runBtn = document.getElementById("run-btn");
const actionMsg = document.getElementById("action-msg");

function showAction(text, kind) {
  actionMsg.textContent = text;
  actionMsg.className = "action-msg" + (kind ? " " + kind : "");
  actionMsg.hidden = false;
}

async function postAction(path) {
  const resp = await fetch(path, { method: "POST" });
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(body.detail || `${path} failed (${resp.status})`);
  return body;
}

refreshBtn.addEventListener("click", async () => {
  refreshBtn.disabled = true;
  showAction("Downloading latest results from GitHub…", "");
  try {
    const r = await postAction("/api/refresh");
    await loadJobs();
    showAction(`Loaded results from run #${r.run_id}${r.title ? " — " + r.title : ""}.`, "ok");
  } catch (e) {
    showAction(String(e.message || e), "error");
  } finally {
    refreshBtn.disabled = false;
  }
});

runBtn.addEventListener("click", async () => {
  if (!confirm("Trigger a new pipeline run in the cloud? Results take a while; use Refresh later to pull them.")) return;
  runBtn.disabled = true;
  showAction("Triggering a pipeline run…", "");
  try {
    await postAction("/api/run");
    showAction("Run triggered. It executes on GitHub; click Refresh once it finishes.", "ok");
  } catch (e) {
    showAction(String(e.message || e), "error");
  } finally {
    runBtn.disabled = false;
  }
});

els.pushBtn.addEventListener("click", async () => {
  els.pushBtn.disabled = true;
  showAction("Refreshing latest tracker, applying your changes, pushing to GitHub…", "");
  try {
    const r = await postAction("/api/push-status");
    await loadJobs();  // reflects the merged/cleared state
    const note = r.base === "refreshed"
      ? "applied onto the latest cloud tracker"
      : "pushed from the local tracker (couldn't refresh first)";
    showAction(`Pushed ${r.pushed} status change${r.pushed === 1 ? "" : "s"} — ${note}.`, "ok");
  } catch (e) {
    showAction(String(e.message || e), "error");
  } finally {
    els.pushBtn.disabled = false;
  }
});

// ── career-ops skills (resume tailoring) ───────────────────────────────────

const tailorBtn = document.getElementById("tailor-btn");
const skillPanel = document.getElementById("skill-panel");
let CAPS = { cli: { available: false }, api: { available: false }, default_path: "ask" };

async function loadCaps() {
  try {
    CAPS = await (await fetch("/api/capabilities")).json();
  } catch { /* leave defaults; the button explains the no-capability case */ }
}

function resetSkillPanel() {
  skillPanel.hidden = true;
  skillPanel.innerHTML = "";
}

// Decide which path to use, honoring a set default, then availability.
function chooseTailorPath() {
  const cli = CAPS.cli.available, api = CAPS.api.available;
  if (!cli && !api) return "none";
  const def = CAPS.default_path;
  if (def === "cli" && cli) return "cli";
  if (def === "api" && api) return "api";
  if (cli && api) return "choose";   // default is "ask" → let the user pick
  return cli ? "cli" : "api";
}

tailorBtn.addEventListener("click", () => {
  if (!selectedJob) return;
  const path = chooseTailorPath();
  if (path === "none") {
    showSkill(
      "No way to run this yet. Install an agent CLI (e.g. claude) or set an LLM " +
      "API key (e.g. GEMINI_API_KEY), then reload.", "error");
    return;
  }
  if (path === "choose") {
    skillPanel.hidden = false;
    skillPanel.className = "skill-panel";
    skillPanel.innerHTML =
      `<p>Run resume tailoring via:</p>
       <div class="skill-choice">
         <button data-path="api">⚡ API — ${escapeHtml(CAPS.api.provider || "provider")} (bounded, no install)</button>
         <button data-path="cli">⌨ CLI — ${escapeHtml(CAPS.cli.name)} (interactive, uses your agent)</button>
       </div>`;
    skillPanel.querySelectorAll("button[data-path]").forEach((b) =>
      b.addEventListener("click", () => runTailor(b.dataset.path)));
    return;
  }
  runTailor(path);
});

async function runTailor(path) {
  const job = selectedJob;
  showSkill(path === "api" ? "Tailoring résumé via the API…" : "Building the CLI command…", "");
  try {
    const resp = await fetch("/api/skills/tailor-resume", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ num: String(job.num), path }),
    });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(body.detail || `tailoring failed (${resp.status})`);
    if (body.path === "cli") renderCliResult(body);
    else renderApiResult(body);
  } catch (e) {
    showSkill(String(e.message || e), "error");
  }
}

function renderApiResult(body) {
  skillPanel.hidden = false;
  skillPanel.className = "skill-panel ok";
  skillPanel.innerHTML =
    `<p>Tailored résumé generated via ${escapeHtml(body.provider)}:</p>
     <a class="skill-download" href="${escapeAttr(body.download_url)}" download>⬇ ${escapeHtml(body.output_file)}</a>`;
}

function renderCliResult(body) {
  skillPanel.hidden = false;
  skillPanel.className = "skill-panel";
  skillPanel.innerHTML =
    `<p>Run this in your terminal (interactive — refines with your agent):</p>
     <pre class="skill-cmd"><code>${escapeHtml(body.command)}</code></pre>
     <button id="skill-copy">Copy command</button>`;
  document.getElementById("skill-copy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(body.command); showSkill("Command copied.", "ok"); }
    catch { showSkill("Couldn't copy — select the command and copy manually.", "error"); }
  });
}

function showSkill(text, kind) {
  skillPanel.hidden = false;
  skillPanel.className = "skill-panel" + (kind ? " " + kind : "");
  skillPanel.innerHTML = `<p>${escapeHtml(text)}</p>`;
}

loadCaps();
loadJobs();
