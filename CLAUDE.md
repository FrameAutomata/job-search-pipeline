# job-search-pipeline

Automated end-to-end job search orchestrator. Scrapes LinkedIn/Indeed/Glassdoor via **JobSpy**, scores results against a resume using YAKE keyword extraction, optionally screens for liveness, then bridges surviving jobs into **career-ops** for AI-powered evaluation. Supports interactive evaluation via any agent CLI (`--batch`) or async bulk evaluation via the Anthropic Messages Batch API (`--submit-batch`/`--retrieve-batch`) at 50% cost.

---

## Pipeline stages

| Stage        | Script                      | Input → Output                                                            |
| ------------ | --------------------------- | ------------------------------------------------------------------------- |
| Scrape       | `pipeline/scrape.py`        | `config/search.yml` → `output/jobs.csv`                                   |
| Filter       | `pipeline/filter.py`        | `output/jobs.csv` → `output/filtered_jobs.csv`                            |
| Screen       | `pipeline/screen.py`        | `filtered_jobs.csv` → filtered in-place (optional liveness check)         |
| Bridge       | `pipeline/bridge.py`        | `filtered_jobs.csv` → `career-ops/data/pipeline.md` + `scan-history.tsv`  |
| Batch prep   | `pipeline/batch_prep.py`    | bridge output → `career-ops/batch/batch-input.tsv` + `batch/jds/*.txt`    |
| Batch evaluate | `pipeline/batch_evaluate.py` | `batch-input.tsv` → parallel LLM evaluation → `reports/*.md` + `tracker-additions/*.tsv` |
| Batch submit | `pipeline/batch_submit.py`  | `batch-input.tsv` + context files → Anthropic Batch API → `batch-api-state.json` |
| Batch retrieve | `pipeline/batch_retrieve.py` | Batch API results → `reports/*.md` + `tracker-additions/*.tsv`          |

`orchestrate.py` chains all stages. Each stage can be skipped independently via `--skip-<stage>`.

---

## How to run

```powershell
# Windows
./run.ps1                        # scrape → filter → screen → bridge → batch-prep
./run.ps1 --batch                # + evaluate interactively via your configured CLI (default: claude)
./run.ps1 --evaluate-batch       # + evaluate via any LLM API (auto-detects provider from env keys)
./run.ps1 --submit-batch         # + submit to Anthropic Batch API (async, ~24 h, 50% cheaper)
./run.ps1 --retrieve-batch       # poll and retrieve completed Batch API results

# Skip stages when re-running
./run.ps1 --skip-scrape --skip-filter --batch
./run.ps1 --skip-scrape --skip-filter --evaluate-batch
./run.ps1 --skip-scrape --skip-filter --submit-batch
```

```bash
# macOS/Linux
./run.sh --batch
./run.sh --evaluate-batch
./run.sh --submit-batch
./run.sh --retrieve-batch
```

---

## Key files

| File                                    | Purpose                                                                                          |
| --------------------------------------- | ------------------------------------------------------------------------------------------------ |
| `orchestrate.py`                        | Main entrypoint — chains all stages, parses CLI flags                                            |
| `run.ps1` / `run.sh`                    | Wrappers — activate venv, route `--batch`, `--submit-batch`, `--retrieve-batch`                  |
| `config/search.yml`                     | **Edit this** — searches, filters, screening config                                              |
| `.env`                                  | Env vars: `RESUME_PATH`, `CAREER_OPS_PATH`, `BATCH_CLI`, `ANTHROPIC_API_KEY`, `BATCH_MODEL`      |
| `resumes/resume.pdf`                    | Resume used to extract scoring keywords                                                          |
| `output/jobs.csv`                       | Raw scrape output                                                                                |
| `output/filtered_jobs.csv`              | Score-filtered and screened jobs                                                                 |
| `career-ops/data/pipeline.md`           | Pending evaluation queue (checkbox list)                                                         |
| `career-ops/data/scan-history.tsv`      | All-time dedup record                                                                            |
| `career-ops/batch/batch-input.tsv`      | Batch evaluation queue                                                                           |
| `career-ops/batch/jds/*.txt`            | Cached job descriptions (inlined into Batch API prompts)                                         |
| `career-ops/batch/batch-api-state.json` | Anthropic Batch API state — job metadata, batch_id, per-job status                              |
| `career-ops/config/profile.yml`         | Candidate profile (created by setup-profile.mjs)                                                |
| `career-ops/batch/batch-runner.sh`      | Interactive batch evaluator — `--cli claude\|opencode\|gemini\|qwen`, `--model`, `--skip-pdf`   |
| `career-ops-data/`                      | Committed copy of career-ops user data — synced by GitHub Actions workflows                     |

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
    linkedin_fetch_description: true # required for description scoring

filter:
  target_titles: ["Senior Software Engineer", "Staff Engineer"] # +5 score bonus
  negative_titles: ["intern", "director", "VP"] # hard-exclude
  min_score: 5
  max_age_hours: 168
  keyword_overrides:
    "react native": 3 # boost terms YAKE missed

screen:
  liveness: true # HTTP check — drops filled/expired listings
  liveness_timeout: 8
