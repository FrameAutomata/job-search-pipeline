# job-search-pipeline

Automated end-to-end job search orchestrator. Scrapes LinkedIn/Indeed/Glassdoor via **JobSpy**, scores results against a resume using YAKE keyword extraction, optionally screens for liveness, then bridges surviving jobs into **career-ops** for AI-powered evaluation. Supports interactive evaluation via any agent CLI (`--batch`) or synchronous parallel API evaluation across any LLM provider (`--evaluate-batch`).

**Applying is not a pipeline stage.** The pipeline finds and evaluates roles; the terminal `--handoff` stage — or the UI's **🤝 Hand off** button (batch) and per-role **Hand off** prompt in the report pane — emits an agent-agnostic **work-order per job site** (`next-roles-<site>.jsonl` + `.md` — one session per site the scraper searches from, written to `HANDOFF_OUT_DIR`, default `output/handoff/`) that the user hands to whatever browser agent they prefer (Claude Cowork, OpenClaw, a local Agent-SDK/`claude -p` runner, …). The agent works one site session at a time — logging into that site once, applying through the user's own logged-in browser — writes each outcome back into the session file's `status` column (`claimed` / `applied` / `handoff` / `skip:<reason>`), and the next `--handoff` run folds those into the **shared** status tracker (`role-status.jsonl`, board-insensitive keys) so a handled role never reappears on any site. Terminal outcomes are also reflected into career-ops' `applications.md` — the tracker the UI renders and the cloud maintains — (`applied`→**Applied**, `skip`→**SKIP**; `handoff`/`claimed`/`drafted` stay dedup-only, and a row already past **Evaluated** is never clobbered), which surfaces them in the UI Kanban and queues an **identity-anchored** pending override; the UI's **Push** then dispatches it to `edit-tracker.yml` so the cloud tracker matches. Handoff is a local-only stage (the daily cloud workflow never runs it), so applied status originates locally and flows *up* to the cloud, same as a manual Kanban edit.

---

## Pipeline stages

| Stage        | Script                      | Input → Output                                                            |
| ------------ | --------------------------- | ------------------------------------------------------------------------- |
| Scrape       | `pipeline/scrape.py`        | `config/search.yml` → `output/jobs.csv`                                   |
| Filter       | `pipeline/filter.py`        | `output/jobs.csv` → `output/filtered_jobs.csv`                            |
| Screen       | `pipeline/screen.py`        | `filtered_jobs.csv` → filtered in-place + backfills missing descriptions  |
| Bridge       | `pipeline/bridge.py`        | `filtered_jobs.csv` → `career-ops/data/pipeline.md` + `scan-history.tsv`  |
| Batch prep   | `pipeline/batch_prep.py`    | bridge output → `career-ops/batch/batch-input.tsv` + `batch/jds/*.txt`    |
| Batch evaluate | `pipeline/batch_evaluate.py` | `batch-input.tsv` → parallel LLM evaluation → `reports/*.md` + `tracker-additions/*.tsv` |
| Handoff      | `pipeline/handoff.py`       | queue (scored-export jsonl if present, else `career-ops/data/applications.md`) + `role-status.jsonl` (+ optional prose JOB_LOG) → `output/handoff/next-roles-<site>.{jsonl,md}` (one session per site) for the user's browser agent |

`orchestrate.py` chains all stages. Each stage can be skipped independently via `--skip-<stage>`; `--handoff` opts into the terminal work-order build (`--handoff-board`, `--handoff-limit`, `--handoff-tailor`).

---

## How to run

```powershell
# Windows
./run.ps1                        # scrape → filter → screen → bridge → batch-prep
./run.ps1 --batch                # + evaluate interactively via your configured CLI (default: claude)
./run.ps1 --evaluate-batch       # + evaluate via any LLM API (auto-detects provider from env keys)

# Select specific search passes — three mutually-exclusive flags:
./run.ps1 --evaluate-batch --only-pass "easy apply"   # explicit name match (errors on typo)
./run.ps1 --evaluate-batch --easy-apply-only          # passes with easy_apply: true (no-ops if none)
./run.ps1 --evaluate-batch --no-easy-apply            # passes without easy_apply: true

# Skip stages when re-running
./run.ps1 --skip-scrape --skip-filter --batch
./run.ps1 --skip-scrape --skip-filter --evaluate-batch

# Build the browser-agent work-orders — one session per job site
# (next-roles-<site>.jsonl). Queue = the scored-export jsonl when one exists,
# else career-ops/data/applications.md (so --recheck-liveness Discards keep dead
# roles out on the tracker path; the agent re-verifies postings live either
# way). --handoff-limit caps each site; --handoff-board narrows to one site;
# --handoff-tailor pre-tailors a resume per row.
./run.ps1 --skip-scrape --skip-filter --handoff
./run.ps1 --skip-scrape --skip-filter --handoff --handoff-board linkedin --handoff-limit 25 --handoff-tailor
```

