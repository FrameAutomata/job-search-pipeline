# job-search-pipeline

A local, fully automated pipeline that runs the complete job-search loop end to end:

1. **Scrape** — [JobSpy](https://github.com/speedyapply/JobSpy) pulls postings from Indeed, LinkedIn, Glassdoor, ZipRecruiter, etc. into `output/jobs.csv`.
2. **Filter** — keywords are extracted *from your resume* (YAKE statistical extraction — works for nursing, marketing, trades, finance, tech, any field). Each job is scored by keyword + target-title matches; negative titles hard-exclude. Output: `output/filtered_jobs.csv`.
3. **Screen** *(opt-in)* — HTTP liveness check drops expired postings; local Ollama fit scoring drops semantic mismatches before they reach evaluation.
4. **Bridge** — surviving postings are appended to [career-ops](https://github.com/santifer/career-ops)'s `data/pipeline.md` queue, deduped against its history.
5. **Batch prep** — writes the evaluation queue (`batch/batch-input.tsv`) and caches job descriptions (`batch/jds/{id}.txt`) for the evaluator.

Evaluation is handled by career-ops using the agent CLI of your choice. Run `--batch` to evaluate automatically using a local model via OpenCode + Ollama (no API cost), or `--deep-eval` to use Claude via your Claude Max subscription.

## Quickstart

```powershell
# Windows
git clone <this repo>
cd job-search-pipeline
.\setup.ps1           # creates venv, installs deps, clones career-ops, runs profile setup
# Edit config\search.yml, then:
.\run.ps1 --batch     # scrape → filter → bridge → evaluate with local LLM
```

```bash
# macOS / Linux
git clone <this repo>
cd job-search-pipeline
./setup.sh
# Edit config/search.yml, then:
./run.sh --batch      # scrape → filter → bridge → evaluate with local LLM
```

See [QUICKSTART.md](QUICKSTART.md) for a detailed walkthrough.

## Configuration

**`config/search.yml`** — search terms, sites, location, filter rules, and optional screen settings.

**`.env`** — paths and overrides:

| Variable | Default | Purpose |
|----------|---------|---------|
| `CAREER_OPS_PATH` | `./career-ops` | Path to career-ops directory |
| `RESUME_PATH` | auto-detected | Path to your resume PDF |
| `SEARCH_CONFIG` | `config/search.yml` | Path to search config |
| `BATCH_CLI` | `opencode` | CLI used by `--batch` |
| `OLLAMA_MODEL` | `qwen2.5:32b` | Ollama model used by `--batch` |

## Screening (opt-in)

Add a `screen:` block to `config/search.yml` to filter out dead postings and poor fits before they reach evaluation:

```yaml
screen:
  liveness: true           # drop 404/410 and "position filled" pages
  liveness_timeout: 8      # seconds per request

  ollama_fit: true         # drop semantic mismatches via local LLM
  ollama_model: qwen2.5:32b
  ollama_threshold: 3.5    # drop jobs scoring below this (1–5 scale)
  ollama_url: http://localhost:11434
  profile: >               # brief candidate summary for the scorer
    Mid-level software engineer, TypeScript, React, Node.js, AWS.
```

Both checks are disabled by default. When all are off, the screen stage adds zero latency.

## Evaluation flags

```powershell
.\run.ps1 --batch       # evaluate with local LLM (OpenCode + Ollama, no API cost)
.\run.ps1 --deep-eval   # evaluate with Claude via batch-runner.sh (Claude Max)
```

`--batch` invokes `career-ops/batch/batch-runner.sh` using the CLI set by `BATCH_CLI` (default: `claude`). Set `OLLAMA_MODEL` to pass a model override. Supported CLIs: `claude`, `opencode`, `gemini`, `qwen`. State is tracked in `career-ops/batch/batch-state.tsv`, so runs are safe to interrupt and resume.

## Skipping steps

```bash
./run.sh --skip-scrape         # reuse output/jobs.csv
./run.sh --skip-filter         # reuse output/filtered_jobs.csv
./run.sh --skip-screen         # skip liveness + fit scoring
./run.sh --skip-bridge         # don't push to career-ops
./run.sh --skip-batch-prep     # don't update the evaluation queue

# Re-run only evaluation on an existing queue
./run.sh --skip-scrape --skip-filter --skip-screen --skip-bridge --skip-batch-prep --batch
```

## Requirements

- Python 3.12 (jobspy pins `numpy==1.26.3`, which has no Python 3.13 wheel; setup scripts auto-select 3.12 via `py -3.12` / `python3.12`)
- Node.js 18+ (career-ops and profile setup)
- The agent CLI of your choice for `--batch` / `--deep-eval` — whichever you have installed:
  - `claude` (default): [Claude Code](https://claude.ai/code)
  - `opencode`: [OpenCode](https://opencode.ai)
  - `gemini`: [Gemini CLI](https://github.com/google-gemini/gemini-cli)
  - `qwen`: Qwen CLI

## License

GPL-3.0-or-later. See [LICENSE](LICENSE) for the full text.

Copyright (C) 2026 FrameAutomata

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version. This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.

Note: this repository contains only the orchestrator. [JobSpy](https://github.com/speedyapply/JobSpy) (MIT) and [career-ops](https://github.com/santifer/career-ops) (MIT) are fetched at setup time as separate works under their own licenses.
