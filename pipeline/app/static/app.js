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

// Statuses hidden by default: terminal/actioned states where no further action is needed.
const ACTIONED_STATUSES = new Set(["Applied", "Rejected", "Discarded", "SKIP"]);
let hideActioned = localStorage.getItem("hideActioned") !== "false";

const els = {
  body: document.getElementById("jobs-body"),
  filter: document.getElementById("filter"),
  statusFilter: document.getElementById("status-filter"),
  hideActionedChk: document.getElementById("hide-actioned"),
  count: document.getElementById("count"),
  empty: document.getElementById("empty"),
  tablePane: document.getElementById("table-pane"),
  boardPane: document.getElementById("board-pane"),
  reportPane: document.getElementById("report-pane"),
  reportBody: document.getElementById("report-body"),
  reportClose: document.getElementById("report-close"),
  reportLink: document.getElementById("report-link"),
  reportStatus: document.getElementById("report-status"),
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
    if (view === "table" && hideActioned && ACTIONED_STATUSES.has(j.status_canonical)) return false;
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
  resetApplyPanel();
  applyEls.btn.hidden = !isLinkedInJob(job);
  applyEls.btn.disabled = false;
  render();
  els.reportLink.href = extractUrl(job) || "#";
  els.reportStatus.value = STATES.includes(job.status_canonical) ? job.status_canonical : "Evaluated";
  els.reportBody.innerHTML = "<p class='empty'>Loading…</p>";
  els.reportPane.hidden = false;
  try {
    const resp = await fetch(`/api/reports/${encodeURIComponent(job.report_num)}`);
    els.reportBody.innerHTML = resp.ok
      ? await resp.text()
      : "<p class='empty'>Report not found.</p>";
    // Fallback: rows evaluated before the URL-into-notes splice landed (or
    // produced by a provider that ignored the splice) have no URL in the
    // tracker's notes cell, so extractUrl returned null and the link is "#".
    // Every report's markdown header carries a "**URL:** ..." line, so once
    // the report body is in the DOM we can recover the link from there.
    if (resp.ok && els.reportLink.getAttribute("href") === "#") {
      const m = (els.reportBody.textContent || "").match(/https?:\/\/[^\s<>"')]+/);
      if (m) els.reportLink.href = m[0];
    }
  } catch (e) {
    els.reportBody.innerHTML = `<p class='empty'>Error loading report: ${escapeHtml(String(e))}</p>`;
  }
}

// The report URL isn't a tracker column; pull the first http(s) link out of
// the notes cell if present. (The full URL also lives in the report header,
// recovered as a fallback in openReport above.)
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
els.hideActionedChk.checked = hideActioned;
els.hideActionedChk.addEventListener("change", () => {
  hideActioned = els.hideActionedChk.checked;
  localStorage.setItem("hideActioned", hideActioned);
  render();
});
els.reportClose.addEventListener("click", () => {
  resetApplyPanel();   // cancel any live apply session + stop its poll loop
  els.reportPane.hidden = true;
  selectedNum = null;
  render();
});

els.viewTable.addEventListener("click", () => { view = "table"; render(); });
els.viewBoard.addEventListener("click", () => { view = "board"; render(); });

// Populate the report-pane status select once, then wire its change event to
// the same persistence path the kanban uses (optimistic update + pending push).
for (const s of STATES) {
  const opt = document.createElement("option");
  opt.value = s;
  opt.textContent = s;
  els.reportStatus.appendChild(opt);
}
els.reportStatus.addEventListener("change", () => {
  if (!selectedJob) return;
  const newStatus = els.reportStatus.value;
  if (selectedJob.status_canonical === newStatus) return;
  changeStatus(selectedJob, newStatus);
});

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

// ── local pipeline run ─────────────────────────────────────────────────────

const localRun = {
  btn: document.getElementById("run-local-btn"),
  panel: document.getElementById("local-run-panel"),
  passes: document.getElementById("local-passes"),
  evaluate: document.getElementById("local-evaluate"),
  start: document.getElementById("local-start"),
  cancel: document.getElementById("local-cancel"),
  stages: document.getElementById("local-stages"),
  log: document.getElementById("local-log"),
  timer: null,
};

localRun.btn.addEventListener("click", () => {
  localRun.panel.hidden = !localRun.panel.hidden;
});

function renderLocalStatus(s) {
  const running = s.running;
  localRun.start.hidden = running;
  localRun.cancel.hidden = !running;
  localRun.passes.disabled = running;
  localRun.evaluate.disabled = running;
  localRun.btn.textContent = running ? `⏳ ${s.stage || "starting"}…` : "💻 Run local";
  if (s.stages && (running || s.exit_code !== null)) {
    localRun.stages.hidden = false;
    localRun.stages.innerHTML = s.stages.map((st) => {
      const seen = (s.stages_seen || []).includes(st);
      const current = st === s.stage && running;
      return `<span class="stage${seen ? " seen" : ""}${current ? " current" : ""}">${st}</span>`;
    }).join("<span class=\"stage-sep\">→</span>");
  }
  localRun.log.hidden = !s.log_tail;
  if (s.log_tail) {
    const atBottom = localRun.log.scrollTop + localRun.log.clientHeight >= localRun.log.scrollHeight - 8;
    localRun.log.textContent = s.log_tail;
    if (atBottom) localRun.log.scrollTop = localRun.log.scrollHeight;
  }
}

async function pollLocalRun() {
  clearTimeout(localRun.timer);
  let s;
  try {
    const resp = await fetch("/api/run-local/status");
    s = await resp.json();
  } catch (_) {
    localRun.timer = setTimeout(pollLocalRun, 4000);  // network blip — keep polling
    return;
  }
  renderLocalStatus(s);
  if (s.running) {
    localRun.timer = setTimeout(pollLocalRun, 2500);
    return;
  }
  if (s.exit_code === null) return;        // never started this session
  if (s.ok) {
    try {
      await postAction("/api/use-local");  // show the fresh local results
      await loadJobs();
      showAction("Local pipeline run finished — showing local results.", "ok");
    } catch (e) {
      showAction(String(e.message || e), "error");
    }
  } else if (localRun.wasCancelled) {
    localRun.wasCancelled = false;         // cancel already showed its message
  } else {
    showAction(`Local pipeline run failed (exit ${s.exit_code}) — see the log below the toolbar.`, "error");
    localRun.panel.hidden = false;
  }
}

localRun.start.addEventListener("click", async () => {
  localRun.start.disabled = true;
  // Fresh run: clear any stale cancel flag from a prior run so it can't swallow
  // THIS run's failure toast (the flag is only meant to suppress the toast for
  // the run the user actually cancelled).
  localRun.wasCancelled = false;
  try {
    const resp = await fetch("/api/run-local", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ passes: localRun.passes.value, evaluate: localRun.evaluate.checked }),
    });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(body.detail || `run-local failed (${resp.status})`);
    showAction("Local pipeline run started.", "ok");
    pollLocalRun();
  } catch (e) {
    showAction(String(e.message || e), "error");
  } finally {
    localRun.start.disabled = false;
  }
});

