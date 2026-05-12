# job-search-pipeline

A local, GitHub-runnable pipeline that automates the job-search loop end to end:

1. **Scrape** — [JobSpy](https://github.com/speedyapply/JobSpy) pulls postings from Indeed, LinkedIn, Glassdoor, ZipRecruiter, etc. into `output/jobs.csv`.
2. **Filter** — keywords are extracted *from your resume* (YAKE statistical extraction — works for nursing, marketing, trades, finance, tech, any field). Each job is scored by keyword + target-title matches; negative titles hard-exclude. Output: `output/filtered_jobs.csv`.
3. **Bridge** — surviving postings are appended to [career-ops](https://github.com/santifer/career-ops)'s `data/pipeline.md` queue (deduped against its history) for AI-powered fit evaluation.

The actual AI fit-grading happens inside career-ops via your AI coding CLI of choice (Claude Code, Gemini CLI, OpenCode, etc.).

## Quickstart

```powershell
# Windows
git clone <this repo>
cd job-search-pipeline
.\setup.ps1                  # creates venv, installs JobSpy, clones career-ops, sets up configs
# Edit .\config\search.yml and .\.env, then drop your resume in .\resumes\
.\run.ps1
```

```bash
# macOS / Linux
git clone <this repo>
cd job-search-pipeline
./setup.sh
# Edit ./config/search.yml and ./.env, then drop your resume in ./resumes/
./run.sh
```

After `run`, change directory into `./career-ops` and run `/career-ops pipeline` in your AI CLI to evaluate the queue.

## Configuration

- **`.env`** — paths (`CAREER_OPS_PATH`, `RESUME_PATH`, `SEARCH_CONFIG`).
- **`config/search.yml`** — JobSpy search terms / sites / location, your `target_titles` and `negative_titles`, minimum score threshold. Optional `keyword_overrides` for terms YAKE misses.

The filter is **domain-agnostic**: drop your resume PDF, list a few `target_titles` you're pursuing (e.g. `"registered nurse"`, `"marketing coordinator"`, `"line cook"`, `"backend engineer"`), and the scorer derives the rest from your resume. No keyword tuning needed for the common case.

## Skipping steps

```bash
./run.sh --skip-scrape    # reuse output/jobs.csv
./run.sh --skip-filter    # reuse output/filtered_jobs.csv
./run.sh --skip-bridge    # don't touch career-ops
```

## Requirements

- Python 3.12 (jobspy pins `numpy==1.26.3`, which has no Python 3.13 wheel; setup scripts auto-select 3.12 via `py -3.12` / `python3.12`)
- Node.js 18+ (career-ops)
- An AI CLI for career-ops evaluation (Claude Code, Gemini CLI, OpenCode, etc.)

## License

GPL-3.0-or-later. See [LICENSE](LICENSE) for the full text.

Copyright (C) 2026 FrameAutomata

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version. This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.

Note: this repository contains only the orchestrator. [JobSpy](https://github.com/speedyapply/JobSpy) (MIT) and [career-ops](https://github.com/santifer/career-ops) (MIT) are fetched at setup time as separate works under their own licenses.