```bash
# macOS/Linux
./run.sh --batch
./run.sh --evaluate-batch
```

---

## Key files

| File                                    | Purpose                                                                                          |
| --------------------------------------- | ------------------------------------------------------------------------------------------------ |
| `orchestrate.py`                        | Main entrypoint — chains all stages, parses CLI flags                                            |
| `run.ps1` / `run.sh`                    | Wrappers — activate venv, route `--batch` to the career-ops CLI runner                           |
| `run-ui.ps1` / `run-ui.sh`              | Launch the local triage UI (`pipeline/app`, FastAPI on localhost) — read-only results view       |
| `pipeline/app/`                         | Local web UI — `server.py` (FastAPI routes + a localhost-only cross-origin guard; incl. the browser-agent handoff endpoints: `POST /api/handoff/build` + status polling for the 🤝 toolbar button, `GET /api/handoff/role-prompt/{num}` for the per-role paste-ready prompt), `data.py` (parse applications.md + render reports), `skills.py` (career-ops skill launchpad: capability detection + a skill registry — résumé-markdown via API or CLI, and PDF / interview-prep / apply via CLI hand-off), `self_update.py` (pull the maintainer's template changes via `git fetch/merge/push`), `reset.py` (start-over: snapshot then wipe job-search state, keep setup; clear the cloud cache), `static/` (SPA). Deps in `requirements-ui.txt`. |
| `config/search.yml`                     | **Edit this** — searches, filters, screening config                                              |
| `.env`                                  | Env vars: `RESUME_PATH`, `CAREER_OPS_PATH`, `BATCH_CLI`, `ANTHROPIC_API_KEY`, `BATCH_MODEL`, `SKILL_PATH_DEFAULT`; tailoring: `RESUME_DOCX_PATH` (default `resumes/resume.docx`), `APPLY_TAILOR_MIN_SCORE` (default 4.0), `TAILOR_MODEL`, `SOFFICE_PATH`; handoff: `HANDOFF_OUT_DIR` (where the per-site work-orders land — set it in the Setup wizard's "Browser-agent handoff folder" field, which writes the key and creates + seeds the folder with a `HANDOFF-README.md` (agent instructions) and a `PROFILE.md` (the living master, seeded from career-ops) the agent follows [`bootstrap_handoff_dir`]; `--handoff`/setup also seed it; default `output/handoff`), `HANDOFF_JOB_LOG` (optional prose log to reconcile); liveness recheck (`--recheck-liveness`): `RECHECK_BUDGET` (stalest roles re-checked per run, default 100), `RECHECK_MIN_AGE_HOURS` (don't re-check a role confirmed within this window, default 6) — together they cap the per-run fetch burst that trips LinkedIn's rate limiter; manual backlog drain (`--recheck-drain`, or auto in the UI when the backlog exceeds the budget): `RECHECK_DRAIN_COOLDOWN` (seconds between budgeted sweeps, default 60), `RECHECK_DRAIN_MAX_CYCLES` (hard cap on sweeps, default 20). The re-check only verifies sites with an unauthenticated liveness path: LinkedIn via the guest endpoint, and Indeed via the jobData GraphQL API (`screen.fetch_indeed_expiry` — the same `apis.indeed.com` endpoint + mobile-app key the JobSpy scraper uses; the Cloudflare wall only guards the website, so batched `expired`-flag lookups work where a page fetch can't). Glassdoor serves a JS anti-bot wall a fetch can't classify and has no API fallback, so it's reported `unverifiable` and skipped |
| `resumes/resume.{pdf,docx,odt}`         | Resume used to extract scoring keywords. Import any of the three (DOCX/ODT recommended — also feeds tailoring below); `pipeline/resume_text.py` dispatches extraction by extension. `extract_resume_text()` in [pipeline/filter.py](pipeline/filter.py) delegates to it; the filter auto-discovers `resume.{pdf,docx,odt}` when `RESUME_PATH` is unset. |
| `resumes/resume.docx`                   | The candidate's source résumé — feeds keyword scoring and is the default upload. **Per-job tailoring no longer slot-edits it:** `--handoff-tailor` now BUILDS a résumé from `PROFILE.md` (LLM → grounded content-JSON → one-page fit — `pipeline/resume_content.py` → `resume_render`/`resume_fit`/`resume_build.fit_to_page`, reusing `resume_tailor`'s LibreOffice/soffice helpers) and caches the gate-passed PDF at `career-ops/output/<Company> - resume.pdf`, **role-aware** via a `.role` sidecar (rebuilt when the role or `PROFILE.md` changes). Jobs scoring below `APPLY_TAILOR_MIN_SCORE` (or `--handoff-tailor-min-score`) get no pre-tailored file. The slot-edit tailor (`pipeline/resume_tailor.py`) is superseded on this path; its hand-edit-wins caching does not apply to the build-from-content model. |
| `output/handoff/next-roles-<site>.{jsonl,md}` | The **work-orders** for the browser agent — one session per job site the scraper searches from (`next-roles-linkedin.jsonl`, `next-roles-indeed.jsonl`, …; unrecognized domains go to `next-roles-other.jsonl`). Each holds that site's fresh scored roles ranked best-first, each with board/url/score/resume-base (and `resume_pdf` under `--handoff-tailor`) plus a `status` column the agent writes back (`claimed`/`applied`/`handoff`/`skip:<reason>`). Site is derived from the posting URL via `board_of()`. |
| `output/handoff/role-status.jsonl`      | The **shared status tracker** (one file across every site session) — one line per handled role keyed `company::role` (case/board/req-id normalized), so a role applied on one site never reappears on another. Seeded from a prose JOB_LOG when `HANDOFF_JOB_LOG`/`--job-log` points at one; extended by agent writeback each run. The machine source of truth for "what's done". |
| `output/handoff/PROFILE.md`             | The browser agent's **living master** — identity, a metric-carrying **fact bank** (résumé experience kept verbatim so numbers never get trimmed), an honesty-rated **skills inventory**, the **standing answers** applications keep asking (work auth, comp, location, EEO), and tailoring rules. Seeded once from `career-ops/cv.md` + `config/profile.yml` by `render_profile_md`/`bootstrap_handoff_dir`, then **grown by the agent** (append-only memory). Non-clobber — qualify + tailor every role against it. When present, it's also the **authoritative candidate profile for local LLM evaluation** — both `--evaluate-batch` and the UI's Add-Job — via the shared `eval_system_prompt` (`resolve_profile_md`), superseding the cv.md/profile.yml/_profile.md/article-digest.md seeds; the cloud evaluator picks it up too via the optional `PROFILE_MASTER_B64` secret (the Setup wizard encodes it; the daily workflow decodes it to `career-ops/PROFILE.md`). |
| `output/jobs.csv`                       | Raw scrape output                                                                                |
| `output/filtered_jobs.csv`              | Score-filtered and screened jobs                                                                 |
| `career-ops/data/pipeline.md`           | Pending evaluation queue (checkbox list)                                                         |
| `career-ops/data/scan-history.tsv`      | All-time dedup record                                                                            |
| `career-ops/batch/batch-input.tsv`      | Batch evaluation queue                                                                           |
| `career-ops/batch/jds/*.txt`            | Cached job descriptions (inlined into evaluation prompts)                                        |
| `career-ops/batch/batch-api-state.json` | `--evaluate-batch` state — per-job metadata, report/tracker numbers, completion status           |
| `career-ops/config/profile.yml`         | Candidate profile (created by setup-profile.mjs)                                                |
| `career-ops/batch/batch-runner.sh`      | Interactive batch evaluator — `--cli claude\|opencode\|gemini\|qwen`, `--model`, `--skip-pdf`   |
| GHA Cache (key `pipeline-state-v1`)     | Per-fork runtime state (scan-history, applications.md, recheck-state, batch state). Never committed. |

