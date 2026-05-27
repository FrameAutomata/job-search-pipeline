# Quick Start — Job Search Pipeline

> **Privacy note:** if you plan to run this in the cloud via GitHub Actions, see the [Using this template](README.md#using-this-template) section in the README first. The short version: clone this template into your own **private** repo before enabling Actions. Local execution has no such concern — everything stays on your machine.

---

## What this does

Scrapes job boards, filters results against your resume, optionally pre-screens for liveness + dedup + LinkedIn description backfill, then feeds surviving jobs into [career-ops](https://github.com/santifer/career-ops) for AI-powered evaluation. Everything after setup is a single command.

---

## Prerequisites

- Python 3.12 (jobspy pins `numpy==1.26.3`, no 3.13 wheel)
- Node.js 18+ (career-ops + profile setup)
- A resume PDF
- At least one of:
  - An agent CLI for `--batch` (interactive evaluation): [Claude Code](https://claude.ai/code) (default), [OpenCode](https://opencode.ai) + [Ollama](https://ollama.com), [Gemini CLI](https://github.com/google-gemini/gemini-cli), or Qwen CLI
  - An LLM API key for `--evaluate-batch` (synchronous parallel evaluation). Free-tier options: `GEMINI_API_KEY`, `GROQ_API_KEY`. Pay-as-you-go open-weight options: `DEEPINFRA_API_KEY`, `OPENROUTER_API_KEY`. Frontier paid: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`. See the "Which provider should I pick?" section below for choosing among them.

---

## Setup (one-time)

```powershell
# Windows
.\setup.ps1

# macOS / Linux
./setup.sh
```

The setup script:
1. Creates a Python virtual environment and installs dependencies
2. Clones career-ops into `./career-ops/`
3. Copies `config/search.example.yml` → `config/search.yml`
4. Runs `setup-profile.mjs` to generate your candidate profile

During profile setup you'll be prompted for:
- Your resume path (auto-detected if present in `resumes/`)
- Target roles and roles to avoid (comma-separated)
- Compensation expectations (target / minimum)
- **Locations to search** (comma-separated, smart-parsed for `City, ST` pairs; e.g. `"US Remote, Dallas, TX, Fort Worth, TX"` → three passes)
- **Distance** (in miles) from each non-remote location
- **How recent** results should be (`hours_old`, default 24)
- **Max results** per site per search term (default 100)
- **Which job boards** to scrape (default linkedin, indeed, glassdoor)
- Whether to include an **easy-apply pass** (runs on a separate 4 h cloud schedule)

This creates / rewrites:
- `career-ops/config/profile.yml` — your candidate profile
- `career-ops/cv.md` — your CV in markdown
- `career-ops/modes/_profile.md` — your career narrative and deal-breakers
- `config/search.yml` — `searches:` block regenerated from your location prompts; `filter:` / `screen:` blocks preserved

To re-run profile setup at any time: `node setup-profile.mjs`. Re-running rewrites the `searches:` block from scratch.

---

## Configure your search

Edit `config/search.yml`:

```yaml
searches:
  - name: "my search"            # used by --only-pass to select this entry
    search_terms:
      - "software engineer"
    sites: [linkedin, indeed, glassdoor]
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

`linkedin_fetch_description: true` makes JobSpy fetch each LinkedIn JD individually during scrape — a sequential per-job request that can take hours on 1000+ results. Keep it `false`; the screen stage extracts the JD from the same page it already fetches for the liveness check.

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
# Use OpenCode + a local Ollama model instead of Claude
BATCH_CLI=opencode OLLAMA_MODEL=qwen2.5:32b ./run.sh --batch
```

State persists in `career-ops/batch/batch-state.tsv` — safe to interrupt and resume.

### `--evaluate-batch` — synchronous API (multi-provider)

```powershell
.\run.ps1 --evaluate-batch                              # auto-detect provider
.\run.ps1 --evaluate-batch --batch-provider gemini      # explicit
.\run.ps1 --evaluate-batch --batch-concurrency 5        # more parallel workers
```

Provider auto-detection order: Gemini → Groq → DeepInfra → OpenRouter → OpenAI → Anthropic. The first one with a configured API key wins. Override with `BATCH_PROVIDER`.

| Provider | Env var | Default model | Cost | Notes |
|---|---|---|---|---|
| Anthropic | `ANTHROPIC_API_KEY` | `claude-sonnet-4-6` | Frontier paid | Closed-weights, established reputation for structured-output tasks. |
| OpenAI | `OPENAI_API_KEY` | `gpt-4o-mini` | Frontier paid (mini is cheaper) | Closed-weights. `gpt-4o-mini` is competitively priced; `gpt-4o` is frontier-tier. |
| Gemini | `GEMINI_API_KEY` | `gemini-2.5-flash` | Free tier with per-model RPD ceilings | Check your per-model limits at aistudio.google.com/usage. Default model has a low RPD; `gemma-4-26b-it` has more headroom. |
| Groq | `GROQ_API_KEY` | `llama-3.3-70b-versatile` | Free tier with tight TPM ceiling | Fast inference, but the per-minute token limit binds tightly on our large prompts — best for small runs. |
| DeepInfra | `DEEPINFRA_API_KEY` | `meta-llama/Llama-3.3-70B-Instruct` | Pay-as-you-go (cheaper than frontier) | Hosted open-weight models. Pricing typically a fraction of frontier API rates per token. |
| OpenRouter | `OPENROUTER_API_KEY` | `meta-llama/llama-3.3-70b-instruct` | Pay-as-you-go (varies by model) | Meta-aggregator — one key, switch models via `BATCH_MODEL`. Pricing varies by which backend model you select. |
| Ollama | `OLLAMA_BASE_URL` | `qwen2.5:32b` | Free (you operate the server) | Local self-hosted. Not reachable from GHA cloud workflow without exposing the server publicly. |

For current per-token pricing, check each provider's pricing page — rates change too often to enumerate here reliably.

### Which provider should I pick?

There are three real factors to weigh: **cost**, **output quality**, and **operational reliability** (rate limits, uptime, deprecation cadence). No single provider wins on all three; pick based on what matters most for your situation.

- **Just trying things out / learning the pipeline.** Pick something with established output consistency so you can tell pipeline problems from LLM problems. Anthropic (`claude-sonnet-4-6`) or OpenAI (`gpt-4o-mini`) are good defaults — they cost money but you only need a few dollars to validate end-to-end.
- **Free, small daily volume.** Gemini's free tier handles small runs (~20 evaluations/day on the default model, ~500/day with `BATCH_MODEL=gemini-3.1-flash-lite`, ~1500/day with `gemma-4-26b-it`). Check your specific per-model limits before relying on a number.
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

The cloud workflows use the `easy_apply` routing pair — `daily-pipeline.yml` runs everything except easy-apply, `easy-apply-pipeline.yml` runs just the easy-apply pass. This means your pass `name:` values can be anything; the workflows route by the JobSpy `easy_apply` field, not by name.

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

---

## Cloud automation (GitHub Actions)

The repo ships three scheduled workflows + one manual workflow. **They refuse to run unless your fork is private.** See the README's [Using this template](README.md#using-this-template) section for setup.

| Workflow | Schedule | What it does |
|---|---|---|
| `daily-pipeline.yml` | Noon UTC | Runs every pass without `easy_apply: true` via `--no-easy-apply`. |
| `easy-apply-pipeline.yml` | Every 4 h at 02/06/10/14/18/22 UTC | Runs every pass with `easy_apply: true` via `--easy-apply-only`. No-ops if none configured. |
| `edit-tracker.yml` | Manual (`workflow_dispatch`) | Replaces `applications.md` in the cache with a base64 blob — for status edits without committing the file. |

All runtime state (scan-history, applications.md, batch state) lives in `actions/cache@v4`. Reports and tracker snapshots are uploaded as `actions/upload-artifact@v4` (90-day retention). No user data is ever committed.

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
| `RESUME_PATH` | auto-detected | Path to your resume PDF (or `.txt` sibling if present — skips PDF extraction) |
| `SEARCH_CONFIG` | `config/search.yml` | Path to search config |
| `BATCH_CLI` | `claude` | CLI used by `--batch` (claude / opencode / gemini / qwen) |
| `BATCH_PROVIDER` | auto-detect | LLM provider for `--evaluate-batch` (overrides detection) |
| `BATCH_MODEL` | per-provider default | Model name for `--evaluate-batch` |
| `OLLAMA_MODEL` | `qwen2.5:32b` | Local model passed to `--batch` when `BATCH_CLI=opencode` |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama endpoint for `--evaluate-batch --batch-provider ollama` |
| `GEMINI_API_KEY` / `GROQ_API_KEY` / `DEEPINFRA_API_KEY` / `OPENROUTER_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | — | LLM provider keys. Auto-detect order: Gemini → Groq → DeepInfra → OpenRouter → OpenAI → Anthropic. |
| `OPENAI_BASE_URL` | OpenAI default | Escape hatch — point the `openai` provider at any OpenAI-compatible endpoint (local vLLM, custom proxy, etc.) |
