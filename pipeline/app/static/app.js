"use strict";

// Read-only triage view: load tracker rows from /api/jobs, render a
// sortable/filterable table, click a row to view its rendered report.

let JOBS = [];
let sortKey = "score_value";
let sortDir = -1; // -1 = descending (highest score first by default)
let selectedNum = null;

const els = {
  body: document.getElementById("jobs-body"),
  filter: document.getElementById("filter"),
  statusFilter: document.getElementById("status-filter"),
  count: document.getElementById("count"),
  empty: document.getElementById("empty"),
  reportPane: document.getElementById("report-pane"),
  reportBody: document.getElementById("report-body"),
  reportClose: document.getElementById("report-close"),
  reportLink: document.getElementById("report-link"),
};

async function loadJobs() {
  const resp = await fetch("/api/jobs");
  const payload = await resp.json();
  JOBS = payload.rows || [];
  showSourceBanner(payload.source);
  populateStatusFilter();
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

async function openReport(job) {
  if (!job.report_num) return;
  selectedNum = job.report_num;
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

loadJobs();
