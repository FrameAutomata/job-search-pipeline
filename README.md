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

You need to (a) get your profile + API key into the repo's **GitHub secrets**, and (b) **enable Actions**. The guided wizard does (a) for you; there's also a manual path.

**First, clone your private copy and install locally** (the wizard runs on your machine and pushes secrets to this repo):

```bash
git clone <your-private-copy-url>
cd <your-private-copy>
./setup.sh          # Windows: .\setup.ps1
```

#### Option A — guided wizard (recommended)

1. Install the [GitHub CLI](https://cli.github.com) and run `gh auth login` (the wizard uses it to write secrets).
2. From inside your private copy, run `./run-ui.sh` (Windows: `.\run-ui.ps1`), open http://localhost:8000, and click **⚙ Setup**.
3. Walk through the wizard (resume → about → roles/comp → search settings → optional narrative → provider + API key → review). On submit it generates your profile and writes every required secret to *this* repo. It refuses to write to a public repo.

See [Guided onboarding](#guided-onboarding-onboard) below and the [QUICKSTART setup steps](QUICKSTART.md#step-2--create-your-profile) for the full walkthrough.

#### Option B — set the secrets by hand

First generate the profile artifacts locally with `node setup-profile.mjs`. That writes `cv.md`, `profile.yml`, `_profile.md`, and `search.yml`, but **not** `resumes/resume.txt` — create that yourself from your resume's plain text (e.g. `pdftotext resume.pdf resumes/resume.txt`, or copy-paste the text into the file). Then in your private copy on github.com go to **Settings → Secrets and variables → Actions → New repository secret** and add one base64-encoded secret per file:

   | Secret | File to encode |
   |--------|----------------|
   | `CV_MD_B64` | `career-ops/cv.md` |
   | `PROFILE_YML_B64` | `career-ops/config/profile.yml` |
   | `SEARCH_CONFIG_B64` | `config/search.yml` |
   | `RESUME_TXT_B64` | `resumes/resume.txt` (the file you created above) |
   | `PROFILE_MD_B64` *(optional)* | `career-ops/modes/_profile.md` |
   | `ARTICLE_DIGEST_B64` *(optional)* | `career-ops/article-digest.md` |

   Plus at least one LLM API key secret: `GEMINI_API_KEY` (free tier) / `GROQ_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`.

   **Encoding a file to base64** (paste the output as the secret value) — use the command for your OS:

   ```powershell
   # Windows / PowerShell — do NOT use `certutil -encode`; it wraps the output in
   # -----BEGIN CERTIFICATE----- lines that aren't valid base64 and break the run.
   [Convert]::ToBase64String([IO.File]::ReadAllBytes("career-ops\cv.md"))
   ```
   ```bash
   # macOS (BSD base64 has no -w; strip newlines instead)
   base64 -i career-ops/cv.md | tr -d '\n'
   ```
   ```bash
   # Linux (GNU coreutils)
   base64 -w0 career-ops/cv.md
   ```

   This manual path is easy to get wrong (the encoding footgun above is the usual cause of a skipped pipeline run). The **guided wizard (Option A) encodes everything correctly for you** — prefer it unless you specifically need the manual route.

#### Then, either way

1. **Actions tab → "I understand my workflows, go ahead and enable them".**
2. **Actions → Daily Job Pipeline → Run workflow** to do a test run before the scheduled cron fires. (A successful daily run also kicks off an easy-apply run immediately after, if you configured an easy-apply pass.)
3. **Read results**: Actions tab → open the run → download the `pipeline-output-*` artifact, or use the local UI's **↻ Refresh** button to pull it without leaving the browser.

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
.\setup.ps1                  # creates venv, installs deps (incl. UI), clones career-ops, copies example configs
node setup-profile.mjs       # generate your profile from your resume (or use the UI wizard — see below)
# setup-profile.mjs writes config\search.yml; tweak it if you like, then pick an evaluation mode:
.\run.ps1 --batch            # interactive CLI agent (default: claude)
.\run.ps1 --evaluate-batch   # API-driven (free Gemini if GEMINI_API_KEY set in .env)
```

```bash
# macOS / Linux
git clone <this repo>
cd job-search-pipeline
./setup.sh
node setup-profile.mjs
./run.sh --evaluate-batch
```

Profile setup is required before the first run — it produces your CV, candidate profile, and `searches:` config from your resume. You can do it in the terminal (`node setup-profile.mjs`, above) or in the browser via the **⚙ Setup** wizard ([Guided onboarding](#guided-onboarding-onboard)); the wizard additionally writes GitHub secrets for cloud runs. For `--evaluate-batch`, put at least one LLM API key in `.env` (e.g. `GEMINI_API_KEY=...`).

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
| `SKILL_PATH_DEFAULT` | `ask` | Default path for career-ops skills run from the UI: `ask` (choose each time), `api`, or `cli`. |

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
- **↻ Refresh** — downloads the most recent *successful* pipeline artifact (daily **or** easy-apply, whichever ran later) via `gh run download` and loads it, so you don't extract by hand. Cached under `.ui-cache/` (gitignored).
- **▶ Run now** — triggers a `daily-pipeline` run in the cloud (`gh workflow run`). It executes on GitHub; click Refresh once it finishes.
- **⇧ Push N changes** — appears once you've made status edits on the board. Pushes them to the cloud tracker via the `edit-tracker` workflow. It first refreshes the latest tracker and applies your changes on top, so roles the pipeline added since your last refresh aren't clobbered.
- **⚙ Setup** — opens the guided onboarding wizard (see below).

`gh` targets the repo of the directory you launch from; set `JOB_SEARCH_REPO=owner/name` to override.

### Running career-ops skills from the UI

After the cloud pipeline has scraped and scored, open a role's report in the side panel — each report has a row of **skill actions** that run the matching career-ops mode for that role:

| Action | What it does | Runs via |
|--------|--------------|----------|
| **Tailor résumé (Markdown)** | JD-matched résumé as `.md` you can drop into your own format | API **or** CLI |
| **Tailor résumé (PDF)** | ATS-optimized PDF (rendered with Playwright) | CLI only |
| **Interview prep** | company/role interview intel with live web research | CLI only |
| **Apply assistant** | reads the application form in your browser and drafts answers | CLI only |

Each skill runs one of two ways, and you choose per action:

- **API** — a bounded, synchronous provider call (uses the LLM keys you already configured). Zero install, finishes in place, returns a downloadable file. No live web research or back-and-forth — so only the résumé-markdown skill offers it.
- **CLI** — hands you a ready-to-run command for your agent (`BATCH_CLI`, default `claude`). Interactive, can pull the live JD, search the web, and drive a browser. The result panel shows the command with **Copy command** and **▶ Run in terminal**, which opens a new console window with the command pre-loaded (Windows → `cmd`, macOS → `Terminal.app`, Linux → the first emulator on PATH from a known list, or whatever `$TERMINAL` names). The agent always runs in *your* terminal, where its tools and cost are visible — we never run it inside the UI process. Every skill supports this path.

The UI shows whichever paths each skill can use (an agent CLI on your PATH, an API key, or both). Set **`SKILL_PATH_DEFAULT=api|cli`** in `.env` to skip the chooser; leave it `ask` (default) to pick each time — handy if you'd rather spend an agent-CLI membership than API credits, or use the API for batch scoring and the CLI for tailoring.

**Which should I use?**
- *API* — fast, no install, good for tailoring résumés at volume; pick this if you don't want to install an agent. (Résumé-markdown only.)
- *CLI* — interactive, with live web research and a real browser. Required for **PDF**, **interview-prep**, and the live **apply** assistant (they need tools the API path can't provide), and best when you want to iterate or pull the freshest JD.
- *Both* — API for quick tailoring, CLI for depth and the browser-driven skills. Recommended for an active search.

**One-time setup for the browser-driven skills** (Apply assistant + PDF résumé). The UI surfaces these inline when you run the affected skill, but the same info up front:

- **Apply assistant** drives a live browser; your agent needs the **Playwright MCP server** registered. For Claude Code:
  ```bash
  claude mcp add playwright -- npx -y @playwright/mcp@latest
  ```
  Other agents have an equivalent — check their MCP docs. Without this, claude says it "doesn't have Playwright in this session" and falls back to asking for a screenshot.
- **PDF résumé** (and the Apply assistant the first time it drives the browser) need **Chromium installed locally**, from inside the career-ops clone:
  ```bash
  cd career-ops
  npx playwright install chromium
  ```
  (Skipped by `setup.ps1`/`setup.sh` so you don't pay the ~150 MB download unless you actually use these skills.)

> Security: the UI is localhost-only and now refuses cross-origin state-changing requests, so a web page you have open can't trigger skill runs, cloud actions, or secret writes behind your back.

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
