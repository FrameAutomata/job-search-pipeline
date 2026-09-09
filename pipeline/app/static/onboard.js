"use strict";

// Guided onboarding wizard. Collects a resume PDF + preferences, then POSTs
// multipart to /api/onboard, which generates the profile artifacts and writes
// them as GitHub secrets.

const STEP_TITLES = ["Resume", "About", "Roles", "Search", "Narrative", "Provider", "Local settings", "Review"];

// BATCH_MODEL may be a comma-separated failover chain (tried in order on
// overload). Python's gemini_limits._spec_models is the same split; these two
// read the field in the prefill and the save handler, which had drifted into
// separate copies of the same expression.
const leadModel = (spec) => (spec || "").split(",")[0].trim();
const isChain = (spec) => (spec || "").split(",").filter((m) => m.trim()).length > 1;

// The model an empty BATCH_MODEL box resolves to. Set when /api/onboard/providers
// loads, and read by BOTH the limits prefill and the save handler — they used to
// apply this fallback in only one of the two places, so limits typed against the
// shown default were dropped while the UI still reported "Saved".
let geminiDefaultModel = "";
// What a blank BATCH_MODEL resolves to in the CLOUD on Gemini: the server
// computes it from the limits table (/api/onboard/providers), because the
// provider default is a ~20-requests-a-day row on a free key and a day's worth
// of roles would exhaust it. Shown in the model field's hint and used as the
// model the rate-limit boxes describe when the field is left blank.
let geminiFreeTierPick = "";
// The model the three rate-limit boxes were last seeded for — see showLimits.
let limitsShownModel = null;

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
// Three flags, because the wizard used to ask one question (is there a sidecar?)
// where it needed three, and answered all of them "no" for any copy set up
// outside this wizard — no prefill, and a step-0 resume gate nothing could
// satisfy, which walled off every later step (#145).
//
// "Edit" when this copy is configured BY ANY ROUTE (/api/onboard/load-config's
// `configured`): prefill, an "already set up" banner, and "Save changes" on the
// submit button instead of "Write secrets".
let editMode = false;
// A resume is already on disk, so step 0's upload is optional. Real state, not
// a proxy for it: a CLI-configured copy has one and the pipeline uses it.
let resumeOnFile = false;
// A provider key is already a GitHub secret, so the API-key field is optional.
// `null` = we couldn't ask (gh missing or unauthenticated) — see apiKeyRequired.
let providerKeyOnFile = null;
// Per-pass settings in the current search.yml that Save would drop, since it
// rewrites `searches:` from the Search step's fields. Empty for any config this
// wizard wrote; non-empty only for a hand-written one, which could not reach
// this screen before. Shown on Search AND on Review — Review is the last thing
// between the user and the flattening.
let searchDetailAtRisk = [];

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

// Two banners compete for the one slot, written by loaders that race. The
// PUBLIC-repo warning wins and is sticky: onboarding refuses to write secrets to
// a public repo, so burying it under "✓ Editing your existing config" leaves
// nothing on screen to explain the refusal when Save fails. The race was
// theoretical while edit mode meant "has submitted this wizard before"; every
// configured copy enters it now (#145).
let publicRepoWarned = false;

function showBanner(text, kind) {
  if (publicRepoWarned && kind !== "warn") return;
  if (kind === "warn") publicRepoWarned = true;
  statusBanner.textContent = text;
  statusBanner.classList.toggle("ok-banner", kind === "ok");
  statusBanner.hidden = false;
}

// Voluntary self-ID (EEO) consent toggles — serialized as explicit "yes"/"no"
// (a bare checkbox is absent when unchecked, which collides with consent's
// default-on), and restored by .checked rather than .value.
const CONSENT_TOGGLES = ["data_processing_consent", "save_answers", "share_answers"];

// The two Provider-step checkboxes that write a repository VARIABLE, serialized
// the same explicit way and for the same reason: an unchecked box is simply
// absent from FormData, which the server would read as "the form never
// mentioned it" and leave the variable as it is — so unticking either in edit
// mode would silently do nothing. Both default ON, so "no" is the answer that
// has to survive the trip.
const VARIABLE_TOGGLES = ["gemini_free_tier", "auto_update_weekly"];

