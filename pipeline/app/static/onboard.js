"use strict";

// Guided onboarding wizard. Collects a resume PDF + preferences, then POSTs
// multipart to /api/onboard, which generates the profile artifacts and writes
// them as GitHub secrets.

const STEP_TITLES = ["Resume", "About", "Roles", "Search", "Narrative", "Provider", "Local eval", "Review"];

const form = document.getElementById("wizard");
const steps = [...document.querySelectorAll(".step")];
const stepper = document.getElementById("steps");
const backBtn = document.getElementById("back-btn");
const nextBtn = document.getElementById("next-btn");
const submitBtn = document.getElementById("submit-btn");
const cancelBtn = document.getElementById("cancel-btn");
const actionMsg = document.getElementById("action-msg");
const resumeInput = document.getElementById("resume");
const repoLine = document.getElementById("repo-line");
const statusBanner = document.getElementById("status-banner");
const reviewEl = document.getElementById("review");
const reviewRepo = document.getElementById("review-repo");

let current = 0;
// "Edit" when /api/onboard/load-config returned a saved payload — the user
// has already onboarded once and is just tweaking config. We relax the
// "resume required" and "api_key required" rules, and the submit button
// reads "Save changes" instead of "Write secrets".
let editMode = false;

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
  // On the final step the wizard is effectively complete, so leaving means
  // "done" rather than abandoning setup.
  cancelBtn.textContent = last ? "Finish" : "Cancel";
  if (last) renderReview();
  // Refresh provider detection whenever the Local eval step is shown so it
  // reflects any key just saved from the cloud Provider step.
  if (current === 6) loadLocalProviders();
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
    `Contact:       ${[f.email, f.phone, f.location].filter(Boolean).join(" · ") || "(none)"}`,
    `Links:         ${[f.linkedin, f.github, f.website].filter(Boolean).join(" · ") || "(none)"}`,
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
  // Resume is required for first-time setup. In edit mode the prior resume
  // already lives on disk and a re-upload is optional, so skip the guard.
  if (i === 0 && !resumeInput.files[0] && !editMode) {
    showAction("Please choose a PDF resume to continue.", "error");
    return false;
  }
  actionMsg.hidden = true;
  return true;
}

// Prefill every field from a previously-submitted onboarding payload.
// Scalar inputs / selects: set .value. sites: tick matching checkboxes,
// untick the rest. include_easy_apply: set .checked.
function prefillForm(saved) {
  for (const [k, v] of Object.entries(saved)) {
    if (k === "sites" || k === "include_easy_apply") continue;
    if (v === undefined || v === null || v === "") continue;
    const el = form.querySelector(`[name="${k}"]`);
    if (el && el.tagName !== "FIELDSET") el.value = v;
  }
  const savedSites = saved.sites || [];
  form.querySelectorAll('input[name="sites"]').forEach((cb) => {
    cb.checked = savedSites.includes(cb.value);
  });
  const easyCb = form.querySelector('input[name="include_easy_apply"]');
  if (easyCb) easyCb.checked = !!saved.include_easy_apply;
}

function enterEditMode(hasResume) {
  editMode = true;
  // Resume optional: drop required, swap the hint, restate intent.
  resumeInput.removeAttribute("required");
  const resumeHint = document.querySelector('[data-step="0"] .hint');
  if (resumeHint && hasResume) {
    resumeHint.textContent =
      "Resume already on file. Upload a new PDF to replace it, or skip this step to keep the existing one.";
  }
  // API key optional: same idea — placeholder explains.
  const apiKeyEl = form.querySelector('input[name="api_key"]');
  if (apiKeyEl) {
    apiKeyEl.placeholder = "leave blank to keep your saved key";
  }
  // Submit button copy: "Save changes" reads better than "Write secrets"
  // for an edit, and signals this isn't a full re-onboarding.
  submitBtn.textContent = "Save changes";
}

