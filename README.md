# job-search-pipeline

> ## ⚠️ Privacy notice — read first
>
> Job-search activity is sensitive. If you are currently employed, your employer should not be able to see that you are evaluating other roles.
>
> This repository is a **public template**. The cloud automation refuses to run on the template itself (no-ops silently) and refuses to run on any public copy of it (hard-stops with a privacy warning). Real runs only happen in **private copies** created via GitHub's "Use this template" button. Workflow run history, commit log, and schedule cadence stay scoped to your private copy and are invisible to anyone but you.
>
> No user data is committed to this repository. All runtime state lives in GitHub Actions Cache (per-copy, invisible) and per-run Artifacts (downloadable from your private copy's Actions tab).
>
> Running the pipeline **locally** has no such concern — your data stays on your machine.

## Using this template

This repo is set up to be cloned into your own private copy. The public template stays as code/docs only.

### Create your private copy

1. On this repository's GitHub page, click **Use this template → Create a new repository**.
2. **Set Owner to your account, give it a name (e.g. `job-search-private`), and check "Private".**
3. Click **Create repository from template**.

GitHub creates a standalone private repo with the same files. It's *not* listed as a fork of this template, so there's no cross-reference and no fork-network visibility back to your account.

### Configure your private copy

Inside your private copy on github.com:

1. **Settings → Secrets and variables → Actions → New repository secret.** Add:
   - `CV_MD_B64` — `base64 -w0 path/to/your/cv.md`
   - `PROFILE_YML_B64` — `base64 -w0 path/to/your/profile.yml`
   - `SEARCH_CONFIG_B64` — `base64 -w0 config/search.yml`
   - `RESUME_TXT_B64` — `base64 -w0 resumes/resume.txt`
   - At least one LLM API key: `GEMINI_API_KEY` (free tier) / `GROQ_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`
   - Optional: `PROFILE_MD_B64`, `ARTICLE_DIGEST_B64`
2. **Actions tab → "I understand my workflows, go ahead and enable them".**
3. **Actions → Daily Job Pipeline → Run workflow** to do a test run before the scheduled cron fires.

### Pulling updates from the template later

Your private copy starts as a snapshot — it doesn't auto-track changes to this template. To pull updates:

```bash
git clone <your-private-copy-url>
cd <your-private-copy>
git remote add template <this-template-url>
git fetch template
git merge template/main   # or rebase, your call
git push
```

Conflicts only happen if you've edited the same files locally — your data lives in cache/secrets/artifacts, not in tracked files.

### For maintainers of this template

If you're maintaining this template (rather than using it for a job search), no action needed — the cron triggers fire here too, but the `preflight` job detects `is_template: true` via the GitHub API and exits cleanly with `should_run=false`. The Actions tab stays clean.

A local, fully automated pipeline that runs the complete job-search loop end to end:

1. **Scrape** — [JobSpy](https://github.com/speedyapply/JobSpy) pulls postings from Indeed, LinkedIn, Glassdoor, ZipRecruiter, etc. into `output/jobs.csv`.
2. **Filter** — keywords are extracted *from your resume* (YAKE statistical extraction — works for nursing, marketing, trades, finance, tech, any field). Each job is scored by keyword + target-title matches; negative titles hard-exclude. Output: `output/filtered_jobs.csv`.
3. **Screen** *(opt-in)* — for jobs that survived the filter, runs an HTTP liveness check (drops expired/filled postings), backfills each LinkedIn description via LinkedIn's public guest job-posting endpoint (reliable full JD, so `linkedin_fetch_description: false` is safe at scrape time), and dedupes against `scan-history.tsv` *before* the fetch so previously-seen URLs cost nothing.
4. **Bridge** — surviving postings are appended to [career-ops](https://github.com/santifer/career-ops)'s `data/pipeline.md` queue. Second dedup pass against scan-history, pipeline.md, and `company::role` pairs in applications.md.
5. **Batch prep** — writes the evaluation queue (`batch/batch-input.tsv`) and caches job descriptions (`batch/jds/{id}.txt`) for the evaluator.

Evaluation has two paths, pick whichever fits:
- `--batch` — interactive agent CLI (Claude Code / OpenCode / Gemini CLI / Qwen / your choice). Generates PDFs, can WebSearch in real time.
- `--evaluate-batch` — synchronous parallel API calls (auto-detects Gemini / Groq / DeepInfra / OpenRouter / OpenAI / Anthropic / Ollama). Immediate results. Used by the cloud workflows.

## Quickstart

```powershell
# Windows
git clone <this repo>
cd job-search-pipeline
.\setup.ps1                  # creates venv, installs deps, clones career-ops, runs profile setup
# Edit config\search.yml, then pick an evaluation mode:
.\run.ps1 --batch            # interactive CLI agent (default: claude)
.\run.ps1 --evaluate-batch   # API-driven (free Gemini if GEMINI_API_KEY set)
```

```bash
# macOS / Linux
git clone <this repo>
cd job-search-pipeline
./setup.sh
./run.sh --evaluate-batch
```

For unattended cloud runs, see [Using this template](#using-this-template) above. See [QUICKSTART.md](QUICKSTART.md) for a detailed walkthrough.

## Configuration

**`config/search.yml`** — search terms, sites, location, filter rules, and optional screen settings.

**`.env`** — paths and overrides:

| Variable | Default | Purpose |
|----------|---------|---------|
| `CAREER_OPS_PATH` | `./career-ops` | Path to career-ops directory |
| `RESUME_PATH` | auto-detected | Path to your resume PDF |
| `SEARCH_CONFIG` | `config/search.yml` | Path to search config |
| `BATCH_CLI` | `claude` | CLI used by `--batch` (claude / opencode / gemini / qwen) |
| `BATCH_PROVIDER` | auto-detect | LLM provider for `--evaluate-batch` (overrides auto-detection) |
| `BATCH_MODEL` | per-provider default | Model override for `--evaluate-batch` |
| `OLLAMA_MODEL` | `qwen2.5:32b` | Ollama model used when `BATCH_CLI=opencode` |
| `GEMINI_API_KEY` / `GROQ_API_KEY` / `DEEPINFRA_API_KEY` / `OPENROUTER_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | — | LLM provider keys. Auto-detect order: Gemini → Groq → DeepInfra → OpenRouter → OpenAI → Anthropic. See [QUICKSTART](QUICKSTART.md#which-provider-should-i-pick) for picking one. |
| `OPENAI_BASE_URL` | OpenAI's default | Escape hatch — point the `openai` provider at any OpenAI-compatible endpoint. |

## Screening (opt-in)

Add a `screen:` block to `config/search.yml` to drop dead postings and backfill missing descriptions before bridge:

```yaml
screen:
  liveness: true           # drop 404/410 and "position filled" pages
  liveness_timeout: 8      # seconds per request
```

What the screen step does when `liveness: true`:
- **Pre-screen dedup**: drops URLs already in `scan-history.tsv` / `pipeline.md` / `applications.md` *before* fetching. With 100-result scrapes on a daily cadence ~80% of rows are repeats — this is the biggest cost saving.
- **Liveness check**: HTTP GET per remaining URL; drops 404/410, "no longer available" / "position filled" pages, listing pages, etc.
- **Description backfill**: extracts the JD from the same fetched page (LinkedIn / Indeed / Glassdoor selectors + `<body>` fallback). Lets you set `linkedin_fetch_description: false` and skip thousands of sequential per-job fetches during scrape.
- **Dead URL recording**: URLs that fail liveness get written to `scan-history.tsv` with status `screened-dead` so future runs skip them at the pre-screen step too.

When `liveness: false` the screen stage is a no-op (no dedup, no fetches, no backfill — bridge still does its own dedup pass).

## Evaluation flags

```powershell
.\run.ps1 --batch                              # interactive: agent CLI per BATCH_CLI (default: claude)
.\run.ps1 --evaluate-batch                     # sync API: auto-detected provider, ~3 parallel workers
.\run.ps1 --evaluate-batch --batch-provider gemini --batch-concurrency 5
```

`--batch` is the interactive CLI agent path; `--evaluate-batch` is the synchronous API path. They aren't mutually exclusive — `--batch` runs after the pipeline.

Three flags (mutually exclusive) select a subset of the `searches:` in your config:

- `--only-pass "name1,name2"` — explicit case-insensitive match on the `name:` field. Errors loudly on no match (typo protection).
- `--easy-apply-only` — only passes with `easy_apply: true`. No-ops cleanly if none configured (used by `easy-apply-pipeline.yml`).
- `--no-easy-apply` — only passes without `easy_apply: true` (used by `daily-pipeline.yml`).

The cloud workflows use the `--easy-apply-only` / `--no-easy-apply` pair, not name matching, so your pass `name:` values can be anything you like — the workflows route by the `easy_apply` JobSpy field instead.

## Skipping steps

```bash
./run.sh --skip-scrape         # reuse output/jobs.csv
./run.sh --skip-filter         # reuse output/filtered_jobs.csv
./run.sh --skip-screen         # skip liveness + description backfill (skips pre-screen dedup too)
./run.sh --skip-bridge         # don't push to career-ops
./run.sh --skip-batch-prep     # don't update the evaluation queue

# Re-run only evaluation on an existing queue
./run.sh --skip-scrape --skip-filter --skip-screen --skip-bridge --skip-batch-prep --evaluate-batch
```

## Local triage UI

A local web app for reading and triaging evaluation results, instead of scrolling raw markdown reports. Runs entirely on your machine (FastAPI on localhost) — nothing leaves your computer.

```bash
pip install -r requirements-ui.txt   # one-time: fastapi, uvicorn, markdown

./run-ui.sh                          # serve on :8000, read ./career-ops
./run-ui.sh --data path/to/extracted-artifact   # read a downloaded GHA artifact instead
```
```powershell
.\run-ui.ps1                         # Windows
.\run-ui.ps1 -Data path\to\extracted-artifact
```

Then open http://localhost:8000.

**Views:**
- **Table** — sortable, filterable list of every evaluated role (default sorted by score, high→low); click a row to read its rendered report in a side panel.
- **Board** — kanban by status (Evaluated / Applied / Responded / Interview / Offer / Rejected / Discarded / SKIP). Drag a role between columns to change its status; changes are held locally (marked with a left border) until you push.

Point it at either your local `career-ops/` directory (if you run the pipeline locally) or a GitHub Actions artifact you've downloaded and extracted (the artifact has the same `reports/` + `data/applications.md` layout).

**Cloud buttons** (require the [`gh` CLI](https://cli.github.com) installed + `gh auth login`):
- **↻ Refresh** — downloads the latest `daily-pipeline` artifact via `gh run download` and loads it, so you don't extract by hand. Cached under `.ui-cache/` (gitignored).
- **▶ Run now** — triggers a `daily-pipeline` run in the cloud (`gh workflow run`). It executes on GitHub; click Refresh once it finishes.
- **⇧ Push N changes** — appears once you've made status edits on the board. Pushes them to the cloud tracker via the `edit-tracker` workflow. It first refreshes the latest tracker and applies your changes on top, so roles the pipeline added since your last refresh aren't clobbered.
- **⚙ Setup** — opens the guided onboarding wizard (see below).

`gh` targets the repo of the directory you launch from; set `JOB_SEARCH_REPO=owner/name` to override.

### Guided onboarding (`/onboard`)

Instead of running the CLI setup + `base64` + `gh secret set` by hand, the
**⚙ Setup** wizard does it from the browser. Walk through a short form — upload
your resume PDF, enter target roles / compensation / locations / boards, pick an
LLM provider and paste its API key — and on submit the server:

1. extracts your resume text with `pdfplumber`,
2. generates `profile.yml`, `cv.md`, `_profile.md`, and `search.yml` via
   `setup-profile.mjs --from-json` (same generators as the CLI — one source of truth),
3. base64-encodes them and writes all required **GitHub secrets** (`gh secret set`,
   value piped via stdin so keys never hit argv/logs), plus your provider key and
   `BATCH_PROVIDER` / `BATCH_MODEL` variables.

It **refuses to write to a public repo** (same privacy guard as the workflows),
and the status line up top shows the target repo, its visibility, and whether
it's already configured. After onboarding, click **▶ Run now** to kick off your
first cloud run.

## Requirements

- Python 3.12 (jobspy pins `numpy==1.26.3`, which has no Python 3.13 wheel; setup scripts auto-select 3.12 via `py -3.12` / `python3.12`)
- Node.js 18+ (career-ops and profile setup)
- The agent CLI of your choice for `--batch` — whichever you have installed:
  - `claude` (default): [Claude Code](https://claude.ai/code)
  - `opencode`: [OpenCode](https://opencode.ai)
  - `gemini`: [Gemini CLI](https://github.com/google-gemini/gemini-cli)
  - `qwen`: Qwen CLI

## License

GPL-3.0-or-later. See [LICENSE](LICENSE) for the full text.

Copyright (C) 2026 FrameAutomata

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version. This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.

Note: this repository contains only the orchestrator. [JobSpy](https://github.com/speedyapply/JobSpy) (MIT) and [career-ops](https://github.com/santifer/career-ops) (MIT) are fetched at setup time as separate works under their own licenses.