---

## Configuration (`config/search.yml`)

```yaml
searches:
  - name: "pass name"
    search_terms: ["term1", "term2"]
    sites: [indeed, linkedin, glassdoor]
    results_wanted: 50
    location: "Dallas, TX"
    hours_old: 168 # mutually exclusive with job_type/is_remote/easy_apply on Indeed
    is_remote: true
    easy_apply: false
    linkedin_fetch_description: false # see "Description backfill" below

filter:
  target_titles: ["Senior Software Engineer", "Staff Engineer"] # +5 score bonus
  negative_titles: ["intern", "director", "VP"] # hard-exclude
  min_score: 5
  max_age_hours: 168
  keyword_overrides:
    "react native": 3 # boost terms YAKE missed

screen:
  liveness: true # HTTP check — drops filled/expired listings; also backfills descriptions
  liveness_timeout: 8
```

**JobSpy constraint**: On Indeed/Glassdoor, `hours_old` is mutually exclusive with `job_type`, `is_remote`, and `easy_apply`. Split into separate search passes to use both filters.

**Description backfill**: `linkedin_fetch_description: true` makes JobSpy fetch each LinkedIn JD individually during scrape — a sequential per-job HTTP request that easily takes 30+ minutes on 1000 results. Keep it **false**. The screen stage backfills descriptions for the jobs that survive filtering:

- **LinkedIn** URLs are fetched via the public **guest job-posting endpoint** (`jobs-guest/jobs/api/jobPosting/{id}`), not the regular `/jobs/view/` page. The regular page is login-walled from datacenter IPs and inconsistently returns a sign-in preview that has no extractable JD (this caused ~17% of LinkedIn jobs to reach evaluation with an empty description). The guest endpoint returns the full JD reliably and gives a cleaner liveness signal (404 = gone, JD present = live). `linkedin_guest_jd_url()` in [pipeline/screen.py](pipeline/screen.py) does the URL mapping; `job_url` in the CSV stays the human-facing page.
- **Indeed / Glassdoor** descriptions usually come from JobSpy directly; if missing, the screen stage extracts them from the same page it fetches for the liveness check (site-specific selectors + a generic `<body>` fallback).

Net effect: we pay ~dozens of fetches per run for the jobs that actually survive filtering, not thousands, and every LinkedIn role gets its complete JD.

---

## Filtering logic

Keywords are extracted from the resume (`resumes/resume.{pdf,docx,odt}`, no hardcoded vocab):

- YAKE 1–3 gram extraction (weight 1) + explicit Skills-section tokens (weight 2)
- `score_job()` matches keywords against title + description + skills fields
- Target title match → +5 bonus; negative title match → hard-exclude (score = None)
- Jobs sorted descending by `relevance_score`

---

## Deduplication

Two-stage dedup against the same sources:

- `career-ops/data/scan-history.tsv` (append-only, statuses: `added`, `screened-dead`)
- `career-ops/data/pipeline.md`
- `career-ops/data/applications.md`

**Stage 1 — pre-screen** ([pipeline/screen.py](pipeline/screen.py)): drops URLs already in any of the above *before* HTTP fetches. With 100-result scrapes on a daily cadence ~80% of rows are repeats, so this is the biggest cost saving in the pipeline. Also writes URLs that fail liveness back to `scan-history.tsv` with status `screened-dead` so future runs skip them too.

**Stage 2 — bridge** ([pipeline/bridge.py](pipeline/bridge.py)): catches anything that slipped through (e.g. when liveness is off) and also dedupes `company::role` pairs against `applications.md` so reposted listings under the same role don't get re-evaluated.

`scan-history.tsv` persists across GitHub Actions runs via the workflow's [pipeline state cache](#cloud-automation-github-actions). The first scheduled run creates the cache; from run #2 onward, dedup is active. Cache is per-fork and never committed to git, so dedup state is private even if your fork is public.

---

## Interactive batch evaluation (`--batch`)

Invokes `career-ops/batch/batch-runner.sh` with the CLI set by `BATCH_CLI` (default: `claude`).
The CLI agent runs fully locally — it reads files, calls WebSearch, generates PDFs.