// ── Gemini free-tier limits ──────────────────────────────────────────────────
// The checkbox and the three AI Studio boxes live on the Provider step, beside
// the model they describe, and one read of them feeds three writers: the
// repository variables GEMINI_FREE_TIER / GEMINI_LIMITS_JSON (the wizard's
// submit), and .env + config/gemini-limits.json (the local-config save as the
// user leaves that step). Reading them in one place is what keeps the cloud and
// the local run conforming to the same numbers.

// Is the opt-in on AND applicable? The row is hidden for an explicit non-Gemini
// provider, where the flag would be a no-op, and a hidden box must not be read
// as an answer.
function geminiFreeTierOn() {
  const row = document.getElementById("gemini-free-tier-row");
  const cb = form.querySelector('input[name="gemini_free_tier"]');
  return !!(cb && cb.checked && row && !row.hidden);
}

// The model the boxes describe: whatever is in the cloud model field, else what
// a blank field resolves to. Chains are edited one model at a time (lead only).
function geminiLimitsModel() {
  const field = form.querySelector('[name="batch_model"]');
  return leadModel((field && field.value) || geminiFreeTierPick || geminiDefaultModel);
}

// {} = nothing to say; {limits} = a payload to send; {error} = a half-filled row.
// RPM and RPD are the pair pacing and the daily cap need, so one without the
// other is an error the user can see rather than a silent drop. A blank TPM
// OMITS the key: sending null would read as "unlimited" and merge over the
// built-in number, turning the token budget off through the only UI it has.
function readGeminiLimits() {
  if (!geminiFreeTierOn()) return {};
  const model = geminiLimitsModel();
  if (!model) return {};
  const num = (id) => {
    const raw = document.getElementById(id)?.value.trim();
    return raw === "" || raw === undefined ? null : Number(raw);
  };
  const rpm = num("gemini-rpm"), tpm = num("gemini-tpm"), rpd = num("gemini-rpd");
  if (rpm === null && tpm === null && rpd === null) {
    return { limits: { [model]: null } };   // cleared → back to the built-in table
  }
  if (rpm === null || rpd === null) {
    return { error: "Enter both RPM and RPD (TPM may be blank to keep the built-in value)." };
  }
  const row = { rpm, rpd };
  if (tpm !== null) row.tpm = tpm;
  return { limits: { [model]: row } };
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
  for (const name of [...CONSENT_TOGGLES, ...VARIABLE_TOGGLES]) {
    const cb = form.querySelector(`input[name="${name}"]`);
    if (cb) obj[name] = cb.checked ? "yes" : "no";
  }
  // The AI Studio numbers ride with the submit so the cloud's GEMINI_LIMITS_JSON
  // and the local override file are written from one set of boxes.
  const limits = readGeminiLimits();
  if (limits.limits) obj.gemini_limits = limits.limits;
  return obj;
}

// Is the API key still required? Optional once one is on the repo — but when we
// couldn't read the repo's secrets at all, fall back to the old rule (a
// configured copy keeps whatever it has) rather than force a re-paste over a
// question we were unable to ask.
function apiKeyRequired() {
  return providerKeyOnFile === null ? !editMode : !providerKeyOnFile;
}

// Placeholder + review copy follow whichever of the two answers is in effect.
// Called from both loaders, so it doesn't matter which resolves first.
function refreshApiKeyHint() {
  const el = form.querySelector('input[name="api_key"]');
  if (el) {
    el.placeholder = apiKeyRequired()
      ? "paste your key"
      : "leave blank to keep your saved key";
  }
}

// Name what Save would drop, beside the fields that would do the dropping.
function showSearchDetailWarning(items) {
  searchDetailAtRisk = items;
  const el = document.getElementById("search-detail-warning");
  if (!el || !items.length) return;
  el.hidden = false;
  el.textContent =
    "⚠ Your config/search.yml sets things these fields can't hold: " +
    items.join("; ") + ". Saving rewrites the search passes from this step, " +
    "so those would be lost — edit search.yml directly to keep them.";
}