nextBtn.addEventListener("click", () => {
  if (!validateStep(current)) return;
  // Leaving the cloud Provider step (5) → pre-fill Local eval (6) with the
  // same provider/model. If the user entered a key there, they almost certainly
  // want to use it locally too. editMode means the key field is blank but the
  // saved key is still active, so pre-fill in that case too.
  if (current === 5) {
    const cloudProvider = form.querySelector('[name="provider"]')?.value;
    const cloudModel    = form.querySelector('[name="batch_model"]')?.value;
    const apiKey        = form.querySelector('[name="api_key"]')?.value?.trim();
    const hasKey        = !!apiKey || editMode;
    if (cloudProvider && hasKey) {
      // Write the key (and provider) to .env now so the Local eval step detects
      // it as configured. Fire-and-forget — the re-detect in showStep(6) picks
      // up the result once the request completes.
      fetch("/api/onboard/local-config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          batch_provider: cloudProvider,
          batch_model:    cloudModel || "",
          batch_cli:      "",
          api_key:        apiKey || "",
        }),
      }).catch(() => {});
      // Pre-fill select immediately (options already populated by loadLocalProviders).
      const sel = document.getElementById("local-provider-select");
      const mod = document.getElementById("local-model-input");
      if (sel && !sel.value) sel.value = cloudProvider;
      if (mod && !mod.value && cloudModel) mod.value = cloudModel;
    }
  }
  showStep(current + 1);
});
backBtn.addEventListener("click", () => showStep(current - 1));

// When a resume is chosen, parse it and autofill the About fields. Only fills
// fields the user hasn't already typed into, so it never clobbers manual edits.
const ABOUT_FIELDS = ["name", "email", "phone", "location", "linkedin", "github", "website"];
const autofillNote = document.getElementById("about-autofill-note");

resumeInput.addEventListener("change", async () => {
  const file = resumeInput.files[0];
  if (!file) return;
  showAction("Reading your resume…", "");
  try {
    const fd = new FormData();
    fd.append("resume", file);
    const resp = await fetch("/api/onboard/parse-resume", { method: "POST", body: fd });
    const info = await resp.json();
    if (!resp.ok) throw new Error(info.detail || "could not read resume");
    let filled = 0;
    for (const k of ABOUT_FIELDS) {
      const el = form.querySelector(`[name="${k}"]`);
      if (el && info[k] && !el.value) { el.value = info[k]; filled++; }
    }
    if (autofillNote) {
      autofillNote.hidden = false;
      autofillNote.textContent = filled
        ? `Autofilled ${filled} field${filled === 1 ? "" : "s"} from your resume — review them in step 2 (About).`
        : "Couldn't auto-detect contact details; fill them in step 2 (About).";
    }
    showAction(
      filled ? `Resume loaded — autofilled ${filled} About field${filled === 1 ? "" : "s"}.` : "Resume loaded.",
      "ok"
    );
  } catch (e) {
    showAction(String(e.message || e), "error");
  }
});

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = collectForm();
  // In edit mode, both fields are optional — the server reuses the existing
  // resume.pdf on disk and the existing provider secret in GitHub.
  if (!editMode && !resumeInput.files[0]) {
    showAction("Resume PDF is required.", "error"); showStep(0); return;
  }
  if (!editMode && !f.api_key) {
    showAction("An API key is required to evaluate jobs.", "error"); showStep(5); return;
  }

  const fd = new FormData();
  // Only attach the resume when one is actually selected. The server treats
  // the missing field as "keep the existing resume on disk."
  if (resumeInput.files[0]) fd.append("resume", resumeInput.files[0]);
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
    }
    // Note: the "already configured" banner is set inside loadSavedConfig
    // (it knows whether we entered edit mode) so we don't double up here.
  } catch (err) {
    repoLine.textContent = `Could not read repo status: ${err.message}. ` +
      "Is gh installed and authenticated?";
  }
}

// If the user has onboarded before, prefill every form field from the saved
// payload so they only have to touch the knob they want to change. Sidecar
// excludes the API key (it lives in GitHub Secrets, write-only).
async function loadSavedConfig() {
  try {
    const resp = await fetch("/api/onboard/load-config");
    const { form: saved, has_resume } = await resp.json();
    if (!saved) return;
    prefillForm(saved);
    enterEditMode(!!has_resume);
    statusBanner.hidden = false;
    statusBanner.classList.add("ok-banner");
    statusBanner.textContent =
      "✓ Editing your existing config. Change what you need and click " +
      "Save changes — leave resume / API key blank to keep them as they are.";
  } catch {
    /* first-time setup: no sidecar, leave the wizard in its default state */
  }
}

