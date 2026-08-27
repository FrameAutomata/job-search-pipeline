"use strict";

// Guided onboarding wizard. Collects a resume PDF + preferences, then POSTs
// multipart to /api/onboard, which generates the profile artifacts and writes
// them as GitHub secrets.

const STEP_TITLES = ["Resume", "About", "Roles", "Search", "Narrative", "Provider", "Local settings", "Review"];

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

// Voluntary self-ID (EEO) consent toggles — serialized as explicit "yes"/"no"
// (a bare checkbox is absent when unchecked, which collides with consent's
// default-on), and restored by .checked rather than .value.
const CONSENT_TOGGLES = ["data_processing_consent", "save_answers", "share_answers"];

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
  for (const name of CONSENT_TOGGLES) {
    obj[name] = form.querySelector(`input[name="${name}"]`).checked ? "yes" : "no";
  }
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
    `Work auth:     ${[f.citizenship, f.requires_sponsorship === "yes" && "needs sponsorship", f.work_auth_regions && `auth: ${f.work_auth_regions}`].filter(Boolean).join(" · ") || "(defaults)"}`,
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
    showAction("Please choose a resume (DOCX, ODT, or PDF) to continue.", "error");
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
    if (CONSENT_TOGGLES.includes(k)) continue;
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
  // Consent toggles: saved as "yes"/"no"; default consent on, save/share off.
  CONSENT_TOGGLES.forEach((name) => {
    const cb = form.querySelector(`input[name="${name}"]`);
    if (cb) cb.checked = name in saved ? saved[name] === "yes" : name === "data_processing_consent";
  });
}