Key options forwarded to the runner:

```
--cli claude|opencode|gemini|qwen  set by BATCH_CLI env var (default: claude)
--model <name>                     set by OLLAMA_MODEL env var; omit to use CLI default
--skip-pdf                         skip PDF step (report + tracker only)
--min-score N                      skip tracker for jobs scoring below N
--retry-failed                     re-attempt failed jobs
--dry-run                          list pending without processing
--start-from N                     resume from job ID N
```

State persisted in `career-ops/batch/batch-state.tsv` — safe to interrupt and resume.

---

## Multi-provider evaluation (`--evaluate-batch`)

Evaluates jobs synchronously using any LLM provider. Results are immediate — no async wait.

**Candidate profile source:** when a living `PROFILE.md` exists (handoff dir, else `career-ops/PROFILE.md` — see `resolve_profile_md`), it is the authoritative candidate profile fed to the evaluator, superseding the `cv.md`/`profile.yml`/`_profile.md`/`article-digest.md` seeds. The batch path and the UI's Add-Job eval share one builder (`eval_system_prompt`), so they resolve it identically. Absent a PROFILE.md, the four seeds are used as before. The cloud daily reads it too when the optional `PROFILE_MASTER_B64` secret is set — the Setup wizard encodes your living PROFILE.md and the workflow decodes it to `career-ops/PROFILE.md` (`resolve_profile_md`'s fallback); without the secret, the cloud uses the seed secrets exactly as before.

```powershell
./run.ps1 --evaluate-batch                         # auto-detects provider from env keys
./run.ps1 --evaluate-batch --batch-provider gemini # explicit provider
./run.ps1 --evaluate-batch --batch-concurrency 5   # more parallel workers
```

Provider auto-detection order: Gemini → Groq → DeepInfra → OpenRouter → DeepSeek → OpenAI → Anthropic (first env key found).

| Provider | Env var | Free tier |
|---|---|---|
| Gemini | `GEMINI_API_KEY` | 1,500 req/day, no credit card |
| Groq | `GROQ_API_KEY` | Yes — fast open-source models |
| DeepInfra | `DEEPINFRA_API_KEY` | No — pay-as-you-go open-weight models |
| OpenRouter | `OPENROUTER_API_KEY` | No — pay-as-you-go, many models via one key |
| DeepSeek | `DEEPSEEK_API_KEY` | No — pay-as-you-go, cheaper than frontier |
| OpenAI | `OPENAI_API_KEY` | No |
| Anthropic | `ANTHROPIC_API_KEY` | No |
| Ollama | `OLLAMA_BASE_URL` | Local — no API key needed |

Set in `.env` or as repository secrets for GitHub Actions. `BATCH_PROVIDER` overrides auto-detection.

---

## Cloud automation (GitHub Actions)

> **Privacy first**: every cloud workflow refuses to run unless your fork is private. The Actions tab of a public repo exposes workflow run history, schedule cadence, and durations — all of which reveal active job searching. See the privacy notice at the top of [README.md](README.md).

Cloud automation workflows:

| Workflow | Schedule | What it does |
|----------|----------|--------------|
| `daily-pipeline.yml` | Noon UTC (7 AM CDT) | Runs **every** search pass (including any `easy_apply: true` pass) once a day — no pass-selection flag. Scrape → filter → screen → bridge → evaluate. |
| `edit-tracker.yml` | Manual (`workflow_dispatch`) | Replaces `applications.md` in the cache with a user-supplied base64 blob. Use after editing the tracker locally. |
| `update-from-template.yml` | Manual (`workflow_dispatch`) | Merges the upstream template's latest `main` into this copy and pushes (copies have no fork link). Skips in the template repo itself; aborts on conflict. The UI's **⬆ Update** button does the same merge+push locally (also refreshing the clone running the UI); `pipeline/app/self_update.py` holds the logic. |

**Storage model — no user data is ever committed:**

- **Setup data** (CV, profile, search config) → repository **Secrets** (encrypted at rest, not visible to forkers)
- **Runtime state** (scan-history, applications.md, pipeline.md, recheck-state, batch state, cached JDs) → **`actions/cache@v4`** keyed `pipeline-state-v1`. Per-fork, restored at workflow start, saved at workflow end. Invisible to anyone but the fork owner.
- **Outputs** (reports, tracker snapshot) → **`actions/upload-artifact@v4`**. Downloadable from the Actions tab for 90 days.