localRun.cancel.addEventListener("click", async () => {
  if (!confirm("Cancel the running local pipeline?")) return;
  try {
    localRun.wasCancelled = true;
    await postAction("/api/run-local/cancel");
    showAction("Local pipeline run cancelled.", "");
  } catch (e) {
    showAction(String(e.message || e), "error");
  }
  pollLocalRun();
});

// Resume polling if a run is already in progress (e.g. the page was reloaded).
fetch("/api/run-local/status").then((r) => r.json()).then((s) => {
  if (s.running) {
    localRun.panel.hidden = false;
    renderLocalStatus(s);
    localRun.timer = setTimeout(pollLocalRun, 2500);
  }
}).catch(() => {});

// ── tracker liveness re-check ──────────────────────────────────────────────

const recheckUi = {
  btn: document.getElementById("recheck-btn"),
  label: "🩺 Re-check liveness",
  timer: null,
};

function renderRecheck(s) {
  recheckUi.btn.disabled = s.running;
  // Number.isFinite so a total of 0 still shows "0/0" (a `?` falsy-test would
  // drop the denominator); total is null only before the first progress tick.
  recheckUi.btn.textContent = s.running
    ? `⏳ Re-checking ${Number.isFinite(s.total) ? `${s.checked}/${s.total}` : s.checked}…`
    : recheckUi.label;
}