// ── Local evaluation provider (step 6) ──────────────────────────────────────

async function loadLocalProviders() {
  const detection = document.getElementById("local-provider-detection");
  const select    = document.getElementById("local-provider-select");
  const cliSelect = document.getElementById("local-cli-select");
  const modelInput = document.getElementById("local-model-input");
  const modelHint  = document.getElementById("local-model-hint");
  const cliHint    = document.getElementById("local-cli-hint");
  if (!detection) return;
  try {
    const resp = await fetch("/api/onboard/providers");
    const d = await resp.json();

    // Build detection summary.
    const apiLines = d.api_providers.map((p) => {
      const tick = p.configured ? "✓" : "✗";
      return `<span class="${p.configured ? "ok-text" : "muted"}">${tick} ${p.name}${p.configured ? "" : " (no key)"}</span>`;
    });
    const cliLines = d.cli_tools.map((c) => {
      const tick = c.available ? "✓" : "✗";
      return `<span class="${c.available ? "ok-text" : "muted"}">${tick} ${c.name}</span>`;
    });
    detection.innerHTML =
      `<strong>API providers:</strong> ${apiLines.join(" &nbsp; ")} &nbsp;&nbsp; ` +
      `<strong>CLIs:</strong> ${cliLines.join(" &nbsp; ")}`;

    // Populate provider select — only configured ones enabled.
    select.innerHTML = '<option value="">— auto-detect (first available key) —</option>';
    for (const p of d.api_providers) {
      const opt = document.createElement("option");
      opt.value = p.name;
      opt.textContent = p.name + (p.configured ? " ✓" : " (no key)");
      if (!p.configured) opt.disabled = true;
      select.appendChild(opt);
    }
    if (d.current.batch_provider) select.value = d.current.batch_provider;

    // Current model.
    modelInput.value = d.current.batch_model || "";

    // Update model hint when provider changes.
    function updateModelHint() {
      const pName = select.value;
      const def = d.provider_defaults[pName] || "";
      modelHint.hidden = !def;
      modelHint.textContent = def ? `Default for ${pName}: ${def}` : "";
    }
    select.addEventListener("change", updateModelHint);
    updateModelHint();

    // Populate CLI select — mark unavailable ones.
    for (const opt of cliSelect.options) {
      const found = d.cli_tools.find((c) => c.name === opt.value);
      if (found && !found.available) opt.textContent = opt.value + " (not installed)";
    }
    if (d.current.batch_cli) cliSelect.value = d.current.batch_cli;

    // CLI hint.
    function updateCliHint() {
      const name = cliSelect.value;
      const found = d.cli_tools.find((c) => c.name === name);
      cliHint.textContent = found && !found.available
        ? `${name} is not on PATH — install it or pick an available CLI.`
        : "";
    }
    cliSelect.addEventListener("change", updateCliHint);
    updateCliHint();
  } catch (e) {
    detection.textContent = "Could not detect providers: " + (e.message || e);
  }
}

document.getElementById("save-local-btn")?.addEventListener("click", async () => {
  const btn     = document.getElementById("save-local-btn");
  const msgEl   = document.getElementById("local-save-msg");
  const provider = document.getElementById("local-provider-select").value;
  const model    = document.getElementById("local-model-input").value.trim();
  const cli      = document.getElementById("local-cli-select").value;
  btn.disabled = true;
  msgEl.hidden = true;
  try {
    const resp = await fetch("/api/onboard/local-config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ batch_provider: provider, batch_model: model, batch_cli: cli }),
    });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(body.detail || "save failed");
    msgEl.textContent = `Saved to .env (${body.updated.join(", ")}). Changes take effect immediately.`;
    msgEl.className = "action-msg ok";
  } catch (e) {
    msgEl.textContent = String(e.message || e);
    msgEl.className = "action-msg error";
  }
  msgEl.hidden = false;
  btn.disabled = false;
});

showStep(0);
loadStatus();
loadSavedConfig();
loadLocalProviders();