```

**JobSpy constraint**: On Indeed/Glassdoor, `hours_old` is mutually exclusive with `job_type`, `is_remote`, and `easy_apply`. Split into separate search passes to use both filters.

---

## Filtering logic

Keywords are extracted from `resumes/resume.pdf` (no hardcoded vocab):

- YAKE 1–3 gram extraction (weight 1) + explicit Skills-section tokens (weight 2)
- `score_job()` matches keywords against title + description + skills fields
- Target title match → +5 bonus; negative title match → hard-exclude (score = None)
- Jobs sorted descending by `relevance_score`

---

## Deduplication

Bridge skips jobs already seen in any of:

- `career-ops/data/scan-history.tsv`
- `career-ops/data/pipeline.md`
- `career-ops/data/applications.md`

Also deduplicates `company::role` pairs from `applications.md`.

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

## Anthropic Batch API (`--submit-batch` / `--retrieve-batch`)

Submits evaluations to the [Anthropic Messages Batch API](https://docs.anthropic.com/en/docs/build-with-claude/message-batches) — async bulk processing at 50% cost vs standard API pricing. Batches complete within 24 hours.

**Requirements**: set `ANTHROPIC_API_KEY` in `.env` (or as an environment variable).

```powershell
# Submit all pending jobs from batch-input.tsv
./run.ps1 --submit-batch

# Or submit without running the pipeline first
./run.ps1 --skip-scrape --skip-filter --skip-screen --skip-bridge --skip-batch-prep --submit-batch

# Check and retrieve completed results
./run.ps1 --retrieve-batch
```

**How it works**:
1. `batch_submit.py` reads `batch-input.tsv`, inlines `cv.md` + `profile.yml` + `_profile.md` + `article-digest.md` into a system prompt, and submits one Batch API request per job.
2. Pre-assigns report numbers and tracker numbers before submission so parallel jobs don't conflict.
3. Saves state to `career-ops/batch/batch-api-state.json` (batch_id + per-job metadata).
4. `batch_retrieve.py` polls the batch status and — when complete — parses XML-tagged responses, writes `reports/*.md` and `batch/tracker-additions/*.tsv`, then runs `node merge-tracker.mjs`.

**Batch mode limitations** (vs interactive `--batch`):
- No PDF generation (no file system access in Messages API)
- No real-time WebSearch — salary/company data from training knowledge (labeled as estimates)
- No Playwright liveness verification — freshness marked "unverified (batch mode)"

**Model selection** (set `BATCH_MODEL` in `.env`):
- `claude-sonnet-4-6` — default; recommended for the A-G reasoning quality (~$1.50/M input at batch price)
- `claude-haiku-4-5-20251001` — cheaper (~$0.40/M) but shallower reasoning on complex evaluation blocks

---

## Cloud automation (GitHub Actions)

Two workflows run automatically to keep your pipeline running without your computer:

| Workflow | Schedule | What it does |
|----------|----------|--------------|
| `daily-pipeline.yml` | Noon UTC (7 AM CDT) | Scrape → filter → bridge → evaluate batch (auto-detected provider) → commit results |
| `retrieve-batch.yml` | Midnight UTC (7 PM CDT) | Poll Anthropic async batch → write reports → commit (only needed with `--submit-batch`) |

**Setup** (one-time):

1. **Fork this repo** on GitHub — secrets and workflow runs are per-fork, so each user needs their own copy. Clone your fork locally.

2. **Create `career-ops-data/`** directory in the repo root — this holds your career-ops user data committed to git:

   ```
   career-ops-data/
     cv.md                        ← your CV (required)
     config/profile.yml           ← your profile (required)
     modes/_profile.md            ← your customizations (optional)
     article-digest.md            ← your proof points (optional)
     data/applications.md         ← tracker (auto-updated by workflow)
     data/scan-history.tsv        ← dedup history (auto-updated)
   ```

   Copy your existing files from `career-ops/`:
   ```bash
   mkdir -p career-ops-data/data career-ops-data/config career-ops-data/modes
   cp career-ops/cv.md career-ops-data/
   cp career-ops/config/profile.yml career-ops-data/config/
   cp career-ops/modes/_profile.md career-ops-data/modes/  # if it exists
   cp career-ops/article-digest.md career-ops-data/  # if it exists
   ```

3. **Add GitHub Secrets** (your fork → Settings → Secrets and variables → Actions):
   - `SEARCH_CONFIG_B64` — `base64 -w0 config/search.yml`
   - `RESUME_TXT_B64` — extracted resume text: `python -c "import pdfplumber; ..."` then `base64 -w0 resumes/resume.txt`
   - At least one provider API key (workflow auto-detects the first one found):
     - `GEMINI_API_KEY` — free tier, recommended; get one at aistudio.google.com
     - `GROQ_API_KEY` — free tier with fast open-source models
     - `OPENAI_API_KEY` — OpenAI
     - `ANTHROPIC_API_KEY` — Anthropic (also required for `--submit-batch` async path)

4. **Commit** `career-ops-data/` and push to your fork. The workflows will trigger on schedule.

5. **Read results**: evaluation reports appear in `career-ops-data/reports/` after `daily-pipeline.yml` runs. Pull your fork to see them locally.

---

## Setup (one-time)

```powershell
./setup.ps1    # creates .venv, installs deps, clones career-ops, runs profile setup
```

Requires Python 3.12 (not 3.13 — jobspy pins numpy 1.26.3, no 3.13 wheel).

---

## Tests

```powershell
.venv/Scripts/python -m pytest
```

Tests live in `tests/` — covers filter scoring, date parsing, bridge dedup, scrape validation.