async function pollRecheck() {
  clearTimeout(recheckUi.timer);
  let s;
  try {
    s = await (await fetch("/api/recheck-liveness/status")).json();
  } catch (_) {
    recheckUi.timer = setTimeout(pollRecheck, 4000);   // network blip — keep polling
    return;
  }
  renderRecheck(s);
  if (s.running) {
    recheckUi.timer = setTimeout(pollRecheck, 2000);
    return;
  }
  if (!s.done) return;                                 // nothing ran this session
  if (s.ok) {
    await loadJobs().catch(() => {});                  // surface the new Discarded rows
    const n = s.discarded;
    // Roles fetched this run but not conclusively read (uncertain or
    // rate-limited) aren't "still open" — subtract them so a throttled sweep
    // can't masquerade as a clean all-open result.
    const caveats = [];
    if (s.unconfirmed) caveats.push(`${s.unconfirmed} couldn't be reached`);
    if (s.throttled) caveats.push(`${s.throttled} rate-limited (will retry)`);
    if (s.deferred) caveats.push(`${s.deferred} deferred to a later run`);
    const tail = caveats.length ? ` ${caveats.join(", ")}.` : "";
    const open = s.checked - (s.unconfirmed || 0) - (s.throttled || 0);
    showAction(
      n
        ? `Liveness re-check: ${n} closed posting${n === 1 ? "" : "s"} marked Discarded (of ${s.checked} checked).${tail}`
        : tail
          ? `Liveness re-check: ${open} role${open === 1 ? "" : "s"} confirmed still open.${tail}`
          : `Liveness re-check: all ${s.checked} role${s.checked === 1 ? "" : "s"} still open.`,
      "ok");
  } else {
    showAction(`Liveness re-check failed${s.error ? `: ${s.error}` : ""}.`, "error");
  }
}

recheckUi.btn.addEventListener("click", async () => {
  recheckUi.btn.disabled = true;
  try {
    const resp = await fetch("/api/recheck-liveness", { method: "POST" });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(body.detail || `re-check failed (${resp.status})`);
    showAction("Liveness re-check started…", "ok");
    pollRecheck();
  } catch (e) {
    recheckUi.btn.disabled = false;
    showAction(String(e.message || e), "error");
  }
});

// Resume polling if a sweep is already in progress (e.g. the page was reloaded).
fetch("/api/recheck-liveness/status").then((r) => r.json()).then((s) => {
  if (s.running) { renderRecheck(s); recheckUi.timer = setTimeout(pollRecheck, 2000); }
}).catch(() => {});

els.pushBtn.addEventListener("click", async () => {
  els.pushBtn.disabled = true;
  showAction("Applying your changes onto the latest cloud tracker and pushing to GitHub…", "");
  try {
    const r = await postAction("/api/push-status");
    await loadJobs();  // the pushed-override overlay keeps the change shown here
    let msg;
    if (r.pushed > 0) {
      // The cloud cache is updated immediately by edit-tracker, but it only
      // shows up in a downloaded artifact after the next pipeline run — so set
      // that expectation instead of implying it's instantly visible on Refresh.
      msg = `Pushed ${r.pushed} change${r.pushed === 1 ? "" : "s"} to the cloud tracker. ` +
            "They stay shown here and will appear in the tracker after the next pipeline run.";
    } else {
      msg = "Nothing pushed — none of the pending changes matched a row in the current cloud tracker.";
    }
    if (r.unresolved) {
      msg += ` (${r.unresolved} couldn't be matched to a current row and were kept for a later push.)`;
    }
    showAction(msg, r.pushed > 0 ? "ok" : "");
  } catch (e) {
    showAction(String(e.message || e), "error");
  } finally {
    els.pushBtn.disabled = false;
  }
});

// ── career-ops skills ──────────────────────────────────────────────────────