function enterEditMode(hasResume) {
  editMode = true;
  // Resume optional: drop required, swap the hint, restate intent.
  resumeInput.removeAttribute("required");
  const resumeHint = document.querySelector('[data-step="0"] .hint');
  if (resumeHint && hasResume) {
    resumeHint.textContent =
      "Resume already on file. Upload a new DOCX, ODT, or PDF to replace it, or skip this step to keep the existing one.";
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

nextBtn.addEventListener("click", async () => {
  if (!validateStep(current)) return;
  // Leaving the cloud Provider step (5) → save the key to .env first, then
  // advance. Awaiting the save ensures loadLocalProviders() (called by
  // showStep(6)) sees the key in os.environ and shows it as configured.
  if (current === 5) {
    const cloudProvider = form.querySelector('[name="provider"]')?.value;
    const cloudModel    = form.querySelector('[name="batch_model"]')?.value;
    const apiKey        = form.querySelector('[name="api_key"]')?.value?.trim();
    const hasKey        = !!apiKey || editMode;
    if (cloudProvider && hasKey) {
      await fetch("/api/onboard/local-config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          batch_provider: cloudProvider,
          batch_model:    cloudModel || "",
          batch_cli:      "",
          api_key:        apiKey || "",
        }),
      }).catch(() => {});
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
// Fail OPEN on anything unexpected. Before the panel was hidden by default a
// broken endpoint could not remove it; now a 500, a non-JSON body or a thrown
// fetch would leave the user with no path to Reset and no error saying why.
// Showing it costs nothing — the button still needs RESET typed — while hiding
// it wrongly is unrecoverable from the UI.
function revealDangerZone(hasState) {
  const danger = document.getElementById("danger-zone");
  if (danger && hasState !== false) danger.hidden = false;
}

async function loadSavedConfig() {
  try {
    const resp = await fetch("/api/onboard/load-config");
    const { form: saved, has_resume, has_state } = await resp.json();

    // Gate the reset panel on has_state — job-search RESULTS exist — not on
    // setup. `saved` misses a CLI-set-up copy, and `has_resume` both
    // false-reveals (a hand-dropped resume before the first run: nothing to
    // reset, which is the exposure this gate exists to prevent) and
    // false-hides (`run-ui.sh --data` against an extracted artifact: a full
    // tracker with no local resume).
    revealDangerZone(has_state);

    if (!saved) return;
    prefillForm(saved);
    enterEditMode(!!has_resume);
    statusBanner.hidden = false;
    statusBanner.classList.add("ok-banner");
    statusBanner.textContent =
      "✓ Editing your existing config. Change what you need and click " +
      "Save changes — leave resume / API key blank to keep them as they are.";
  } catch {
    // Not necessarily first-time setup any more — the panel's default state is
    // now "absent", so a failed probe must not read as "nothing to reset".
    revealDangerZone(undefined);
  }
}

// ── Local settings — .env (step 6): eval provider, tailoring, handoff folder ──

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

    // Tailoring provider/model (optional — blank inherits the eval provider/model).
    // Every provider is selectable (a key can be supplied below), so none disabled.
    const tailorSelect = document.getElementById("local-tailor-provider");
    const tailorModel = document.getElementById("local-tailor-model");
    if (tailorSelect) {
      tailorSelect.innerHTML = '<option value="">— same as evaluation —</option>';
      for (const p of d.api_providers) {
        const opt = document.createElement("option");
        opt.value = p.name;
        opt.textContent = p.name + (p.configured ? " ✓" : " (needs key)");
        tailorSelect.appendChild(opt);
      }
      if (d.current.tailor_provider) tailorSelect.value = d.current.tailor_provider;
    }
    if (tailorModel) tailorModel.value = d.current.tailor_model || "";

    const handoffDirInput = document.getElementById("local-handoff-dir");
    if (handoffDirInput) handoffDirInput.value = d.current.handoff_out_dir || "";

    // Current Gemini free-tier opt-in.
    const freeTierCb = document.getElementById("gemini-free-tier");
    const freeTierRow = document.getElementById("gemini-free-tier-row");
    if (freeTierCb) freeTierCb.checked = !!d.current.gemini_free_tier;

    // Update model hint + the Gemini-only free-tier checkbox when provider changes.
    function updateModelHint() {
      const pName = select.value;
      const def = d.provider_defaults[pName] || "";
      modelHint.hidden = !def;
      modelHint.textContent = def ? `Default for ${pName}: ${def}` : "";
      // Gemini-only — show for Gemini and auto-detect (which may resolve to Gemini),
      // hide only when an explicit non-Gemini provider is chosen.
      if (freeTierRow) freeTierRow.hidden = pName !== "" && pName !== "gemini";
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
  // Gemini-only: don't persist the flag for an explicit non-Gemini provider
  // (it'd be a no-op anyway). Allowed for Gemini + auto-detect.
  const geminiFreeTier = (provider === "" || provider === "gemini")
    && document.getElementById("gemini-free-tier").checked;
  const apiKey         = document.getElementById("local-api-key")?.value || "";
  const tailorProvider = document.getElementById("local-tailor-provider")?.value || "";
  const tailorModel    = document.getElementById("local-tailor-model")?.value.trim() || "";
  const tailorKey      = document.getElementById("local-tailor-key")?.value || "";
  const handoffDir     = document.getElementById("local-handoff-dir")?.value.trim() || "";
  btn.disabled = true;
  msgEl.hidden = true;
  try {
    const resp = await fetch("/api/onboard/local-config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ batch_provider: provider, batch_model: model, batch_cli: cli,
                             api_key: apiKey,
                             gemini_free_tier: geminiFreeTier,
                             tailor_provider: tailorProvider, tailor_model: tailorModel,
                             tailor_api_key: tailorKey,
                             handoff_out_dir: handoffDir }),
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

// "Browse…" beside the handoff-folder field: the server pops a native OS folder
// dialog (this UI is local) and returns the chosen path — browsers can't hand a
// page a folder's real path. Falls back to typing if no picker is available.
document.getElementById("local-handoff-browse")?.addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  const input = document.getElementById("local-handoff-dir");
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Opening…";
  try {
    const resp = await fetch("/api/onboard/pick-folder", { method: "POST" });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(body.detail || "picker unavailable");
    if (body.path) input.value = body.path;   // "" = cancelled → leave the field as-is
  } catch {
    input.placeholder = "couldn't open a folder dialog — type the path here";
    input.focus();
  } finally {
    btn.disabled = false;
    btn.textContent = orig;
  }
});

// Danger zone: start over. Double-gated (a typed "RESET") since it's destructive
// and the cloud-cache deletion is irreversible.
const resetBtn = document.getElementById("reset-btn");
resetBtn.addEventListener("click", async () => {
  // Read the checkbox BEFORE prompting, and warn on BOTH paths — they fail in
  // opposite directions. Checked: the cloud cache deletion cannot be undone.
  // Unchecked: the cloud keeps its copy, so the next Refresh or daily run
  // merges this history straight back and the reset undoes itself (see
  // reset.py). A prompt that only covered one of those left the other silent.
  const clearCloud = document.getElementById("reset-clear-cloud").checked;
  const typed = prompt(
    "This wipes your job-search results (tracker, history, reports, queue, PDFs). " +
    "Your setup is kept and a snapshot is saved first.\n\n" +
    (clearCloud
      ? "The cloud state cache is also deleted — that part is IRREVERSIBLE."
      : "The cloud keeps its copy: the next Refresh or daily run will pull this " +
        "history back into the tracker.") +
    "\n\nType RESET to confirm:");
  if (typed !== "RESET") return;
  const msg = document.getElementById("reset-msg");
  const show = (text, kind) => { msg.hidden = false; msg.textContent = text; msg.className = "action-msg" + (kind ? " " + kind : ""); };
  resetBtn.disabled = true;
  show("Resetting…", "");
  try {
    const resp = await fetch("/api/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: "RESET", clear_cloud: clearCloud }),
    });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(body.detail || `reset failed (${resp.status})`);
    let txt = `Reset complete — wiped ${body.count} item(s); snapshot saved under .ui-cache/backups/.`;
    if (body.cloud) txt += ` Cleared ${body.cloud.deleted.length} cloud cache(s).`;
    if (body.cloud_error) txt += ` (Cloud not cleared: ${body.cloud_error})`;
    show(txt, "ok");
  } catch (e) {
    show(String(e.message || e), "error");
  } finally {
    resetBtn.disabled = false;
  }
});

showStep(0);
loadStatus();
loadSavedConfig();
loadLocalProviders();
