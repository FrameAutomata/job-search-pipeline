"use strict";

// Guided onboarding wizard. Collects a resume PDF + preferences, then POSTs
// multipart to /api/onboard, which generates the profile artifacts and writes
// them as GitHub secrets.

const STEP_TITLES = ["Resume", "About", "Roles", "Search", "Narrative", "Provider", "Review"];

const form = document.getElementById("wizard");
const steps = [...document.querySelectorAll(".step")];
const stepper = document.getElementById("steps");
const backBtn = document.getElementById("back-btn");
const nextBtn = document.getElementById("next-btn");
const submitBtn = document.getElementById("submit-btn");
const actionMsg = document.getElementById("action-msg");
const resumeInput = document.getElementById("resume");
const repoLine = document.getElementById("repo-line");
const statusBanner = document.getElementById("status-banner");
const reviewEl = document.getElementById("review");
const reviewRepo = document.getElementById("review-repo");

let current = 0;

// Build the step indicator.
STEP_TITLES.forEach((t, i) => {
  const li = document.createElement("li");
  li.textContent = `${i + 1}. ${t}`;
  li.dataset.step = i;
  stepper.appendChild(li);
});

function showStep(i) {
  current = Math.max(0, Math.min(steps.length - 1, i));
  steps.forEach((s) => (s.hidden = Number(s.dataset.step) !== current));
  [...stepper.children].forEach((li, idx) => {
    li.classList.toggle("active", idx === current);
    li.classList.toggle("done", idx < current);
  });
  backBtn.disabled = current === 0;
  const last = current === steps.length - 1;
  nextBtn.hidden = last;
  submitBtn.hidden = !last;
  if (last) renderReview();
}

function showAction(text, kind) {
  actionMsg.textContent = text;
  actionMsg.className = "action-msg" + (kind ? " " + kind : "");
  actionMsg.hidden = false;
}

// Collect the form into a plain object (sites -> array of checked values).
function collectForm() {
  const fd = new FormData(form);
  const obj = {};
  for (const [k, v] of fd.entries()) {
    if (k === "sites") continue; // handled below
    obj[k] = typeof v === "string" ? v : undefined;
  }
  obj.sites = [...form.querySelectorAll('input[name="sites"]:checked')].map((c) => c.value);
  obj.include_easy_apply = form.querySelector('input[name="include_easy_apply"]').checked;
  return obj;
}

function renderReview() {
  const f = collectForm();
  const file = resumeInput.files[0];
  const lines = [
    `Resume:        ${file ? file.name : "(none selected!)"}`,
    `Name:          ${f.name || "(default)"}`,
    `Target roles:  ${f.target_roles || "(default: Software Engineer)"}`,
    `Avoid:         ${f.negative_roles || "(none)"}`,
    `Comp:          ${f.comp_target || "$130K-170K"} (min ${f.comp_min || "$110K"})`,
    `Locations:     ${f.locations || "(default: US Remote)"}`,
    `Recency:       ${f.hours_old || 24}h · results ${f.results_wanted || 100} · ${f.distance || 50}mi`,
    `Boards:        ${(f.sites || []).join(", ") || "(default)"}`,
    `Easy Apply:    ${f.include_easy_apply ? "yes" : "no"}`,
    `Provider:      ${f.provider}${f.batch_model ? " · " + f.batch_model : ""}`,
    `API key:       ${f.api_key ? "•".repeat(Math.min(12, f.api_key.length)) + " (will be written)" : "(none — required)"}`,
  ];
  reviewEl.textContent = lines.join("\n");
}

// Light per-step validation before advancing.
function validateStep(i) {
  if (i === 0 && !resumeInput.files[0]) {
    showAction("Please choose a PDF resume to continue.", "error");
    return false;
  }
  actionMsg.hidden = true;
  return true;
}

nextBtn.addEventListener("click", () => {
  if (!validateStep(current)) return;
  showStep(current + 1);
});
backBtn.addEventListener("click", () => showStep(current - 1));

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = collectForm();
  if (!resumeInput.files[0]) { showAction("Resume PDF is required.", "error"); showStep(0); return; }
  if (!f.api_key) { showAction("An API key is required to evaluate jobs.", "error"); showStep(5); return; }

  const fd = new FormData();
  fd.append("resume", resumeInput.files[0]);
  fd.append("form", JSON.stringify(f));

  submitBtn.disabled = true;
  showAction("Generating your profile and writing secrets… this takes a few seconds.", "");
  try {
    const resp = await fetch("/api/onboard", { method: "POST", body: fd });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(body.detail || `onboarding failed (${resp.status})`);
    showAction(
      `Done — wrote ${body.secrets_written.length} secrets to ${body.repo}: ` +
      `${body.secrets_written.join(", ")}. You can now go back and click "Run now".`,
      "ok"
    );
    submitBtn.hidden = true;
    nextBtn.hidden = true;
  } catch (err) {
    showAction(String(err.message || err), "error");
    submitBtn.disabled = false;
  }
});

// Load repo status up front so the user knows where secrets will go (and whether
// the repo is private).
async function loadStatus() {
  try {
    const resp = await fetch("/api/onboard/status");
    const s = await resp.json();
    if (!resp.ok) throw new Error(s.detail || "could not read repo status");
    repoLine.textContent = `Target repo: ${s.repo} (${s.visibility})`;
    reviewRepo.textContent = s.repo;
    if (s.visibility === "PUBLIC") {
      statusBanner.hidden = false;
      statusBanner.textContent =
        "⚠ This repo is PUBLIC. Make your fork private before onboarding — " +
        "onboarding will refuse to write secrets to a public repo.";
    } else if (s.ready) {
      statusBanner.hidden = false;
      statusBanner.classList.add("ok-banner");
      statusBanner.textContent =
        "✓ Already configured. Submitting again overwrites the existing secrets.";
    }
  } catch (err) {
    repoLine.textContent = `Could not read repo status: ${err.message}. ` +
      "Is gh installed and authenticated?";
  }
}

showStep(0);
loadStatus();
