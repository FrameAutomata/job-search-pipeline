# Quick Start — Job Search Pipeline

> **Privacy note:** if you plan to run this in the cloud via GitHub Actions, see the [Using this template](README.md#using-this-template) section in the README first. The short version: clone this template into your own **private** repo before enabling Actions. Local execution has no such concern — everything stays on your machine.

---

## What this does

Scrapes job boards, filters results against your resume, optionally pre-screens for liveness + dedup + LinkedIn description backfill, then feeds surviving jobs into [career-ops](https://github.com/santifer/career-ops) for AI-powered evaluation. Everything after setup is a single command.

---

## Prerequisites

- Python 3.12 (jobspy pins `numpy==1.26.3`, no 3.13 wheel)
- Node.js 20+ (career-ops + profile setup — career-ops pins `playwright@1.62.1`, which requires Node 20)
- A resume — DOCX, ODT, or PDF (DOCX recommended: per-job resume tailoring slot-edits a DOCX, and editable formats extract more cleanly than PDF)
- At least one of:
  - An agent CLI for `--batch` (interactive evaluation): [Claude Code](https://claude.ai/code) (default), [OpenCode](https://opencode.ai), [Gemini CLI](https://github.com/google-gemini/gemini-cli), or Qwen CLI — any of these can be backed by a local [Ollama](https://ollama.com) model via `OLLAMA_MODEL`
  - An LLM API key for `--evaluate-batch` (synchronous parallel evaluation). Free-tier options: `GEMINI_API_KEY`, `GROQ_API_KEY`. Pay-as-you-go open-weight options: `DEEPINFRA_API_KEY`, `OPENROUTER_API_KEY`, `DEEPSEEK_API_KEY`. Frontier paid: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`. See the "Which provider should I pick?" section below for choosing among them.

---

## Setup (one-time)

Setup is two steps: **install the pipeline**, then **create your profile**.

### Step 1 — install

```powershell
# Windows
.\setup.ps1

# macOS / Linux
./setup.sh
```

The setup script:
1. Creates a Python 3.12 virtual environment and installs dependencies (pipeline + local UI)
2. Clones career-ops into `./career-ops/`
3. Installs the Node dependencies (career-ops + pipeline)
4. Copies `.env.example` → `.env` and `config/search.example.yml` → `config/search.yml`, and creates `resumes/` and `output/`

It does **not** ask for your details — you create your profile in step 2.

### Step 2 — create your profile

Your profile (CV + candidate details + `searches:` config) is generated from your resume and a few answers. Pick one path:

#### Option A — guided wizard in the browser (recommended)

The wizard generates your profile **and** writes it to your private repo's GitHub secrets, so it's the path to use if you want the scheduled cloud automation.

1. **Install the GitHub CLI** ([cli.github.com](https://cli.github.com)) and sign in: `gh auth login`. (Needed only for writing the cloud secrets — skip it if you'll run locally only, and use Option B instead.)
2. **Launch the UI from a clone of your private copy.** `gh` writes secrets to whatever repo the launch directory points at, so run the wizard from inside your private copy (see [Cloud automation](#cloud-automation-github-actions) for how to create it). Override the target with `JOB_SEARCH_REPO=owner/name`.
3. **Start the UI and open the wizard:**
   ```powershell
   .\run-ui.ps1        # macOS / Linux: ./run-ui.sh
   ```
   Open http://localhost:8000 and click **⚙ Setup**. The status line up top shows the target repo, its visibility, and whether it's already configured.
4. **Walk through the steps:**
   - **Resume** — upload your resume as DOCX, ODT, or PDF (DOCX recommended — per-job tailoring slot-edits a DOCX). Text is extracted locally; the About step is auto-filled from it
   - **About you** — name, email, location, optional phone / LinkedIn / GitHub / website
   - **Roles & compensation** — target roles, roles to avoid, target / minimum comp, location flexibility
   - **Search settings** — locations (`City, ST` pairs stay together; put "Remote" in a chunk for a remote pass), distance, recency (`hours_old`), max results, job boards, and an optional easy-apply pass (runs every 4 h in the cloud)
   - **Career narrative** *(optional, improves evaluations)* — transition story, deal-breakers, portfolio
   - **AI evaluation provider** — pick a provider and paste its API key (piped straight to a GitHub secret; never logged or stored locally)
   - **Review & submit** — generates the profile artifacts locally and, when `gh` targets a **private** repo, writes them plus your API key as encrypted GitHub secrets. It refuses to write to a public repo. On the last step the **Finish** button returns you to the triage UI.

#### Option B — terminal prompts

Generates the profile **locally only** — no GitHub secrets are written (use this for local runs, or run it before adding cloud secrets by hand):

```powershell
node setup-profile.mjs
```

You'll be prompted for:
- Your resume path (auto-detected if present in `resumes/`)
- Target roles and roles to avoid (comma-separated)
- Compensation expectations (target / minimum)
- **Locations to search** (comma-separated, smart-parsed for `City, ST` pairs; e.g. `"US Remote, Dallas, TX, Fort Worth, TX"` → three passes)
- **Distance** (in miles) from each non-remote location
- **How recent** results should be (`hours_old`, default 24)
- **Max results** per site per search term (default 100)
- **Which job boards** to scrape — Indeed and/or LinkedIn, the only two supported boards (Glassdoor/ZipRecruiter are Cloudflare-blocked; Google Jobs drops connections that crash the scraper)
- Whether to include an **easy-apply pass** (runs on a separate 4 h cloud schedule)

### What setup produces

Either path creates / rewrites:
- `career-ops/config/profile.yml` — your candidate profile
- `career-ops/cv.md` — your CV in markdown
- `career-ops/modes/_profile.md` — your career narrative and deal-breakers
- `config/search.yml` — `searches:` block regenerated from your locations; `filter:` / `screen:` blocks preserved

The wizard (Option A) also saves your uploaded resume under its own extension — `resumes/resume.docx` / `.odt` / `.pdf` — plus `resumes/resume.txt` (the extracted text used for keyword scoring). A saved `resume.docx` doubles as the source resume tailoring slot-edits per job (the `--handoff-tailor` work-order enrichment). Option B reads the resume path you point it at.

To re-run profile setup at any time, re-open the wizard or run `node setup-profile.mjs`. Re-running rewrites the `searches:` block from scratch.

You're now ready to [run the pipeline](#run-the-pipeline) locally, or to enable the [cloud automation](#cloud-automation-github-actions).

---

## Configure your search

Edit `config/search.yml`:

```yaml
searches:
  - name: "my search"            # used by --only-pass to select this entry
    search_terms:
      - "software engineer"
    sites: [indeed, linkedin]     # the only supported boards (others are blocked or crash the scraper)
    location: "Dallas, TX"
    results_wanted: 100          # 100 is typical; bigger numbers are slow
    hours_old: 24
    linkedin_fetch_description: false  # screen stage backfills the JD instead

filter:
  target_titles:
    - "software engineer"
    - "backend engineer"
  negative_titles:
    - "senior"
    - "staff"
  min_score: 5

screen:
  liveness: true                 # recommended — also enables pre-screen dedup + JD backfill
  liveness_timeout: 8
```

`linkedin_fetch_description: true` makes JobSpy fetch each LinkedIn JD individually during scrape — a sequential per-job request that can take hours on 1000+ results. Keep it `false`; the screen stage backfills LinkedIn descriptions via LinkedIn's public guest job-posting endpoint (full JD, no login wall) for the small set of jobs that survive filtering.

---

## Run the pipeline

Two evaluation modes, pick one:

```powershell
# Windows — interactive CLI agent (Claude Code is the default)
.\run.ps1 --batch

# Synchronous API evaluation (auto-detects provider from env keys; Gemini free tier is fine)
.\run.ps1 --evaluate-batch
```

```bash
# macOS / Linux — same flags
./run.sh --evaluate-batch
```

This runs five stages in sequence, then evaluates:

| Stage | What it does |
|-------|--------------|
| Scrape | Hits job boards, writes `output/jobs.csv` |
| Filter | Scores by keyword + title match using precompiled regex alternations; writes `output/filtered_jobs.csv` |
| Screen | *(opt-in via `screen.liveness: true`)* Pre-dedups against scan-history before HTTP fetch; drops expired/filled postings; backfills missing LinkedIn descriptions; records dead URLs as `screened-dead` |
| Bridge | Pushes new jobs into `career-ops/data/pipeline.md`, deduped against scan-history + applications.md |
| Batch prep | Writes `career-ops/batch/batch-input.tsv` and `batch/jds/{id}.txt` |

`--batch` is the interactive CLI path and runs after the pipeline stages; `--evaluate-batch` is the synchronous API path. They aren't mutually exclusive.

Results land in:
- Reports → `career-ops/reports/{num}-{company}-{date}.md`
- Tracker lines → `career-ops/batch/tracker-additions/{id}.tsv` (merged into `applications.md` by `node merge-tracker.mjs`, which the batch evaluator/retriever runs automatically)

---

## Evaluation options

### `--batch` — interactive CLI agent

```powershell
.\run.ps1 --batch
```

Invokes `career-ops/batch/batch-runner.sh` using the CLI set by `BATCH_CLI` (default: `claude`). Supported: `claude`, `opencode`, `gemini`, `qwen`. Runs locally — the agent can read files, call WebSearch, and generate PDFs.

```bash
# Use a local Ollama model with any supported CLI (e.g. Claude Code or OpenCode)
OLLAMA_MODEL=qwen2.5:32b ./run.sh --batch
BATCH_CLI=opencode OLLAMA_MODEL=qwen2.5:32b ./run.sh --batch
```

State persists in `career-ops/batch/batch-state.tsv` — safe to interrupt and resume.

### `--evaluate-batch` — synchronous API (multi-provider)

```powershell
.\run.ps1 --evaluate-batch                              # auto-detect provider
.\run.ps1 --evaluate-batch --batch-provider gemini      # explicit
.\run.ps1 --evaluate-batch --batch-concurrency 5        # more parallel workers
```

Provider auto-detection order: Gemini → Groq → DeepInfra → OpenRouter → DeepSeek → OpenAI → Anthropic. The first one with a configured API key wins. Override with `BATCH_PROVIDER`.

| Provider | Env var | Default model | Cost | Notes |
|---|---|---|---|---|
| Anthropic | `ANTHROPIC_API_KEY` | `claude-sonnet-4-6` | Frontier paid | Closed-weights, established reputation for structured-output tasks. |
| OpenAI | `OPENAI_API_KEY` | `gpt-4o-mini` | Frontier paid (mini is cheaper) | Closed-weights. `gpt-4o-mini` is competitively priced; `gpt-4o` is frontier-tier. |
| Gemini | `GEMINI_API_KEY` | `gemini-2.5-flash` | Free tier with per-model RPD ceilings | Check your per-model limits at aistudio.google.com/rate-limit. Default model has a low RPD (~20/day); `gemma-4-26b-a4b-it` has 1,500/day. The batch run warns if a run would exceed the cap. |
| Groq | `GROQ_API_KEY` | `llama-3.3-70b-versatile` | Free tier with tight TPM ceiling | Fast inference, but the per-minute token limit binds tightly on our large prompts — best for small runs. |
| DeepInfra | `DEEPINFRA_API_KEY` | `deepseek-ai/DeepSeek-V4-Flash` | Pay-as-you-go (cheaper than frontier) | Hosted open-weight models. Pricing typically a fraction of frontier API rates per token. |
| OpenRouter | `OPENROUTER_API_KEY` | `meta-llama/llama-3.3-70b-instruct` | Pay-as-you-go (varies by model) | Meta-aggregator — one key, switch models via `BATCH_MODEL`. Pricing varies by which backend model you select. |
| DeepSeek | `DEEPSEEK_API_KEY` | `deepseek-chat` | Pay-as-you-go (cheaper than frontier) | DeepSeek's first-party API — cheaper than DeepInfra hosting the same open weights. Sign up at platform.deepseek.com. |
| Ollama | `OLLAMA_BASE_URL` | `qwen2.5:32b` | Free (you operate the server) | Local self-hosted. Not reachable from GHA cloud workflow without exposing the server publicly. |

For current per-token pricing, check each provider's pricing page — rates change too often to enumerate here reliably.

### Which provider should I pick?

There are three real factors to weigh: **cost**, **output quality**, and **operational reliability** (rate limits, uptime, deprecation cadence). No single provider wins on all three; pick based on what matters most for your situation.

- **Just trying things out / learning the pipeline.** Pick something with established output consistency so you can tell pipeline problems from LLM problems. Anthropic (`claude-sonnet-4-6`) or OpenAI (`gpt-4o-mini`) are good defaults — they cost money but you only need a few dollars to validate end-to-end.
- **Free, small daily volume.** Gemini's free tier handles small runs (~20 evaluations/day on the default model, ~500/day with `BATCH_MODEL=gemini-3.1-flash-lite`, ~1,500/day with `gemma-4-26b-a4b-it`). Check your specific per-model limits before relying on a number — and the batch run warns if a run would exceed the cap.
- **Free, willing to navigate quotas.** Groq's free tier is fast but the TPM ceiling is tight against our large prompts. Workable for small runs, not for daily 100+ job evaluations.
- **Active job search, cost matters at scale.** Hosted open-weight providers (DeepInfra, OpenRouter) typically run a fraction of frontier API per-token rates. Worth using after you've validated the pipeline against a frontier baseline so you know what "good output" looks like — open-weight models like Llama 3.3 70B are capable but occasionally weaker on the more nuanced report sections.
- **A/B testing several models.** OpenRouter — single API key, dozens of backends accessible via `BATCH_MODEL`.
- **Maximum control / data stays local.** Ollama for local runs. Or set `OPENAI_BASE_URL` to point the `openai` provider at your own vLLM / TGI / LM Studio endpoint.

Override the model with `BATCH_MODEL=...` in `.env` or `--batch-model <name>`.

---

## Selecting specific search passes

Three mutually-exclusive flags subset the `searches:` in your config:

- `--only-pass "name1,name2"` — explicit name match (case-insensitive). Errors on no match, so it catches typos like `--only-pass "easyy apply"`.
- `--easy-apply-only` — only passes with `easy_apply: true`. Clean no-op if none configured.
- `--no-easy-apply` — only passes without `easy_apply: true`.

```bash
./run.sh --evaluate-batch --only-pass "easy apply"
./run.sh --evaluate-batch --no-easy-apply
```

These flags are for ad-hoc local runs. The daily cloud workflow runs **every** pass once a day with no selection flag, so your pass `name:` values can be anything.

---

## Skip flags

```powershell
.\run.ps1 --skip-scrape           # reuse existing output/jobs.csv
.\run.ps1 --skip-filter           # reuse existing output/filtered_jobs.csv
.\run.ps1 --skip-screen           # skip liveness + JD backfill (and pre-screen dedup)
.\run.ps1 --skip-bridge           # don't push to career-ops
.\run.ps1 --skip-batch-prep       # don't update the evaluation queue

# Re-run evaluation only (skip the full pipeline)
.\run.ps1 --skip-scrape --skip-filter --skip-screen --skip-bridge --skip-batch-prep --evaluate-batch
```

> **What `--skip-scrape` reuses may be empty.** A scrape that returns zero rows
> truncates `output/jobs.csv` instead of leaving the previous run's rows to be
> re-processed as today's. A rate-limited run looks the same as a genuinely empty
> one — JobSpy returns no rows rather than raising, and the run exits 0 — so if a
> re-drive produces nothing, check whether `output/jobs.csv` is 0 bytes. To tell a
> throttle from a real empty day, look further up the same log: JobSpy logs
> `429 Response - Blocked by LinkedIn for too many requests` or Indeed's
> `responded with status code: 403` when a board turns it away. `--skip-filter` and
> `output/filtered_jobs.csv` carry the same caveat.

---

## Cloud automation (GitHub Actions)

The repo ships two scheduled workflows + four manual (`workflow_dispatch`) ones, plus `tests.yml` on pull requests. **They refuse to run unless your fork is private.** See the README's [Using this template](README.md#using-this-template) section for setup.

| Workflow | Schedule | What it does |
|---|---|---|
| `daily-pipeline.yml` | Noon UTC | Runs **every** search pass (including any `easy_apply: true` pass) once a day. |
| `gc-actions-storage.yml` | Sundays 03:30 UTC | Prunes old artifacts and workflow run logs, which share your account's Actions **storage** quota. |
| `edit-tracker.yml` | Manual (`workflow_dispatch`) | Replaces `applications.md` in the cache with a base64 blob — for status edits without committing the file. |
| `export-reports.yml` | Manual (`workflow_dispatch`) | Packages the **full** report history, plus the current tracker, from the cache as one download. Run it when setting up a new machine, or after going longer than the 7-day artifact retention without a Refresh. |
| `seed-reports.yml` | Manual (`workflow_dispatch`) | The mirror: repairs the cache's `reports/` from past artifacts. Writes to the state cache, so it shares a concurrency group with the daily and `edit-tracker` — dispatch it during a pipeline run and it queues behind it rather than overwriting it. |
| `update-from-template.yml` | Manual (`workflow_dispatch`) | Merges the upstream template's latest `main` into your copy. |

All runtime state (scan-history, applications.md, pipeline.md, batch state, and the accumulated `reports/`) lives in `actions/cache`. Each run additionally uploads **its own** new reports plus the current tracker as an `actions/upload-artifact` (7-day retention) — a per-run delta, not the whole history, so artifact storage stays bounded however long the pipeline has been running. To pull the whole history down once (new machine, or a long gap between Refreshes), run **Export Reports**. No user data is ever committed. Every workflow that *writes* that cache — the daily, `edit-tracker`, `seed-reports` — shares one concurrency group, so they queue rather than overwriting each other; only one run may be queued at a time, so a second dispatch while one is waiting cancels the waiting one.

---

## Key files

```
job-search-pipeline/
├── config/search.yml            # Search terms, filter rules, screen settings
├── .env                         # Paths and model/provider overrides
├── output/
│   ├── jobs.csv                 # Raw scrape output
│   ├── filtered_jobs.csv        # After filter (and screen if enabled)
│   └── _keywords.json           # YAKE cache keyed by resume sha
└── career-ops/
    ├── config/profile.yml       # Your candidate profile
    ├── cv.md                    # Your CV
    ├── modes/_profile.md        # Career narrative and deal-breakers
    ├── data/
    │   ├── pipeline.md          # Jobs queued for evaluation
    │   ├── scan-history.tsv     # Dedup record (statuses: added, screened-dead)
    │   └── applications.md      # Your application tracker
    ├── batch/
    │   ├── batch-input.tsv      # Evaluation queue
    │   ├── batch-state.tsv      # Interactive --batch progress (resumable)
    │   ├── batch-api-state.json # --evaluate-batch state
    │   ├── jds/                 # Cached job descriptions
    │   └── tracker-additions/   # Pending tracker lines (merged by merge-tracker.mjs)
    └── reports/                 # Full A–G evaluation reports
```

---

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `CAREER_OPS_PATH` | `./career-ops` | Path to career-ops directory |
| `RESUME_PATH` | auto-detected | Path to your resume (DOCX / ODT / PDF). Unset → auto-discovers `resumes/resume.{pdf,docx,odt}`. A `.txt` sibling, if present, is used directly and skips extraction. |
| `SEARCH_CONFIG` | `config/search.yml` | Path to search config |
| `BATCH_CLI` | `claude` | CLI used by `--batch` (claude / opencode / gemini / qwen) |
| `BATCH_PROVIDER` | auto-detect | LLM provider for `--evaluate-batch` (overrides detection) |
| `BATCH_MODEL` | per-provider default | Model name for `--evaluate-batch` |
| `OLLAMA_MODEL` | `qwen2.5:32b` | Model name passed as `--model` to whichever CLI `--batch` uses (works with any `BATCH_CLI`) |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama endpoint for `--evaluate-batch --batch-provider ollama` |
| `GEMINI_API_KEY` / `GROQ_API_KEY` / `DEEPINFRA_API_KEY` / `OPENROUTER_API_KEY` / `DEEPSEEK_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | — | LLM provider keys. Auto-detect order: Gemini → Groq → DeepInfra → OpenRouter → DeepSeek → OpenAI → Anthropic. |
| `OPENAI_BASE_URL` | OpenAI default | Escape hatch — point the `openai` provider at any OpenAI-compatible endpoint (local vLLM, custom proxy, etc.) |
| `SKILL_PATH_DEFAULT` | `ask` | Default path for career-ops skills run from the triage UI (résumé tailoring, etc.): `ask` (pick each time), `api` (always the provider call), or `cli` (always hand off to your agent). See the [README UI section](README.md#running-career-ops-skills-from-the-ui). |
