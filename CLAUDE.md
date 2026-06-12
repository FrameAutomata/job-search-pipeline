# job-search-pipeline

Automated end-to-end job search orchestrator. Scrapes LinkedIn/Indeed/Glassdoor via **JobSpy**, scores results against a resume using YAKE keyword extraction, optionally screens for liveness, then bridges surviving jobs into **career-ops** for AI-powered evaluation. Supports interactive evaluation via any agent CLI (`--batch`) or synchronous parallel API evaluation across any LLM provider (`--evaluate-batch`).

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

`orchestrate.py` chains all stages. Each stage can be skipped independently via `--skip-<stage>`.

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
| `pipeline/app/`                         | Local web UI — `server.py` (FastAPI routes + a localhost-only cross-origin guard), `data.py` (parse applications.md + render reports), `skills.py` (career-ops skill launchpad: capability detection + a skill registry — résumé-markdown via API or CLI, and PDF / interview-prep / apply via CLI hand-off), `static/` (SPA). Deps in `requirements-ui.txt`. |
| `config/search.yml`                     | **Edit this** — searches, filters, screening config                                              |
| `.env`                                  | Env vars: `RESUME_PATH`, `CAREER_OPS_PATH`, `BATCH_CLI`, `ANTHROPIC_API_KEY`, `BATCH_MODEL`, `SKILL_PATH_DEFAULT`; tailoring: `RESUME_DOCX_PATH` (default `resumes/resume.docx`), `APPLY_TAILOR_MIN_SCORE` (default 4.0), `TAILOR_MODEL`, `SOFFICE_PATH`; UI apply review: `APPLY_HOLD_TIMEOUT` (seconds the browser is held open awaiting Submit/Cancel, default 300) |
| `resumes/resume.pdf`                    | Resume used to extract scoring keywords                                                          |
| `resumes/resume.docx`                   | Source for per-job **tailored resumes** (`pipeline/resume_tailor.py`) — the apply stage slot-edits a copy per company (summary / bullets / skills values only; headers, employers, dates untouchable), verifies one page via LibreOffice against the pristine copy's baseline, and uploads the verified PDF. Jobs scoring below `APPLY_TAILOR_MIN_SCORE` (or `--apply-tailor-min-score`) use the default resume. Cached as `career-ops/output/<Company> - resume.docx/.pdf`; hand-edited files win if newer. |
| `output/jobs.csv`                       | Raw scrape output                                                                                |
| `output/filtered_jobs.csv`              | Score-filtered and screened jobs                                                                 |
| `career-ops/data/pipeline.md`           | Pending evaluation queue (checkbox list)                                                         |
| `career-ops/data/scan-history.tsv`      | All-time dedup record                                                                            |
| `career-ops/batch/batch-input.tsv`      | Batch evaluation queue                                                                           |
| `career-ops/batch/jds/*.txt`            | Cached job descriptions (inlined into evaluation prompts)                                        |
| `career-ops/batch/batch-api-state.json` | `--evaluate-batch` state — per-job metadata, report/tracker numbers, completion status           |
| `career-ops/config/profile.yml`         | Candidate profile (created by setup-profile.mjs)                                                |
| `career-ops/batch/batch-runner.sh`      | Interactive batch evaluator — `--cli claude\|opencode\|gemini\|qwen`, `--model`, `--skip-pdf`   |
| GHA Cache (key `pipeline-state-v1`)     | Per-fork runtime state (scan-history, applications.md, batch state). Never committed.            |

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

Keywords are extracted from `resumes/resume.pdf` (no hardcoded vocab):

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

```powershell
./run.ps1 --evaluate-batch                         # auto-detects provider from env keys
./run.ps1 --evaluate-batch --batch-provider gemini # explicit provider
./run.ps1 --evaluate-batch --batch-concurrency 5   # more parallel workers
```

Provider auto-detection order: Gemini → Groq → OpenAI → Anthropic (first env key found).

| Provider | Env var | Free tier |
|---|---|---|
| Gemini | `GEMINI_API_KEY` | 1,500 req/day, no credit card |
| Groq | `GROQ_API_KEY` | Yes — fast open-source models |
| OpenAI | `OPENAI_API_KEY` | No |
| Anthropic | `ANTHROPIC_API_KEY` | No |
| Ollama | `OLLAMA_BASE_URL` | Local — no API key needed |

Set in `.env` or as repository secrets for GitHub Actions. `BATCH_PROVIDER` overrides auto-detection.

---

## Cloud automation (GitHub Actions)

> **Privacy first**: every cloud workflow refuses to run unless your fork is private. The Actions tab of a public repo exposes workflow run history, schedule cadence, and durations — all of which reveal active job searching. See the privacy notice at the top of [README.md](README.md).

Three workflows make up the cloud automation:

| Workflow | Schedule | What it does |
|----------|----------|--------------|
| `daily-pipeline.yml` | Noon UTC (7 AM CDT) | Runs every pass without `easy_apply: true` via `--no-easy-apply`. Scrape → filter → screen → bridge → evaluate. |
| `easy-apply-pipeline.yml` | Every 4 h at 02/06/10/14/18/22 UTC **and** right after a successful `daily-pipeline` run (`workflow_run` chain) | Runs every pass with `easy_apply: true` via `--easy-apply-only`. These listings churn fast — re-scrape often and rely on screen-stage dedup so only new+live postings get evaluated. No-ops cleanly if no easy-apply passes are configured. |
| `edit-tracker.yml` | Manual (`workflow_dispatch`) | Replaces `applications.md` in the cache with a user-supplied base64 blob. Use after editing the tracker locally. |

**Storage model — no user data is ever committed:**

- **Setup data** (CV, profile, search config) → repository **Secrets** (encrypted at rest, not visible to forkers)
- **Runtime state** (scan-history, applications.md, pipeline.md, batch state, cached JDs) → **`actions/cache@v4`** keyed `pipeline-state-v1`. Per-fork, restored at workflow start, saved at workflow end. Invisible to anyone but the fork owner.
- **Outputs** (reports, tracker snapshot) → **`actions/upload-artifact@v4`**. Downloadable from the Actions tab for 90 days.

**Pass selection flags** (mutually exclusive): `--only-pass "name1,name2"` (case-insensitive name match, errors on no match), `--easy-apply-only` (passes with `easy_apply: true`, no-ops if none), `--no-easy-apply` (passes without `easy_apply: true`). The cloud workflows route by `easy_apply` field rather than name so user-renamed passes still work.

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

   Optional: `PROFILE_MD_B64` (`career-ops/modes/_profile.md`), `ARTICLE_DIGEST_B64` (`career-ops/article-digest.md`).

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
