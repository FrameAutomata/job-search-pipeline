# Quick Start — Job Search Pipeline

---

## What this does

Scrapes job boards, filters results against your resume, optionally pre-screens for liveness and fit, then feeds surviving jobs into [career-ops](https://github.com/santifer/career-ops) for AI-powered evaluation. Everything after setup is a single command.

---

## Prerequisites

- Python 3.12
- Node.js 18+
- [Ollama](https://ollama.com) (for local evaluation and optional screening)
- [OpenCode](https://opencode.ai) (for `--batch` evaluation)
- A resume PDF

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

During profile setup you'll be asked for your resume path, target roles, location, and compensation expectations. This creates:
- `career-ops/config/profile.yml` — your candidate profile
- `career-ops/cv.md` — your CV in markdown
- `career-ops/modes/_profile.md` — your career narrative and deal-breakers

To re-run profile setup at any time: `node setup-profile.mjs`

---

## Configure your search

Edit `config/search.yml`:

```yaml
searches:
  - name: "my search"
    search_terms:
      - "software engineer"
    sites: [linkedin, indeed, glassdoor]
    location: "Dallas, TX"
    results_wanted: 50
    hours_old: 168

filter:
  target_titles:
    - "software engineer"
    - "backend engineer"
  negative_titles:
    - "senior"
    - "staff"
  min_score: 5
```

---

## Run the pipeline

```powershell
# Windows — full pipeline + local LLM evaluation
.\run.ps1 --batch

# macOS / Linux
./run.sh --batch
```

This runs five stages in sequence:

| Stage | What it does |
|-------|-------------|
| Scrape | Hits job boards, writes `output/jobs.csv` |
| Filter | Scores by keyword + title match, writes `output/filtered_jobs.csv` |
| Screen | *(opt-in)* Drops expired postings and poor fits via local LLM |
| Bridge | Pushes new jobs into `career-ops/data/pipeline.md`, deduped |
| Batch prep | Writes `career-ops/batch/batch-input.tsv` and `batch/jds/{id}.txt` |

Then `--batch` evaluates the queue using OpenCode + your local Ollama model. Each job gets a full A–G career-ops evaluation (report + tracker line).

Results:
- Reports → `career-ops/reports/{num}-{company}-{date}-local.md`
- Tracker lines → `career-ops/batch/tracker-additions/{id}.tsv`
- Merge into your application tracker: run `/career-ops` in career-ops

---

## Evaluation options

```powershell
# Local model (free, no API cost) — default
.\run.ps1 --batch

# Claude via Claude Max subscription (full career-ops evaluation)
.\run.ps1 --deep-eval

# Both on the same run (local first, then Claude for anything it missed)
.\run.ps1 --batch
.\run.ps1 --deep-eval
```

### Changing the local model

Set `OLLAMA_MODEL` in `.env`:

```
OLLAMA_MODEL=qwen2.5:32b
```

Then pull it: `ollama pull qwen2.5:32b`

For best quality on 8 GB VRAM + 16 GB RAM: `qwen2.5:32b` (~20 GB, partially CPU-offloaded). For fully GPU-resident: `gemma3:12b` (~7.3 GB).

---

## Optional: pre-screening

Add a `screen:` block to `config/search.yml` to filter out dead postings and poor fits *before* they reach evaluation:

```yaml
screen:
  liveness: true            # HTTP check — drops 404/410 and "position filled" pages
  liveness_timeout: 8       # seconds per request

  ollama_fit: true          # semantic fit check via local LLM
  ollama_model: qwen2.5:32b
  ollama_threshold: 3.5     # drop jobs scoring below this (1–5 scale)
  ollama_url: http://localhost:11434
  profile: >                # brief candidate summary for the scorer
    Mid-level software engineer, TypeScript, React, Node.js, AWS.
```

Both checks are off by default. When disabled the stage adds zero latency.

---

## Skip flags

```powershell
.\run.ps1 --skip-scrape           # reuse existing output/jobs.csv
.\run.ps1 --skip-filter           # reuse existing output/filtered_jobs.csv
.\run.ps1 --skip-screen           # skip liveness + fit scoring
.\run.ps1 --skip-bridge           # don't push to career-ops
.\run.ps1 --skip-batch-prep       # don't update the evaluation queue

# Re-run evaluation only (skip the full pipeline)
.\run.ps1 --skip-scrape --skip-filter --skip-screen --skip-bridge --skip-batch-prep --batch
```

---

## Key files

```
job-search-pipeline/
├── config/search.yml            # Search terms, filter rules, screen settings
├── .env                         # Paths and model overrides
├── output/
│   ├── jobs.csv                 # Raw scrape output
│   └── filtered_jobs.csv        # After filter (and screen if enabled)
└── career-ops/
    ├── config/profile.yml       # Your candidate profile
    ├── cv.md                    # Your CV
    ├── modes/_profile.md        # Career narrative and deal-breakers
    ├── data/
    │   ├── pipeline.md          # Jobs queued for evaluation
    │   ├── scan-history.tsv     # Dedup history
    │   └── applications.md      # Your application tracker
    ├── batch/
    │   ├── batch-input.tsv      # Evaluation queue
    │   ├── batch-state.tsv      # Evaluation progress (resumable)
    │   ├── jds/                 # Cached job descriptions
    │   ├── logs/                # Per-job worker logs
    │   └── tracker-additions/   # Pending tracker lines
    └── reports/                 # Full A–G evaluation reports
```

---

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `CAREER_OPS_PATH` | `./career-ops` | Path to career-ops directory |
| `RESUME_PATH` | auto-detected | Path to your resume PDF |
| `SEARCH_CONFIG` | `config/search.yml` | Path to search config |
| `BATCH_CLI` | `opencode` | CLI used by `--batch` |
| `OLLAMA_MODEL` | `qwen2.5:32b` | Ollama model for `--batch` and screen |