function renderReview() {
  const f = collectForm();
  const file = resumeInput.files[0];
  // Hoisted: the alignment padding in the template strings below is load-bearing
  // (the review renders as monospace textContent), so branching logic doesn't
  // belong interleaved with it.
  const resumeLabel = file ? file.name
    : resumeOnFile ? "(keeping the one on file)" : "(none selected!)";
  const keyLabel = f.api_key
    ? "•".repeat(Math.min(12, f.api_key.length)) + " (will be written)"
    : apiKeyRequired() ? "(none — required)" : "(keeping your saved key)";
  // The Provider step's free-tier answer and the digest channels are the two
  // things a user can leave configured without seeing them again, so Review is
  // where they are restated — a webhook typed into a password field and never
  // echoed is otherwise unverifiable before Save.
  const freeTierLabel = f.provider !== "gemini" ? "(n/a — not Gemini)"
    : f.gemini_free_tier === "no" ? "no — full-rate requests"
    : "yes — pace and cap to the free-tier limits";
  const channels = [];
  if (f.digest_discord_webhook) channels.push("Discord (new webhook)");
  if (f.digest_email_to) channels.push(`email → ${f.digest_email_to}`);
  const digestLabel = channels.length ? channels.join(" · ")
    : "(unchanged — whatever this repo already has)";
  // The two Local-step selects have no form name (they save to .env on their
  // own button), so Review reads them from the DOM.
  const cliSelectEl = document.getElementById("local-cli-select");
  const cliLabel = cliSelectEl
    ? (cliSelectEl.selectedOptions[0]?.textContent || cliSelectEl.value) : "(default)";
  const policyLabel = document.getElementById("local-submit-policy")?.value || "(default)";
  const lines = [
    `Resume:        ${resumeLabel}`,
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
    `API key:       ${keyLabel}`,
    `Free tier:     ${freeTierLabel}`,
    `Daily digest:  ${digestLabel}`,
    `Weekly update: ${f.auto_update_weekly === "no" ? "no" : "yes"}`,
    `Agent CLI:     ${cliLabel}`,
    `At Submit:     ${policyLabel}`,
  ];
  // Review is the last screen before Save rewrites `searches:`, so the warning
  // has to be here too — not only on a step the user may never open.
  if (searchDetailAtRisk.length) {
    lines.push("", "⚠ Saving replaces your search passes, dropping: "
                   + searchDetailAtRisk.join("; "));
  }
  reviewEl.textContent = lines.join("\n");
}

// Light per-step validation before advancing.
function validateStep(i) {
  // A resume is required only when there isn't one already — asked of the disk,
  // not of whether this wizard has been submitted before.
  if (i === 0 && !resumeInput.files[0] && !resumeOnFile) {
    showAction("Please choose a resume (DOCX, ODT, or PDF) to continue.", "error");
    return false;
  }
  actionMsg.hidden = true;
  return true;
}

// Prefill from what this copy is configured with (the server merges the wizard's
// sidecar under the real config files). Scalar inputs / selects: set .value.
// sites: tick matching checkboxes, untick the rest. include_easy_apply: .checked.
//
// The two group fields are applied only when the payload carries them. The
// source used to be a whole form submit, which always did; a prefill assembled
// from the files may cover profile.yml's half and not search.yml's, and
// "absent" must leave the HTML defaults (both boards ticked) rather than clear
// every board.
function prefillForm(saved) {
  for (const [k, v] of Object.entries(saved)) {
    if (k === "sites" || k === "include_easy_apply") continue;
    if (CONSENT_TOGGLES.includes(k)) continue;
    if (VARIABLE_TOGGLES.includes(k)) continue;
    if (v === undefined || v === null || v === "") continue;
    const el = form.querySelector(`[name="${k}"]`);
    if (el && el.tagName !== "FIELDSET") el.value = v;
  }
  if (Array.isArray(saved.sites)) {
    form.querySelectorAll('input[name="sites"]').forEach((cb) => {
      cb.checked = saved.sites.includes(cb.value);
    });
  }
  const easyCb = form.querySelector('input[name="include_easy_apply"]');
  if (easyCb && "include_easy_apply" in saved) easyCb.checked = !!saved.include_easy_apply;
  // Consent toggles: saved as "yes"/"no"; default consent on, save/share off.
  CONSENT_TOGGLES.forEach((name) => {
    const cb = form.querySelector(`input[name="${name}"]`);
    if (cb) cb.checked = name in saved ? saved[name] === "yes" : name === "data_processing_consent";
  });
  // Both default ON in the markup, so an absent answer keeps that default —
  // only an explicit "no" from a previous submit unticks them.
  VARIABLE_TOGGLES.forEach((name) => {
    const cb = form.querySelector(`input[name="${name}"]`);
    if (cb && name in saved) cb.checked = saved[name] === "yes";
  });
}