const skillActions = document.getElementById("skill-actions");
const skillPanel = document.getElementById("skill-panel");
let CAPS = { cli: { available: false }, api: { available: false }, terminal: { available: false }, default_path: "ask", skills: [] };
let currentSkill = null;  // last skill run; used by Run-in-terminal's relaunch.

async function loadCaps() {
  try {
    CAPS = await (await fetch("/api/capabilities")).json();
  } catch { /* leave defaults; buttons explain the no-capability case */ }
  renderSkillActions();
}

// One button per skill in the report header. Rendered once caps are known.
function renderSkillActions() {
  skillActions.innerHTML = "";
  for (const skill of CAPS.skills || []) {
    const btn = document.createElement("button");
    btn.textContent = skill.label;
    btn.title = `Run "${skill.label}" for this role`;
    btn.addEventListener("click", () => startSkill(skill));
    skillActions.appendChild(btn);
  }
}

function resetSkillPanel() {
  skillPanel.hidden = true;
  skillPanel.innerHTML = "";
}

// Which paths can run THIS skill: CLI works for all; API only for api-capable
// skills with a key configured.
function pathsFor(skill) {
  return { cli: !!CAPS.cli.available, api: !!(skill.api && CAPS.api.available) };
}

// Decide the path, honoring a set default then availability.
function choosePath(skill) {
  const { cli, api } = pathsFor(skill);
  if (!cli && !api) return "none";
  const def = CAPS.default_path;
  if (def === "cli" && cli) return "cli";
  if (def === "api" && api) return "api";
  if (cli && api) return "choose";   // default is "ask" → let the user pick
  return cli ? "cli" : "api";
}

function startSkill(skill) {
  if (!selectedJob) return;
  const path = choosePath(skill);
  if (path === "none") {
    // Skill is unrunnable: say specifically what's missing for it.
    const need = skill.api
      ? "Install an agent CLI (e.g. claude) or set an LLM API key (e.g. GEMINI_API_KEY)"
      : "This skill needs an agent CLI (live browser / web search). Install one (e.g. claude) or set BATCH_CLI";
    showSkill(`Can't run “${skill.label}” yet. ${need}, then reload.`, "error");
    return;
  }
  if (path === "choose") {
    skillPanel.hidden = false;
    skillPanel.className = "skill-panel";
    skillPanel.innerHTML =
      `<p>Run “${escapeHtml(skill.label)}” via:</p>
       <div class="skill-choice">
         <button data-path="api">⚡ API — ${escapeHtml(CAPS.api.provider || "provider")} (bounded, no install)</button>
         <button data-path="cli">⌨ CLI — ${escapeHtml(CAPS.cli.name)} (interactive, uses your agent)</button>
       </div>`;
    skillPanel.querySelectorAll("button[data-path]").forEach((b) =>
      b.addEventListener("click", () => runSkill(skill, b.dataset.path)));
    return;
  }
  runSkill(skill, path);
}

async function runSkill(skill, path) {
  currentSkill = skill;
  const job = selectedJob;
  showSkill(path === "api" ? `Running “${skill.label}” via the API…` : "Building the CLI command…", "");
  try {
    const resp = await fetch("/api/skills/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ skill: skill.id, num: String(job.num), path }),
    });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(body.detail || `skill failed (${resp.status})`);
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
    `<p>Generated via ${escapeHtml(body.provider)}:</p>
     <a class="skill-download" href="${escapeAttr(body.download_url)}" download>⬇ ${escapeHtml(body.output_file)}</a>`;
}