**Pass selection flags** (mutually exclusive, for manual/local runs): `--only-pass "name1,name2"` (case-insensitive name match, errors on no match), `--easy-apply-only` (passes with `easy_apply: true`, no-ops if none), `--no-easy-apply` (passes without `easy_apply: true`). The daily cloud workflow runs **every** pass with no selection flag; these flags exist for ad-hoc local runs and route by the `easy_apply` field rather than name so user-renamed passes still work.

**Setup** (one-time):

1. **Fork this repo** on GitHub. Then **Settings → General → Change repository visibility → Make private**. Workflows hard-stop if the repo is public.

2. **Add GitHub Secrets** (Settings → Secrets and variables → Actions → New repository secret). Each `*_B64` secret is one file, base64-encoded. **Easiest: use the ⚙ Setup wizard — it encodes and writes all of these correctly.** To do it by hand, encode with the command for your OS:

   ```powershell
   # Windows / PowerShell — do NOT use `certutil -encode` (it adds
   # -----BEGIN CERTIFICATE----- lines that aren't valid base64 and break the run).
   [Convert]::ToBase64String([IO.File]::ReadAllBytes("career-ops\cv.md"))
   ```
   ```bash
   base64 -i career-ops/cv.md | tr -d '\n'   # macOS (BSD base64, no -w)
   base64 -w0 career-ops/cv.md               # Linux (GNU coreutils)
   ```

   Required: `CV_MD_B64` (`career-ops/cv.md`), `PROFILE_YML_B64` (`career-ops/config/profile.yml`), `SEARCH_CONFIG_B64` (`config/search.yml`), `RESUME_TXT_B64` (`resumes/resume.txt` — run setup locally to generate the .txt), plus at least one LLM provider key:
     - `GEMINI_API_KEY` — free tier, recommended; get one at aistudio.google.com
     - `GROQ_API_KEY` — free tier with fast open-source models
     - `OPENAI_API_KEY` — OpenAI
     - `ANTHROPIC_API_KEY` — Anthropic

   Optional: `PROFILE_MD_B64` (`career-ops/modes/_profile.md`), `ARTICLE_DIGEST_B64` (`career-ops/article-digest.md`), `PROFILE_MASTER_B64` (your living `PROFILE.md` → `career-ops/PROFILE.md`, so the cloud evaluator scores against the same master as local eval).

   The workflows decode tolerantly (they strip certutil PEM wrappers and CRLFs) and, if a secret still isn't valid base64, fail with a message naming the offending secret instead of a generic abort.

3. **Enable Actions** (Actions tab → enable workflows). Workflows run on their schedules.

4. **Read results**: Actions tab → click the run → download the `pipeline-output-*` artifact. Extract to see `reports/*.md` and `applications.md`.

5. **Edit `applications.md`** (to mark roles as Applied / Rejected / etc.):
   1. Download the latest artifact (above) and extract `applications.md`.
   2. Edit it locally.
   3. base64-encode it and copy the output — Windows: `[Convert]::ToBase64String([IO.File]::ReadAllBytes("applications.md"))`; macOS: `base64 -i applications.md | tr -d '\n'`; Linux: `base64 -w0 applications.md`. (Not `certutil` on Windows.)
   4. Actions → "Edit Tracker" → Run workflow → paste the base64 → Run.
   5. The next pipeline run will see the updated statuses.

**Cache eviction**: GitHub Actions Cache evicts entries after 7 days of inactivity. A daily-running workflow keeps it warm, so this only bites if you pause the workflow for >7 days. Worst case after eviction: dedup starts empty, some jobs get re-evaluated once. Recoverable.

---

## Setup (one-time)

```powershell
./setup.ps1    # creates .venv, installs deps (core + UI), clones career-ops; points to the /onboard wizard for profile setup
```

Requires Python 3.12 (not 3.13 — jobspy pins numpy 1.26.3, no 3.13 wheel).

---

## Tests

```powershell
.venv/Scripts/python -m pytest
```

Tests live in `tests/` — covers filter scoring, date parsing, bridge dedup, scrape validation.
