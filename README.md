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

> **One copy per GitHub account.** The Actions Free plan gives **2,000 Linux minutes a month per account** for private repositories — per *account*, not per repository. A daily run of this pipeline is long enough (see [Running it for free](#running-it-for-free)) that two copies under the same account will run out of minutes before the month does, and GitHub then stops creating workflow runs at all. If two people want their own searches, put the second copy under a second account. The digest that arrives after each run tells you what the month is costing at the current pace.

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
3. Walk through the wizard (resume → about → roles/comp → search settings → optional narrative → provider + API key + [daily digest](#the-daily-digest) → local settings, including your [agent CLI](#choosing-an-agent-cli) and [submit policy](#applying-to-jobs) → review). On submit it generates your profile and writes every required secret to *this* repo. It refuses to write to a public repo.

See [Guided onboarding](#guided-onboarding-onboard) below and the [QUICKSTART setup steps](QUICKSTART.md#step-2--create-your-profile) for the full walkthrough.

#### Option B — set the secrets by hand

First generate the profile artifacts locally with `node setup-profile.mjs` (point it at a DOCX, ODT, or PDF resume — DOCX/ODT recommended, since resume tailoring slot-edits an editable copy per job for the browser-agent work-order). That writes `cv.md`, `profile.yml`, `_profile.md`, and `search.yml`, but **not** `resumes/resume.txt` — create that yourself from your resume's plain text (e.g. `python -m pipeline.resume_text resumes/resume.docx > resumes/resume.txt`, or copy-paste the text into the file). Then in your private copy on github.com go to **Settings → Secrets and variables → Actions → New repository secret** and add one base64-encoded secret per file:

   | Secret | File to encode |
   |--------|----------------|
   | `CV_MD_B64` | `career-ops/cv.md` |
   | `PROFILE_YML_B64` | `career-ops/config/profile.yml` |
   | `SEARCH_CONFIG_B64` | `config/search.yml` |
   | `RESUME_TXT_B64` | `resumes/resume.txt` (the file you created above) |
   | `PROFILE_MD_B64` *(optional)* | `career-ops/modes/_profile.md` |
   | `ARTICLE_DIGEST_B64` *(optional)* | `career-ops/article-digest.md` |
   | `PROFILE_MASTER_B64` *(optional)* | your living `PROFILE.md` → decoded to `career-ops/PROFILE.md` |

   Plus at least one LLM API key secret: `GEMINI_API_KEY` (free tier) / `GROQ_API_KEY` / `DEEPINFRA_API_KEY` / `OPENROUTER_API_KEY` / `DEEPSEEK_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`.

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
2. **Actions → Daily Job Pipeline → Run workflow** to do a test run before the scheduled cron fires. It runs every configured search pass (including any easy-apply pass) in one go.
3. **Read results**: Actions tab → open the run → download the `pipeline-output-*` artifact, or use the local UI's **↻ Refresh** button to pull it without leaving the browser.
   The artifact holds **that run's** new reports plus the current tracker — not the whole report history, which would grow every artifact without bound and eventually exhaust your account's Actions storage quota (an exhausted quota stops GitHub creating workflow runs at all). Refresh merges each run's reports into your local `career-ops/`, so the accumulated history builds up there; the cloud's own copy lives in the Actions cache.

### Pulling updates from the template later

Your private copy starts as a snapshot — it doesn't auto-track changes to this template. To pull updates:

```bash
git clone <your-private-copy-url>
cd <your-private-copy>
git remote add template <this-template-url>
git fetch template
git merge --allow-unrelated-histories template/main   # first update only; harmless after
git push
```

Conflicts only happen if you've edited the same files locally — your data lives in cache/secrets/artifacts, not in tracked files.

### For maintainers of this template

If you're maintaining this template (rather than using it for a job search), no action needed — the cron triggers fire here too, but the `preflight` job detects `is_template: true` via the GitHub API and exits cleanly with `should_run=false`. The Actions tab stays clean.

A local, fully automated pipeline that runs the complete job-search loop end to end:

1. **Scrape** — [JobSpy](https://github.com/speedyapply/JobSpy) pulls postings from Indeed and LinkedIn into `output/jobs.csv`. These are the only two supported boards: Glassdoor and ZipRecruiter sit behind a Cloudflare wall that 403s every scripted request (zero rows contributed), and Google Jobs drops connections mid-response in a way that crashes JobSpy's scraper. Unsupported sites in a config are stripped at load time with a warning.
2. **Filter** — keywords are extracted *from your resume* (YAKE statistical extraction — works for nursing, marketing, trades, finance, tech, any field). Each job is scored by keyword + target-title matches; negative titles hard-exclude. Output: `output/filtered_jobs.csv`.
3. **Screen** *(opt-in)* — for jobs that survived the filter, runs an HTTP liveness check (drops expired/filled postings), backfills each LinkedIn description via LinkedIn's public guest job-posting endpoint (reliable full JD, so `linkedin_fetch_description: false` is safe at scrape time), and dedupes against `scan-history.tsv` *before* the fetch so previously-seen URLs cost nothing.
4. **Bridge** — surviving postings are appended to [career-ops](https://github.com/santifer/career-ops)'s `data/pipeline.md` queue. Second dedup pass against scan-history, pipeline.md, and `company::role` pairs in applications.md.
5. **Batch prep** — writes the evaluation queue (`batch/batch-input.tsv`) and caches job descriptions (`batch/jds/{id}.txt`) for the evaluator.

Evaluation has two paths, pick whichever fits:
- `--batch` — interactive agent CLI (whichever you picked — see [Choosing an agent CLI](#choosing-an-agent-cli); free options first). Generates PDFs, can WebSearch in real time.
- `--evaluate-batch` — synchronous parallel API calls (auto-detects Gemini / Groq / DeepInfra / OpenRouter / DeepSeek / OpenAI / Anthropic / Ollama). Immediate results. Used by the cloud workflows.

## Quickstart

```powershell
# Windows
git clone <this repo>
cd job-search-pipeline
.\setup.ps1                  # creates venv, installs deps (incl. UI), clones career-ops, copies example configs
node setup-profile.mjs       # generate your profile from your resume (or use the UI wizard — see below)
# setup-profile.mjs writes config\search.yml; tweak it if you like, then pick an evaluation mode:
.\run.ps1 --batch            # interactive CLI agent (BATCH_CLI; free OpenCode by default)
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
| `RESUME_PATH` | auto-detected | Path to your resume (DOCX / ODT / PDF; DOCX/ODT recommended). Unset → auto-discovers `resumes/resume.{pdf,docx,odt}` |
| `SEARCH_CONFIG` | `config/search.yml` | Path to search config |
| `BATCH_CLI` | `opencode` | The agent CLI that applies for you and runs `--batch`. See [Choosing an agent CLI](#choosing-an-agent-cli); `python -m pipeline.agent_cli --list` prints the ids, tiers and what's installed. |
| `AGENT_MODEL` | per-CLI | Model the agent CLI starts with, on both paths (the hand-off command and `--batch`). Unset, each CLI uses its own default — except `gemini`, which gets a model with a workable free-tier quota. |
| `HANDOFF_SUBMIT_POLICY` | `stop-before-submit` | What the browser agent does at the Submit button. See [Applying to jobs](#applying-to-jobs). |
| `BATCH_PROVIDER` | auto-detect | LLM provider for `--evaluate-batch` (overrides auto-detection) |
| `BATCH_MODEL` | per-provider default | Model override for `--evaluate-batch` |
| `OLLAMA_MODEL` | unset | Older, `--batch`-only name for the agent CLI's model (e.g. `llama3.1:70b` against a local Ollama server). When set it wins over `AGENT_MODEL` on that path; unset, `--batch` takes `AGENT_MODEL`, else whatever the registry or the CLI itself would start on. |
| `GEMINI_API_KEY` / `GROQ_API_KEY` / `DEEPINFRA_API_KEY` / `OPENROUTER_API_KEY` / `DEEPSEEK_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | — | LLM provider keys. Auto-detect order: Gemini → Groq → DeepInfra → OpenRouter → DeepSeek → OpenAI → Anthropic. See [QUICKSTART](QUICKSTART.md#which-provider-should-i-pick) for picking one. |
| `OPENAI_BASE_URL` | OpenAI's default | Escape hatch — point the `openai` provider at any OpenAI-compatible endpoint. |
| `SKILL_PATH_DEFAULT` | `ask` | Default path for career-ops skills run from the UI: `ask` (choose each time), `api`, or `cli`. |
| `UI_LAN` / `UI_PASSWORD` / `UI_ALLOWED_HOSTS` | off | Serve the triage UI to your home network. See [Triaging from your phone](#triaging-from-your-phone-lan-mode). |
| `DIGEST_*` | — | Where the daily digest is delivered and how much of it. See [The daily digest](#the-daily-digest). In the cloud these are repository secrets and variables, not `.env`. |

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
- **Description backfill**: extracts the JD from the same fetched page (LinkedIn / Indeed selectors + `<body>` fallback). Lets you set `linkedin_fetch_description: false` and skip thousands of sequential per-job fetches during scrape.
- **Dead URL recording**: URLs that fail liveness get written to `scan-history.tsv` with status `screened-dead` so future runs skip them at the pre-screen step too. `scan-history.tsv` has three statuses in all — `added`, `screened-dead`, and `screened-offsite` for a row a remote search pass returned whose description is plainly on-site somewhere else. The first two are permanent; `screened-offsite` expires after 60 days, so widening your locations later can bring those roles back.

When `liveness: false` the screen stage is a no-op (no dedup, no fetches, no backfill — bridge still does its own dedup pass).

## Evaluation flags

```powershell
.\run.ps1 --batch                              # interactive: agent CLI per BATCH_CLI (default: opencode)
.\run.ps1 --evaluate-batch                     # sync API: auto-detected provider, ~3 parallel workers
.\run.ps1 --evaluate-batch --batch-provider gemini --batch-concurrency 5
```

`--batch` is the interactive CLI agent path; `--evaluate-batch` is the synchronous API path. They aren't mutually exclusive — `--batch` runs after the pipeline.

Three flags (mutually exclusive) select a subset of the `searches:` in your config:

- `--only-pass "name1,name2"` — explicit case-insensitive match on the `name:` field. Errors loudly on no match (typo protection).
- `--easy-apply-only` — only passes with `easy_apply: true`. No-ops cleanly if none configured.
- `--no-easy-apply` — only passes without `easy_apply: true`.

These flags are for ad-hoc local runs; the daily cloud workflow runs **every** pass once a day with no selection flag. Pass `name:` values can be anything you like — when you do use selection, it routes by the `easy_apply` JobSpy field, not the name.

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

> **`--skip-scrape` can hand you an empty file.** A scrape that returns zero rows
> truncates `output/jobs.csv` rather than leaving the previous run's rows behind to
> be re-processed as today's results. JobSpy swallows rate-limit responses
> (403/429/999) and returns an empty result instead of raising, so a throttled
> overnight run produces the same 0-byte `jobs.csv` as a genuine zero-result day,
> and the run exits 0 either way. If a re-drive comes back with nothing, check the
> size of `output/jobs.csv` before adjusting your filters — and to tell the two
> apart, scroll up in the same run log: JobSpy logs the refusal itself, e.g.
> `429 Response - Blocked by LinkedIn for too many requests` or Indeed's
> `responded with status code: 403`. Filters are worth touching only if neither
> appears. The same caveat applies to `--skip-filter` and `output/filtered_jobs.csv`,
> which is truncated whenever nothing survives the filter.

## Applying to jobs

The pipeline finds and evaluates roles. **It never submits an application.** When
you're ready to apply, it builds a *work-order* — one file per job board, holding
that board's best-scoring roles with the posting link, the score and (with
`--handoff-tailor`) a tailored résumé — and hands it to a browser agent that
works through them in *your* logged-in browser:

```bash
./run.sh --skip-scrape --skip-filter --handoff        # or the UI's 🤝 Hand off button
```

The files land in `output/handoff/` (`HANDOFF_OUT_DIR`): `next-roles-linkedin.jsonl`,
`next-roles-indeed.jsonl`, one session per board, plus a `HANDOFF-README.md` the
agent follows and a `PROFILE.md` it keeps growing as it learns your answers.

### What happens at the Submit button — your call

By default **the agent does not submit anything.** It opens the posting, fills the
form, checks it against your profile, and then stops and marks the row
`ready-to-submit`. You open that row, read what it wrote, click Submit yourself,
and set the row to `applied`. Until you do, that role comes back at the **top** of
its board's work-order every time — under a heading that says *"Waiting for you:
form filled — open it, check it, click Submit"* — so a half-finished application
can't quietly disappear.

Set `HANDOFF_SUBMIT_POLICY` in `.env` (or on the wizard's Local settings step) to
change that:

| Policy | What the agent does |
|---|---|
| `stop-before-submit` *(default)* | Fills and reviews every application, stops before Submit, records `ready-to-submit`. You click Submit and record `applied`. |
| `submit-easy-apply` | Submits the one-click board applications (Indeed Apply, LinkedIn Easy Apply) on its own; everything else stops before Submit as `ready-to-submit`. |
| `submit-all` | Submits everything it prepares. |

The other statuses an agent writes back are `claimed` (in progress), `handoff`
(blocked on something only you can do — a login, a CAPTCHA, a verification code)
and `skip:<reason>`. Anything recorded is remembered across every board, so a role
you applied to on LinkedIn never resurfaces on Indeed — `ready-to-submit` being
the single deliberate exception.

### Choosing an agent CLI

`BATCH_CLI` picks the agent CLI used for applying and for `--batch` evaluation.
Free options come first, and the default needs no API key at all:

| CLI | Tier | What that means | Install |
|---|---|---|---|
| `opencode` — OpenCode **(default)** | free | Its Zen gateway's rotating free hosted models need no key at all; or bring a free AI Studio Gemini key (`gemini-3.1-flash-lite`, ~500 requests/day ≈ 6–12 prepared applications). A paid Anthropic or OpenAI key plugs into the same CLI, so this is also the upgrade path. `--prompt` pre-fills the prompt and you press Enter. | `curl -fsSL https://opencode.ai/install \| bash` (or `npm install -g opencode-ai`) |
| `agy` — Antigravity CLI | free | Google's successor to Gemini CLI for individuals. The free Individual tier has a **weekly** agent quota (small — reports of ~20 requests/day and multi-day cooldowns), so expect a few prepared applications a week, not a day. Google may use your prompts — which include your profile — to improve its products unless you opt out in Antigravity's privacy settings. | `curl -fsSL https://antigravity.google/cli/install.sh \| bash` (Windows: `irm https://antigravity.google/cli/install.ps1 \| iex`) |
| `gemini` — Gemini CLI | free | Google stopped serving **personal logins** on 2026-06-18; the CLI still runs on a `GEMINI_API_KEY` from AI Studio at the API's free-tier limits. The launcher starts it on `gemini-3.1-flash-lite` (~500 requests/day) because the CLI's own default Flash model gets ~20 a day on a free key. Free-tier prompts may be used to improve Google's products. | `npm install -g @google/gemini-cli`, then put a key from [aistudio.google.com](https://aistudio.google.com) in `.env` |
| `claude` — Claude Code | paid | A Claude Pro subscription or API credits; no free tier. The strongest at driving a browser through a long application form. | `npm install -g @anthropic-ai/claude-code` |
| `qwen` — Qwen Code | paid | The free login ended 2026-04-15, so it needs a paid API key now. | `npm install -g @qwen-code/qwen-code` |

`python -m pipeline.agent_cli --list` prints this table with what's installed
marked; `--check` tells you whether the one you configured is on your PATH and
how to install it if not. Set `AGENT_MODEL` to start any of them on a specific
model. Before the agent can drive a browser it needs the Playwright MCP server
registered — one command, whichever CLI you use:

```bash
python -m pipeline.agent_cli --register-mcp
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

Point it at either your local `career-ops/` directory (if you run the pipeline locally) or a GitHub Actions artifact you've downloaded and extracted (the artifact has the same `reports/` + `data/applications.md` layout, but carries only that run's reports — the tracker in it is complete).

**Cloud buttons** (require the [`gh` CLI](https://cli.github.com) installed + `gh auth login`):
- **↻ Refresh** — downloads the most recent *successful* `daily-pipeline` artifact via `gh run download` and **merges it into your local tracker** (offline-first): cloud wins for shared roles, and rows from a local `Run local` are preserved. Each artifact carries that run's new reports, so local `career-ops/` accumulates the history as you refresh. On a fresh machine, or after a gap longer than the 7-day artifact retention, the tracker rows still arrive in full but the report files behind them don't — run the **Export Reports** workflow once (Actions tab) to download the whole history from the cache and extract it over your local `career-ops/`. The merged tracker lives durably in `career-ops/`, so it survives restarts and is still there when you're offline or out of CI/CD credits. If GitHub is unreachable, your last-synced local data is left untouched.
- **▶ Run now** — triggers a `daily-pipeline` run in the cloud (`gh workflow run`). It executes on GitHub; click Refresh once it finishes.
- **⇧ Push N changes** — appears once you've made status edits on the board. Pushes them to the cloud tracker via the `edit-tracker` workflow. It first refreshes the latest tracker and applies your changes on top, so roles the pipeline added since your last refresh aren't clobbered. `edit-tracker` shares a concurrency group with the daily pipeline, so a Push during a run **queues** behind it instead of racing it — but GitHub keeps only one queued run per group, so pressing Push twice while a daily is still going cancels the first one. If a queued **Edit Tracker** run shows up cancelled in the Actions tab, re-drag those cards and Push again.
- **⚙ Setup** — opens the guided onboarding wizard (see below).
- **⬆ Update available** — appears only when the maintainer's template has changes your copy doesn't have yet. Clicking merges the template's latest `main` into your local clone and pushes to your copy — updating both the local UI and the cloud copy in one step (restart `run-ui` afterward to load the new code). On a merge conflict it leaves your tree clean and tells you to resolve it manually. Cloud-only users can instead run the **Update from Template** workflow from the Actions tab.

`gh` targets the repo of the directory you launch from; set `JOB_SEARCH_REPO=owner/name` to override.

### Triaging from your phone (LAN mode)

The UI binds `127.0.0.1` and refuses cross-origin POSTs, so only this machine can
reach it and it needs no password. To read the board from a phone on the same
network:

```bash
UI_PASSWORD='a long passphrase' ./run-ui.sh --lan     # Windows: .\run-ui.ps1 -Lan
```

- **`UI_PASSWORD` is required.** LAN mode puts the UI on every interface of this
  machine, so both the launcher and the server refuse to start without one (each
  checks it, because the launcher can't read `.env` and the server can). Any
  username works; the password is what's checked, on every request.
- Basic auth over plain HTTP is for a network you trust. **Stop the server before
  joining another one.**
- From the LAN the UI can move a card between columns and push that to the cloud —
  and nothing else. Reset, Update, running the pipeline, the Setup wizard,
  launching an agent and building a hand-off stay loopback-only: they run programs
  and rewrite config, which isn't what a phone is for.
- `UI_ALLOWED_HOSTS` is the comma-separated list of names/addresses a browser may
  use in the URL bar. Left unset the server works them out at startup and prints
  what it accepted; set it when that guess is wrong.

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
- **CLI** — hands you a ready-to-run command for your agent (`BATCH_CLI`, default `opencode` — see [Choosing an agent CLI](#choosing-an-agent-cli)). Interactive, can pull the live JD, search the web, and drive a browser. The result panel shows the command with **Copy command** and **▶ Run in terminal**, which opens a new console window with the command pre-loaded (Windows → `cmd`, macOS → `Terminal.app`, Linux → the first emulator on PATH from a known list, or whatever `$TERMINAL` names). The agent always runs in *your* terminal, where its tools and cost are visible — we never run it inside the UI process. Every skill supports this path.

The UI shows whichever paths each skill can use (an agent CLI on your PATH, an API key, or both). Set **`SKILL_PATH_DEFAULT=api|cli`** in `.env` to skip the chooser; leave it `ask` (default) to pick each time — handy if you'd rather spend an agent-CLI membership than API credits, or use the API for batch scoring and the CLI for tailoring.

**Which should I use?**
- *API* — fast, no install, good for tailoring résumés at volume; pick this if you don't want to install an agent. (Résumé-markdown only.)
- *CLI* — interactive, with live web research and a real browser. Required for **PDF**, **interview-prep**, and the live **apply** assistant (they need tools the API path can't provide), and best when you want to iterate or pull the freshest JD.
- *Both* — API for quick tailoring, CLI for depth and the browser-driven skills. Recommended for an active search.

**One-time setup for the browser-driven skills** (Apply assistant + PDF résumé). `setup.ps1` / `setup.sh` does both of these for you now; the steps below are the manual fallback if setup couldn't (no agent CLI on PATH yet, install failure, etc.) — the UI also surfaces them inline when you run the affected skill, and the Setup wizard has a **Register browser bridge** button beside the CLI picker.

- **Apply assistant** drives a live browser; your agent needs the **Playwright MCP server** registered. Each CLI registers it differently — some take a command, some want an entry merged into a config file — so one command covers whichever you use:
  ```bash
  python -m pipeline.agent_cli --register-mcp                    # the CLI you configured
  python -m pipeline.agent_cli --register-mcp opencode           # a specific one
  python -m pipeline.agent_cli --register-mcp-all-installed      # what setup runs
  ```
  It is safe to run before the CLI is installed — you get the install line instead of an error — and safe to run twice. Without it the agent says it "doesn't have Playwright in this session" and falls back to asking you for a screenshot.
- **PDF résumé** (and the Apply assistant the first time it drives the browser) need **Chromium installed locally**, from inside the career-ops clone:
  ```bash
  cd career-ops
  npx playwright install chromium
  ```
  (~150 MB. Idempotent — re-running setup is cheap.)

> Security: the UI is localhost-only and now refuses cross-origin state-changing requests, so a web page you have open can't trigger skill runs, cloud actions, or secret writes behind your back.

### Guided onboarding (`/onboard`)

Instead of running the CLI setup + `base64` + `gh secret set` by hand, the
**⚙ Setup** wizard does it from the browser. Walk through a short form — upload
your resume (DOCX, ODT, or PDF — DOCX/ODT recommended), enter target roles /
compensation / locations / boards, pick an LLM provider and paste its API key,
say where the [daily digest](#the-daily-digest) should be delivered, and pick
your [agent CLI](#choosing-an-agent-cli) and [submit policy](#applying-to-jobs) —
and on submit the server:

1. extracts your resume text (`pipeline/resume_text.py` dispatches by format:
   pdfplumber for PDF, python-docx for DOCX, odfpy for ODT),
2. generates `profile.yml`, `cv.md`, `_profile.md`, and `search.yml` via
   `setup-profile.mjs --from-json` (same generators as the CLI — one source of truth),
3. base64-encodes them and writes all required **GitHub secrets** (`gh secret set`,
   value piped via stdin so keys never hit argv/logs), plus your provider key, the
   digest's delivery secrets, and the repository variables the daily reads —
   `BATCH_PROVIDER` / `BATCH_MODEL`, the digest's thresholds, your Gemini free-tier
   limits, and whether to merge template updates weekly.

Re-opening it later prefills from the files a run actually uses, so it doubles as
an editor: change one answer and Save. A blank secret field means "keep the one I
already saved", never "unset it".

It **refuses to write to a public repo** (same privacy guard as the workflows),
and the status line up top shows the target repo, its visibility, and whether
it's already configured. After onboarding, click **▶ Run now** to kick off your
first cloud run.

## The daily digest

Nobody wants to open the Actions tab every morning to find out whether anything
happened. After every cloud run — success or failure — the pipeline sends one
message naming the roles worth your attention: score, the evaluator's verdict
(`Apply` / `Consider` / `Research first`), the one line of reasoning, a link
straight to the posting, and the full reports attached as a single file. If the
run failed you get one line saying so, with a link to the run. If nothing scored
high enough you get a one-line heartbeat, so silence always means something is
broken rather than "a quiet day".

**Discord, in about two minutes** (no account beyond the server you already have):

1. In your Discord server: **Server Settings → Integrations → Webhooks → New
   Webhook**.
2. Pick the channel it should post to, then **Copy Webhook URL**.
3. Paste it into the Setup wizard's **Daily digest** block (Provider step), or add
   it by hand as the repository secret `DIGEST_DISCORD_WEBHOOK` under **Settings →
   Secrets and variables → Actions**.

**Email instead, or as well:** set `DIGEST_EMAIL_TO` plus `DIGEST_SMTP_HOST`,
`DIGEST_SMTP_PORT` (587, STARTTLS), `DIGEST_SMTP_USER` and `DIGEST_SMTP_PASS`.
Gmail needs an [App Password](https://support.google.com/accounts/answer/185833)
with 2-step verification on — your normal password will not work. `From` defaults
to the SMTP user.

Either channel alone is enough, and an unset one is skipped. Tuning lives in
repository **variables** (same page, Variables tab) and each has a working
default: `DIGEST_MIN_SCORE` (4.0), `DIGEST_LIMIT` (10 roles), `DIGEST_ALWAYS`
(send the heartbeat), `DIGEST_ATTACH_REPORTS`, `DIGEST_NEXT_STEP` (replace the
closing "open the UI, Refresh, Hand off" line). The digest can never fail a run —
it exits 0 whatever happens.

To see what it would send, without sending anything: the digest reports what is
*new since a manifest*, so take the snapshot before the run and build the digest
after it, and blank the two channel variables for that one command — which
guarantees nothing leaves the machine, whether or not roles qualify.

```bash
# before the run — record which report files already exist
python -m pipeline.run_artifact snapshot --root career-ops \
  --manifest /tmp/manifest.json --delta reports
# after it — write the digest to a file instead of delivering it
DIGEST_DISCORD_WEBHOOK= DIGEST_EMAIL_TO= \
  python -m pipeline.daily_digest --root career-ops \
  --manifest /tmp/manifest.json --dump /tmp/digest.json
```

`--manifest` is required (it is what "new this run" is measured against), and
`__main__` loads `.env`, so a webhook configured there *is* used unless you blank
it as above.

## Running it for free

The default configuration costs nothing: GitHub Actions' free minutes, Gemini's
free API tier for evaluation, a Discord webhook for delivery, and a free agent CLI
for applying. Each of those has a ceiling, and the ones that bite are these:

- **Evaluation capacity** isn't the requests-per-day number. On the recommended
  free-tier model the tokens-per-minute budget binds first — an evaluation is a
  whole job description plus your profile — which works out to roughly **2,400
  evaluations a day**, ten times what a daily scrape produces. That is not the
  constraint.
- **Actions minutes are.** The Free plan gives **2,000 Linux minutes a month per
  account**, and staying inside Gemini's free-tier token budget means pacing:
  160–190 evaluations take **95–115 minutes** of runner time, so a daily at that
  length is around 3,000 minutes a month and doesn't fit. Two copies under one
  account certainly don't.
- **The fix is your own numbers.** The built-in rate-limit table is a hand-copied
  snapshot of the free tier and is only the fallback. Put your project's real
  limits from [aistudio.google.com/rate-limit](https://aistudio.google.com/rate-limit)
  into the Setup wizard's RPM/TPM/RPD boxes — they're usually more generous than
  the snapshot, and a looser token budget is a shorter run at the same quota.
  Failing that, shrink the run: fewer search passes, a smaller `results_wanted`, a
  higher `min_score`. `python -m pipeline.gemini_limits --show` prints what's in
  effect and which limit is binding.
- **The digest does the arithmetic for you**, quoting each run's minutes and the
  monthly projection at that pace, and warning inside the last 10%.

**What paying buys**, in the order it's worth buying: a paid evaluation key removes
the pacing, and with it most of the minutes problem; paid Actions minutes (or a
Team plan) remove the budget; a paid agent CLI is better at driving a browser
through a long application form. None of the three is needed for the loop to run
end to end.

## Requirements

- Python 3.12 (jobspy pins `numpy==1.26.3`, which has no Python 3.13 wheel; setup scripts auto-select 3.12 via `py -3.12` / `python3.12`)
- Node.js 20+ (career-ops and profile setup — career-ops pins `playwright@1.62.1`, which requires Node 20)
- An agent CLI, for applying and for `--batch` — see [Choosing an agent CLI](#choosing-an-agent-cli). The default is free and needs no API key.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE) for the full text.

Copyright (C) 2026 FrameAutomata

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version. This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.

Note: this repository contains only the orchestrator. [JobSpy](https://github.com/speedyapply/JobSpy) (MIT) and [career-ops](https://github.com/santifer/career-ops) (MIT) are fetched at setup time as separate works under their own licenses.