function renderCliResult(body) {
  skillPanel.hidden = false;
  skillPanel.className = "skill-panel";
  const canLaunch = !!(CAPS.terminal && CAPS.terminal.available);
  const prereqs = body.prereqs || [];
  // Prereq notes render any backtick-wrapped commands as <code> so the user
  // can copy them. The notes themselves are server-controlled strings — never
  // user input — so this rich rendering is safe.
  const prereqHtml = prereqs.length
    ? `<div class="skill-prereqs">
         <strong>One-time setup for this skill:</strong>
         <ul>${prereqs.map((n) => `<li>${renderPrereq(n)}</li>`).join("")}</ul>
       </div>`
    : "";
  skillPanel.innerHTML =
    `${prereqHtml}
     <p>Run this in your terminal (interactive — refines with your agent):</p>
     <pre class="skill-cmd"><code>${escapeHtml(body.command)}</code></pre>
     <div class="skill-choice">
       ${canLaunch ? `<button id="skill-run">▶ Run in terminal</button>` : ""}
       <button id="skill-copy">Copy command</button>
     </div>`;
  document.getElementById("skill-copy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(body.command); showSkill("Command copied.", "ok"); }
    catch { showSkill("Couldn't copy — select the command and copy manually.", "error"); }
  });
  if (canLaunch) {
    document.getElementById("skill-run").addEventListener("click", () => runInTerminal(body));
  }
}

async function runInTerminal(cliBody) {
  showSkill("Opening a new terminal…", "");
  try {
    const resp = await fetch("/api/skills/launch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // selectedJob is the role currently open in the side panel; the server
      // rebuilds the exact same command from {skill, num} so a cross-origin
      // attacker can't smuggle an arbitrary command through this endpoint.
      body: JSON.stringify({ skill: currentSkill.id, num: String(selectedJob.num) }),
    });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(body.detail || `launch failed (${resp.status})`);
    showSkill("Terminal opened — the agent is running in the new window.", "ok");
  } catch (e) {
    showSkill(String(e.message || e), "error");
  }
}

function showSkill(text, kind) {
  skillPanel.hidden = false;
  skillPanel.className = "skill-panel" + (kind ? " " + kind : "");
  skillPanel.innerHTML = `<p>${escapeHtml(text)}</p>`;
}