// A resume is already on disk: drop `required`, swap the hint, and let step 0
// be walked past. Separate from edit mode because the disk answers it — the
// wizard's own history doesn't.
function allowExistingResume() {
  resumeOnFile = true;
  resumeInput.removeAttribute("required");
  const resumeHint = document.querySelector('[data-step="0"] .hint');
  if (resumeHint) {
    resumeHint.textContent =
      "Resume already on file. Upload a new DOCX, ODT, or PDF to replace it, or skip this step to keep the existing one.";
  }
}

function enterEditMode() {
  editMode = true;
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
    const hasKey        = !!apiKey || !apiKeyRequired();
    const limits        = readGeminiLimits();
    if (limits.error) { showAction(limits.error, "error"); return; }
    if (cloudProvider && hasKey) {
      await fetch("/api/onboard/local-config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          batch_provider: cloudProvider,
          batch_model:    cloudModel || "",
          batch_cli:      "",
          api_key:        apiKey || "",
          // The free-tier answers belong to THIS step, so this is where they
          // reach .env and config/gemini-limits.json — the same values the
          // submit writes as repository variables, so a local run and the
          // cloud daily conform to one set of numbers.
          gemini_free_tier: geminiFreeTierOn(),
          gemini_limits:    limits.limits,
          // Not this step's field, but a blank one UNSETS the key: carry the
          // Local step's current selection through rather than clear it on the
          // way past.
          handoff_submit_policy:
            document.getElementById("local-submit-policy")?.value || "",
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
  // Each field is optional exactly when the thing it supplies already exists —
  // a resume on disk (the server extracts it), a provider secret on the repo.
  if (!resumeOnFile && !resumeInput.files[0]) {
    showAction("A resume is required (DOCX, ODT, or PDF).", "error"); showStep(0); return;
  }
  if (apiKeyRequired() && !f.api_key) {
    showAction("An API key is required to evaluate jobs.", "error"); showStep(5); return;
  }
  const limitsCheck = readGeminiLimits();
  if (limitsCheck.error) { showAction(limitsCheck.error, "error"); showStep(5); return; }

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

// Which digest channels the repo already has, by secret. Secrets are
// write-only, so this is the only way an edit-mode visit can say "you already
// have this" — without it the blank webhook field reads as "not set up" and the
// user pastes it again every visit.
function showDigestStatus(s) {
  const el = document.getElementById("digest-status");
  if (!el) return;
  const have = [];
  if (s.has_digest_discord) have.push("Discord");
  if (s.has_digest_email) have.push("email");
  el.hidden = false;
  el.textContent = have.length
    ? `${have.join(" + ")} digest: configured — leave the fields blank to keep it.`
    : "No digest configured yet — either channel below is enough.";
}

// Repository VARIABLES are readable, so edit mode prefills the digest
// thresholds and the two checkboxes from what the cloud actually runs on.
// Once, from loadStatus: the fields are user-editable and re-applying them
// later would undo typing. Absent names leave the markup defaults alone,
// which is what makes "checked unless you said otherwise" hold.
let cloudVariablesApplied = false;
function prefillCloudVariables(vars) {
  if (cloudVariablesApplied) return;
  cloudVariablesApplied = true;
  const put = (field, name) => {
    const el = form.querySelector(`[name="${field}"]`);
    if (el && vars[name] !== undefined && vars[name] !== "") el.value = vars[name];
  };
  put("digest_min_score", "DIGEST_MIN_SCORE");
  put("digest_limit", "DIGEST_LIMIT");
  const tick = (field, name, on) => {
    const cb = form.querySelector(`input[name="${field}"]`);
    if (cb && vars[name] !== undefined) cb.checked = on(vars[name]);
  };
  // update-from-template.yml's schedule proceeds only on the exact string
  // "true", so that is the only value that reads as checked.
  tick("auto_update_weekly", "AUTO_UPDATE_FROM_TEMPLATE", (v) => v === "true");
  tick("gemini_free_tier", "GEMINI_FREE_TIER", (v) => v === "true");
}

// Load repo status up front so the user knows where secrets will go (and whether
// the repo is private).
async function loadStatus() {
  try {
    const resp = await fetch("/api/onboard/status");
    const s = await resp.json();
    if (!resp.ok) throw new Error(s.detail || "could not read repo status");
    repoLine.textContent = `Target repo: ${s.repo} (${s.visibility})`;
    reviewRepo.textContent = s.repo;
    providerKeyOnFile = !!s.has_provider;
    refreshApiKeyHint();
    showDigestStatus(s);
    prefillCloudVariables(s.variables || {});
    if (s.visibility === "PUBLIC") {
      showBanner(
        "⚠ This repo is PUBLIC. Make your fork private before onboarding — " +
        "onboarding will refuse to write secrets to a public repo.", "warn");
    }
    // Note: the "already configured" banner is set inside loadSavedConfig
    // (it knows whether we entered edit mode) so we don't double up here.
  } catch (err) {
    repoLine.textContent = `Could not read repo status: ${err.message}. ` +
      "Is gh installed and authenticated?";
  }
}

// If this copy is already configured, prefill every form field from what it is
// configured WITH so the user only has to touch the knob they want to change.
// The API key is never prefilled (it lives in GitHub Secrets, write-only).
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
    const { form: saved, configured, has_resume, has_state,
            search_detail_at_risk } = await resp.json();

    // Gate the reset panel on has_state — job-search RESULTS exist — not on
    // setup. `saved` misses a CLI-set-up copy, and `has_resume` both
    // false-reveals (a hand-dropped resume before the first run: nothing to
    // reset, which is the exposure this gate exists to prevent) and
    // false-hides (`run-ui.sh --data` against an extracted artifact: a full
    // tracker with no local resume).
    revealDangerZone(has_state);

    // Each of these answers its own question, so each is applied on its own
    // terms — a resume with no config still opens step 0, and a config with no
    // resume still prefills. Nothing configured means nothing to prefill: the
    // server sends form: null there (config/search.yml is the example until the
    // user answers), and the early return keeps the banner off too.
    if (has_resume) allowExistingResume();
    if (!configured) return;
    if (saved) prefillForm(saved);
    showSearchDetailWarning(search_detail_at_risk || []);
    enterEditMode();
    refreshApiKeyHint();
    // The banner is a CLAIM about the fields, so it has to match them. This copy
    // is set up, but if nothing could be read back — an unparseable profile.yml,
    // say — the form is blank, and "the fields show what's in effect now" over a
    // blank form is the #145 screenshot with a banner denying it.
    showBanner(saved
      ? "✓ Editing your existing config — the fields show what's in effect now. " +
        "Change what you need and click Save changes; leave resume / API key " +
        "blank to keep them as they are."
      : "✓ This copy is already set up, but none of its settings could be read " +
        "back — check config/search.yml and career-ops/config/profile.yml. The " +
        "fields below are blank and will be saved as shown.", "ok");
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
    // Only an explicit "on" in .env is applied. `current.gemini_free_tier` is a
    // bool, so it cannot tell "the key says false" from "there is no key", and
    // the box is checked in the markup on purpose — an unpaced free key 429s on
    // its first busy run. An explicit OFF comes back through the sidecar or the
    // repository variable, both of which do carry the distinction.
    if (freeTierCb && d.current.gemini_free_tier) freeTierCb.checked = true;

    // Rate limits for whichever model is in the box. Re-read on every model
    // change: the numbers are per-model, so fields left showing the previous
    // model's values would be saved against the new one.
    const limitEls = {
      rpm: document.getElementById("gemini-rpm"),
      tpm: document.getElementById("gemini-tpm"),
      rpd: document.getElementById("gemini-rpd"),
    };
    const limitStatus = document.getElementById("gemini-limits-status");
    const cloudProvider = form.querySelector('[name="provider"]');
    const cloudModel    = form.querySelector('[name="batch_model"]');
    const cloudHint     = document.getElementById("cloud-model-hint");
    geminiDefaultModel = d.provider_defaults.gemini || "";
    geminiFreeTierPick = d.free_tier_recommendation || "";

    function showLimits() {
      if (!limitEls.rpm) return;
      // The boxes sit beside the CLOUD model field now, so that field names the
      // model they describe (blank → what a blank field resolves to).
      const m = geminiLimitsModel();
      const mine = (d.current.gemini_limits_user || {})[m];
      const eff = (d.current.gemini_limits || {})[m];
      // VALUES come only from the user's own row; the baked numbers are shown as
      // PLACEHOLDERS. Prefilling them as values made "just press Save" write a
      // frozen copy of the built-in table into the override file — which then
      // shadows every future template update to those numbers, silently, and
      // made onboard.html's "leave all three blank" instruction unreachable.
      // Only when the model they describe changed: loadLocalProviders re-runs
      // every time the Local step is opened, and re-seeding here would wipe
      // numbers the user typed on the Provider step on the way past.
      if (limitsShownModel !== m) {
        limitsShownModel = m;
        limitEls.rpm.value = mine?.rpm ?? "";
        limitEls.tpm.value = mine?.tpm ?? "";
        limitEls.rpd.value = mine?.rpd ?? "";
      }
      limitEls.rpm.placeholder = eff?.rpm ?? "e.g. 15";
      limitEls.tpm.placeholder = eff?.tpm ?? "e.g. 250000";
      limitEls.rpd.placeholder = eff?.rpd ?? "e.g. 1000";
      if (!limitStatus) return;
      if (!m) {
        limitStatus.textContent = "Pick a model to set its limits.";
      } else if (mine) {
        limitStatus.textContent = `${m}: using your saved numbers.`;
      } else if (eff) {
        limitStatus.textContent = `${m}: using the built-in fallback — override it below.`;
      } else {
        limitStatus.textContent =
          `${m} has no known limits, so pacing and the daily cap can't apply. Enter them below.`;
      }
      // BATCH_MODEL accepts a comma-separated failover chain whose daily
      // capacity is the SUM across members, but these three fields edit one
      // model. Say so, rather than let a chain user think they've covered it.
      if (isChain(cloudModel && cloudModel.value)) {
        limitStatus.textContent +=
          " (Chain detected — these fields edit the first model only; add the others"
          + " to config/gemini-limits.json by hand.)";
      }
    }

    // The cloud Provider step owns the free-tier row and the model placeholder.
    // A blank model on Gemini is not the provider default — the server computes
    // the free tier's best row — so the hint has to say which, or the user reads
    // "leave blank for provider default" and gets something else.
    function updateCloudProvider() {
      const pName = cloudProvider ? cloudProvider.value : "";
      const row = document.getElementById("gemini-free-tier-row");
      if (row) row.hidden = pName !== "gemini";
      if (cloudHint) {
        const isGemini = pName === "gemini";
        const rec = geminiFreeTierPick;
        cloudHint.hidden = !(isGemini && rec);
        cloudHint.textContent = isGemini && rec
          ? `Blank = ${rec} on Gemini's free tier (the highest-capacity row your limits allow); `
            + "other providers use their own default."
          : "";
      }
      showLimits();
    }
    if (cloudProvider) cloudProvider.addEventListener("change", updateCloudProvider);
    if (cloudModel) cloudModel.addEventListener("input", showLimits);
    updateCloudProvider();

    // Update the LOCAL model hint when the local provider changes.
    function updateModelHint() {
      const pName = select.value;
      const def = d.provider_defaults[pName] || "";
      modelHint.hidden = !def;
      modelHint.textContent = def ? `Default for ${pName}: ${def}` : "";
    }
    select.addEventListener("change", updateModelHint);
    updateModelHint();

    // The CLI select is RENDERED from the registry (id, label, tier, installed)
    // rather than relabelled in place: the tier is what the choice turns on, and
    // free-first order is the registry's, not the markup's. The static options
    // in onboard.html stay as the no-JS fallback and as the drift guard's
    // subject (tests/test_app_onboard.py::TestOnboardHtmlAgentClis).
    if (Array.isArray(d.cli_tools) && d.cli_tools.length) {
      cliSelect.innerHTML = "";
      for (const c of d.cli_tools) {
        const opt = document.createElement("option");
        opt.value = c.id || c.name;
        opt.textContent = `${c.label || opt.value} — ${c.tier || "?"}`
          + (c.installed ? " ✓ installed" : " (not installed)");
        cliSelect.appendChild(opt);
      }
    }
    cliSelect.value = d.current.batch_cli
      || (d.cli_tools.find((c) => c.default) || {}).id
      || cliSelect.value;

    // The hint is the registry's own tier note, verbatim — it is where the free
    // tier's real cost lives (daily/weekly quota, data use) — plus the install
    // line when the CLI isn't on PATH.
    function updateCliHint() {
      const found = d.cli_tools.find((c) => (c.id || c.name) === cliSelect.value);
      if (!found) { cliHint.textContent = ""; return; }
      const parts = [];
      if (found.tier_note) parts.push(found.tier_note);
      if (!found.installed && found.install_hint) {
        parts.push(`Not on PATH — install it: ${found.install_hint}`);
      }
      cliHint.textContent = parts.join(" ");
    }
    cliSelect.addEventListener("change", updateCliHint);
    updateCliHint();

    // Submit policy: options and glosses are the server's copy of
    // handoff.SUBMIT_POLICIES, so the wizard can't offer a policy a run would
    // reject. The static options stay as the no-JS fallback and the guard's
    // subject (TestOnboardHtmlSubmitPolicy).
    const policySelect = document.getElementById("local-submit-policy");
    const policyHint   = document.getElementById("local-submit-policy-hint");
    if (policySelect && Array.isArray(d.submit_policies) && d.submit_policies.length) {
      const chosen = d.current.handoff_submit_policy
        || (d.submit_policies.find((p) => p.default) || {}).id || "";
      policySelect.innerHTML = "";
      for (const p of d.submit_policies) {
        const opt = document.createElement("option");
        opt.value = p.id;
        opt.textContent = p.id + (p.default ? " (default)" : "");
        policySelect.appendChild(opt);
      }
      if (chosen) policySelect.value = chosen;
      const showPolicy = () => {
        const found = d.submit_policies.find((p) => p.id === policySelect.value);
        if (policyHint) policyHint.textContent = found ? found.gloss : "";
      };
      policySelect.addEventListener("change", showPolicy);
      showPolicy();
    }
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
  const policy   = document.getElementById("local-submit-policy")?.value || "";
  const apiKey         = document.getElementById("local-api-key")?.value || "";
  const tailorProvider = document.getElementById("local-tailor-provider")?.value || "";
  const tailorModel    = document.getElementById("local-tailor-model")?.value.trim() || "";
  const tailorKey      = document.getElementById("local-tailor-key")?.value || "";
  const handoffDir     = document.getElementById("local-handoff-dir")?.value.trim() || "";

  // The Gemini free-tier answers are NOT sent from here. The checkbox and the
  // three rate-limit boxes moved to the Provider step, which posts them as the
  // user leaves it; omitting both fields is what tells the server to leave
  // GEMINI_FREE_TIER and config/gemini-limits.json exactly as that step wrote
  // them, rather than unsetting a flag this step never showed.
  btn.disabled = true;
  msgEl.hidden = true;
  try {
    const resp = await fetch("/api/onboard/local-config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ batch_provider: provider, batch_model: model, batch_cli: cli,
                             api_key: apiKey,
                             tailor_provider: tailorProvider, tailor_model: tailorModel,
                             tailor_api_key: tailorKey,
                             handoff_out_dir: handoffDir,
                             handoff_submit_policy: policy }),
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

// "Register browser bridge": tells the selected agent CLI where the Playwright
// MCP server is, which is what lets it drive a real browser. Safe to press
// before the CLI is installed — the registry answers with the install hint
// instead of failing, so the button is also how you find out you need one.
document.getElementById("register-bridge-btn")?.addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  const msg = document.getElementById("register-bridge-msg");
  const cli = document.getElementById("local-cli-select")?.value || "";
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Registering…";
  try {
    const resp = await fetch("/api/agent-cli/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cli }),
    });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(body.detail || `registration failed (${resp.status})`);
    if (msg) msg.textContent = body.message || "Registered.";
  } catch (err) {
    if (msg) msg.textContent = String(err.message || err);
  } finally {
    btn.disabled = false;
    btn.textContent = orig;
  }
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