// Prereq notes are server-controlled strings; we escape them, then promote
// backtick-fenced spans to <code> so commands are visually distinct (and
// easy to spot-copy).
function renderPrereq(note) {
  return escapeHtml(note).replace(/`([^`]+)`/g, "<code>$1</code>");
}

// ── apply review-and-submit ─────────────────────────────────────────────────
// Open a visible browser, fill the LinkedIn Easy Apply form, review the drafted
// answers, then Submit (or Cancel). The server holds the browser open between
// fill and submit; we just poll status and surface Submit/Cancel.

const applyEls = {
  btn: document.getElementById("apply-btn"),
  panel: document.getElementById("apply-panel"),
  jobId: null,
  timer: null,
  deciding: false,   // a Submit/Cancel POST is in flight — don't rebuild the panel
};

applyEls.btn.addEventListener("click", startApply);

function isLinkedInJob(job) {
  return /linkedin\.com\/jobs\/view\//i.test(extractUrl(job) || "");
}

function resetApplyPanel() {
  clearTimeout(applyEls.timer);
  // Cancel a still-live session so navigating away doesn't orphan the held
  // browser and 409-block the next apply until the hold timeout (a terminal /
  // submitting session 409s here, which is fine — the .catch swallows it).
  if (applyEls.jobId) {
    fetch(`/api/jobs/apply-cancel/${applyEls.jobId}`, { method: "POST" }).catch(() => {});
  }
  applyEls.jobId = null;
  applyEls.deciding = false;
  applyEls.panel.hidden = true;
  applyEls.panel.innerHTML = "";
}

async function startApply() {
  if (!selectedJob) return;
  applyEls.btn.disabled = true;
  applyEls.panel.hidden = false;
  applyEls.panel.className = "apply-panel";
  applyEls.panel.innerHTML = "<p>Opening a browser and filling the form — watch the window…</p>";
  try {
    const resp = await fetch("/api/jobs/apply-async", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ num: String(selectedJob.num) }),
    });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(body.detail || `apply failed (${resp.status})`);
    applyEls.jobId = body.job_id;
    pollApply();
  } catch (e) {
    applyEls.panel.className = "apply-panel error";
    applyEls.panel.innerHTML = `<p>${escapeHtml(String(e.message || e))}</p>`;
    applyEls.btn.disabled = false;
  }
}

async function pollApply() {
  clearTimeout(applyEls.timer);
  if (!applyEls.jobId) return;
  let s;
  try {
    s = await (await fetch(`/api/jobs/apply-status/${applyEls.jobId}`)).json();
  } catch (_) {
    applyEls.timer = setTimeout(pollApply, 1500);  // network blip — keep polling
    return;
  }
  renderApply(s);
  if (["pending", "ready", "submitting", "cancelling"].includes(s.status)) {
    applyEls.timer = setTimeout(pollApply, s.status === "ready" ? 2500 : 1000);
  }
}

function renderApply(s) {
  const p = applyEls.panel;
  p.hidden = false;
  if (s.status === "pending") {
    p.className = "apply-panel";
    p.innerHTML = "<p>Opening a browser and filling the form — watch the window…</p>";
    return;
  }
  if (s.status === "submitting") {
    p.className = "apply-panel";
    p.innerHTML = "<p>Submitting…</p>";
    return;
  }
  if (s.status === "cancelling") {
    p.className = "apply-panel";
    p.innerHTML = "<p>Cancelling…</p>";
    return;
  }
  if (s.status === "ready") {
    // A decision is in flight (we already showed "Submitting…/Cancelling…") —
    // don't rebuild the form with live buttons before the worker flips status.
    if (applyEls.deciding) return;
    p.className = "apply-panel";
    const rows = (s.answers || [])
      .map((a) => `<tr><td>${escapeHtml(a[0])}</td><td>${escapeHtml(a[1])}</td></tr>`)
      .join("");
    const review = (s.needs_review || []).length
      ? `<p class="warn">⚠ ${s.needs_review.length} field(s) need your review: ${escapeHtml(s.needs_review.join("; "))}</p>`
      : "";
    p.innerHTML =
      `<p><b>Review the drafted answers, then submit.</b> Nothing is sent until you click Submit.</p>
       ${review}
       <table class="apply-answers"><tbody>${rows || "<tr><td>(no fields drafted)</td><td></td></tr>"}</tbody></table>
       <div class="apply-actions">
         <button id="apply-do-submit" class="primary">Submit application</button>
         <button id="apply-do-cancel">Cancel</button>
       </div>`;
    document.getElementById("apply-do-submit").addEventListener("click", () => decideApply("submit"));
    document.getElementById("apply-do-cancel").addEventListener("click", () => decideApply("cancel"));
    return;
  }
  // Terminal states.
  applyEls.btn.disabled = false;
  applyEls.deciding = false;
  applyEls.jobId = null;   // session is over — don't cancel it again on navigate-away
  if (s.status === "submitted") {
    p.className = "apply-panel ok";
    p.innerHTML = "<p>✓ Submitted — marked Applied.</p>";
    loadJobs();
  } else if (s.status === "expired") {
    p.className = "apply-panel warn";
    p.innerHTML = "<p>This posting is no longer accepting applications — marked Discarded.</p>";
    loadJobs();
  } else if (s.status === "cancelled") {
    p.className = "apply-panel";
    p.innerHTML = "<p>Cancelled — nothing was submitted.</p>";
  } else if (s.status === "timeout") {
    p.className = "apply-panel";
    p.innerHTML = "<p>Timed out waiting for a decision — the browser was closed. Click Apply to retry.</p>";
  } else {
    p.className = "apply-panel error";
    p.innerHTML = `<p>Couldn't apply: ${escapeHtml(s.code || "failed")}${s.reason ? " — " + escapeHtml(s.reason) : ""}</p>`;
  }
}

async function decideApply(decision) {
  if (!applyEls.jobId) return;
  applyEls.deciding = true;   // suppress the ready-panel rebuild until the worker flips
  applyEls.panel.innerHTML = `<p>${decision === "submit" ? "Submitting" : "Cancelling"}…</p>`;
  try {
    const resp = await fetch(`/api/jobs/apply-${decision}/${applyEls.jobId}`, { method: "POST" });
    if (!resp.ok) {
      const b = await resp.json().catch(() => ({}));
      throw new Error(b.detail || `failed (${resp.status})`);
    }
    pollApply();  // the worker moves to submitting -> submitted/cancelled; poll surfaces it
  } catch (e) {
    // The POST didn't take — the session is still as it was. Clear `deciding`
    // and resume polling so the reviewable form (with buttons) returns.
    applyEls.deciding = false;
    applyEls.panel.className = "apply-panel error";
    applyEls.panel.innerHTML =
      `<p>${escapeHtml(String(e.message || e))} — the form is still open; restoring…</p>`;
    applyEls.timer = setTimeout(pollApply, 1500);
  }
}

loadCaps();
loadJobs();

// ── Add Job modal ────────────────────────────────────────────────────────────

const addJobModal  = document.getElementById("add-job-modal");
const addJobForm   = document.getElementById("add-job-form");
const addJobStatus = document.getElementById("add-job-status");
const addJobSubmit = document.getElementById("add-job-submit");
const evalStatus   = document.getElementById("eval-status");

function openAddJobModal() {
  addJobForm.reset();
  addJobStatus.hidden = true;
  addJobStatus.className = "add-job-status";
  addJobSubmit.disabled = false;
  addJobModal.hidden = false;
  document.getElementById("add-job-url").focus();
}

function closeAddJobModal() {
  addJobModal.hidden = true;
}

function setAddJobStatus(text, kind) {
  addJobStatus.textContent = text;
  addJobStatus.className = "add-job-status" + (kind ? " " + kind : "");
  addJobStatus.hidden = false;
}

function showEvalStatus(text, kind) {
  evalStatus.className = "eval-status" + (kind ? " " + kind : "");
  evalStatus.innerHTML = "";
  if (kind === "pending") {
    const dot = document.createElement("span");
    dot.className = "eval-status-dot";
    evalStatus.appendChild(dot);
  }
  const span = document.createElement("span");
  span.textContent = text;
  evalStatus.appendChild(span);
  evalStatus.hidden = false;
}

function pollAddJobStatus(jobId) {
  const iv = setInterval(async () => {
    try {
      const resp = await fetch(`/api/jobs/add-status/${jobId}`);
      const body = await resp.json().catch(() => ({}));
      if (body.status === "done") {
        clearInterval(iv);
        const r = body.result;
        const scoreText = r.score != null ? ` · score ${Number(r.score).toFixed(1)}/5` : "";
        const coRole = [r.company, r.role].filter(Boolean).join(" — ");
        showEvalStatus(`Added #${r.report_num}${coRole ? " — " + coRole : ""}${scoreText}`, "ok");
        await loadJobs();
      } else if (body.status === "error") {
        clearInterval(iv);
        showEvalStatus(body.error || "Evaluation failed", "error");
      }
    } catch (_) { /* network blip — keep polling */ }
  }, 3000);
}

document.getElementById("add-job-btn").addEventListener("click", openAddJobModal);
document.getElementById("add-job-close").addEventListener("click", closeAddJobModal);
document.getElementById("add-job-cancel").addEventListener("click", closeAddJobModal);
addJobModal.addEventListener("click", (e) => { if (e.target === addJobModal) closeAddJobModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !addJobModal.hidden) closeAddJobModal(); });

addJobForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const url     = document.getElementById("add-job-url").value.trim();
  const company = document.getElementById("add-job-company").value.trim();
  const role    = document.getElementById("add-job-role").value.trim();
  if (!url) return;

  addJobSubmit.disabled = true;

  let resp;
  try {
    resp = await fetch("/api/jobs/add-async", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, company, role }),
    });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      setAddJobStatus(body.detail || `Request failed (${resp.status})`, "error");
      if (resp.status === 503) {
        const link = document.createElement("a");
        link.href = "/onboard";
        link.textContent = " → ⚙ Setup";
        link.style.marginLeft = "6px";
        addJobStatus.appendChild(link);
      }
      addJobSubmit.disabled = false;
      return;
    }
    closeAddJobModal();
    showEvalStatus("Evaluating job — fetching description and running LLM… (20–60 s)", "pending");
    pollAddJobStatus(body.job_id);
  } catch (err) {
    setAddJobStatus(String(err.message || err), "error");
    addJobSubmit.disabled = false;
  }
});
