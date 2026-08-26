# job-search-pipeline

Automated end-to-end job search orchestrator. Scrapes LinkedIn/Indeed via **JobSpy** (the only two supported boards — see the scrape-sites note under Configuration), scores results against a resume using YAKE keyword extraction, optionally screens for liveness, then bridges surviving jobs into **career-ops** for AI-powered evaluation. Supports interactive evaluation via any agent CLI (`--batch`) or synchronous parallel API evaluation across any LLM provider (`--evaluate-batch`).

**Applying is not a pipeline stage.** The pipeline finds and evaluates roles; the terminal `--handoff` stage — or the UI's **🤝 Hand off** button (batch) and per-role **Hand off** prompt in the report pane — emits an agent-agnostic **work-order per job site** (`next-roles-<site>.jsonl` + `.md` — one session per site the scraper searches from, written to `HANDOFF_OUT_DIR`, default `output/handoff/`) that the user hands to whatever browser agent they prefer (Claude Cowork, OpenClaw, a local Agent-SDK/`claude -p` runner, …). The agent works one site session at a time — logging into that site once, applying through the user's own logged-in browser — writes each outcome back into the session file's `status` column (`claimed` / `applied` / `handoff` / `skip:<reason>`), and the next `--handoff` run folds those into the **shared** status tracker (`role-status.jsonl`, board-insensitive keys) so a handled role never reappears on any site. Terminal outcomes are also reflected into career-ops' `applications.md` — the tracker the UI renders and the cloud maintains — (`applied`→**Applied**, `skip`→**SKIP**; `handoff`/`claimed`/`drafted` stay dedup-only, and a row already past **Evaluated** is never clobbered), which surfaces them in the UI Kanban and queues an **identity-anchored** pending override; the UI's **Push** then dispatches it to `edit-tracker.yml` so the cloud tracker matches. Handoff is a local-only stage (the daily cloud workflow never runs it), so applied status originates locally and flows *up* to the cloud, same as a manual Kanban edit.

---

## Pipeline stages

| Stage        | Script                      | Input → Output                                                            |
| ------------ | --------------------------- | ------------------------------------------------------------------------- |
| Scrape       | `pipeline/scrape.py`        | `config/search.yml` → `output/jobs.csv` (truncated when a run returns zero rows) |
| Filter       | `pipeline/filter.py`        | `output/jobs.csv` → `output/filtered_jobs.csv`                            |
| Screen       | `pipeline/screen.py`        | `filtered_jobs.csv` → filtered in-place + backfills missing descriptions  |
| Bridge       | `pipeline/bridge.py`        | `filtered_jobs.csv` → `career-ops/data/pipeline.md` + `scan-history.tsv`  |
| Batch prep   | `pipeline/batch_prep.py`    | bridge output → `career-ops/batch/batch-input.tsv` + `batch/jds/*.txt`    |
| Batch evaluate | `pipeline/batch_evaluate.py` | `batch-input.tsv` → parallel LLM evaluation → `reports/*.md` + `tracker-additions/*.tsv` |
| Handoff      | `pipeline/handoff.py`       | queue (scored-export jsonl if present, else `career-ops/data/applications.md`) + `role-status.jsonl` (+ optional prose JOB_LOG) → `output/handoff/next-roles-<site>.{jsonl,md}` (one session per site) for the user's browser agent |

`orchestrate.py` chains all stages. Each stage can be skipped independently via `--skip-<stage>`; `--handoff` opts into the terminal work-order build (`--handoff-board`, `--handoff-limit`, `--handoff-tailor`).

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

# Build the browser-agent work-orders — one session per job site
# (next-roles-<site>.jsonl). Queue = the scored-export jsonl when one exists,
# else career-ops/data/applications.md (so --recheck-liveness Discards keep dead
# roles out on the tracker path; the agent re-verifies postings live either
# way). --handoff-limit caps each site; --handoff-board narrows to one site;
# --handoff-tailor pre-tailors a resume per row.
./run.ps1 --skip-scrape --skip-filter --handoff
./run.ps1 --skip-scrape --skip-filter --handoff --handoff-board linkedin --handoff-limit 25 --handoff-tailor
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
| `pipeline/app/`                         | Local web UI — `server.py` (FastAPI routes + a localhost-only cross-origin guard; incl. the browser-agent handoff endpoints: `POST /api/handoff/build` + status polling for the 🤝 toolbar button, `GET /api/handoff/role-prompt/{num}` for the per-role paste-ready prompt; and the local-search-override endpoints `GET/POST/DELETE /api/local-search` behind the "Edit local search config" panel by the Run-local controls), `data.py` (parse applications.md + render reports), `skills.py` (career-ops skill launchpad: capability detection + a skill registry — résumé-markdown via API or CLI, and PDF / interview-prep / apply via CLI hand-off), `self_update.py` (pull the maintainer's template changes via `git fetch/merge/push`), `reset.py` (start-over: snapshot then wipe job-search state, keep setup; clear the cloud cache), `static/` (SPA). Deps in `requirements-ui.txt`. |
| `pipeline/rowio.py`                     | The one contract for "the previous **CSV** stage produced no rows": `read_rows(path)` (missing, zero-byte **and header-only** all read as `[]`) and `write_rows(path, rows, fieldnames=None)` (zero rows truncates the file to zero bytes). The rule is deliberately asymmetric — **producers write exactly zero bytes; readers accept anything with no data rows** — which is what lets it work against files it didn't write (a header-only CSV left by an older run, and scrape's happy path, where pandas owns the columns and writes via `to_csv`). Two neighbours are deliberately out of scope and say so in the module docstring: the `.tsv` queue/state files, whose `st_size == 0` answers "should I write a header" on a file that accumulates across runs, not "did upstream produce nothing"; and handoff's jsonl+md work-orders, where the markdown half renders an empty table on purpose because its reader is a human. Truncating is the load-bearing half — a stage that produced nothing today must not leave yesterday's output for the next stage to re-process as today's, and `--skip-scrape`/`--skip-filter` reuse whatever is on disk. Every "nothing survived" exit in scrape, filter and screen writes through it, and filter, screen and bridge read through it, so a producer and a consumer can't disagree about which shape means empty. Writes go via a temp file and `os.replace`, so a raise mid-write can't leave a header-plus-partial-rows third shape that reads as a complete short result. Reads decode `utf-8-sig`, so an Excel round-trip's BOM doesn't rename the first column out from under every stage. A dependency-free leaf (stdlib only) on the same terms as `sites.py`. |
| `pipeline/stdio.py`                     | The stage-log buffering rule, in one place: **every entry point calls `line_buffer_stdout()` first** — `orchestrate.main()` and each `__main__` block under `pipeline/` — and no `print` anywhere needs `flush=True`. Redirected to a file or pipe, Python block-buffers stdout at 8KB, so progress lines sit unseen and a run looks hung on exactly the slow steps someone is watching. This replaced three overlapping mechanisms (`PYTHONUNBUFFERED` at the two callers that redirect, a reconfigure in `orchestrate.main()` alone, and `flush=True` on 17 of ~83 prints — unevenly enough that `python -m pipeline.filter > log` was block-buffered regardless). That is one rule for the output *we* write, not one mechanism overall: `PYTHONUNBUFFERED` at the two redirecting callers stays, because it is the only thing covering stdout we don't route through our own `print` — libraries logging directly, and grandchild processes that inherit the env. The workflow's redundant `python -u` (which duplicated the env var exactly) is gone. The **UI process** is covered explicitly — uvicorn imports `server.py` rather than running a `__main__` block and gets no `PYTHONUNBUFFERED`, while `recheck.drain` and Add-Job's evaluator print from it — so `server.py` calls it at import. Two deliberate carve-outs: `batch_evaluate`'s interrupt summary flushes explicitly because the next line is `os._exit(130)`, which skips stdio flushing entirely; and `server.py`'s handoff log opens its sink `buffering=1` under a `redirect_stdout`, since reconfiguring `sys.stdout` can't help an in-process redirect. Deliberately not `pipeline/__init__.py`, which would reconfigure uvicorn's stdout as an import side effect of the UI's `import pipeline.sites`. `tests/test_stdio.py` guards both that every entry point calls it and that no `flush=True` comes back. |
| `pipeline/sites.py`                     | Single source of truth for what a search pass is allowed to ask for — `SUPPORTED_SITES`, the `partition_sites` matching rule, `resolve_sites(cfg)` (the per-pass board rule, including "a missing `sites` key inherits the supported boards"), `board_conflicts(cfg)` (JobSpy's per-board mutex rule, `MUTEX_GROUPS`, returning a message **per offending board** rather than raising, so the scraper can drop just that board and the UI can 400 the save; `limitation_conflict(cfg)` is the same thing collapsed to one message for the two callers that ask about the pass as a whole), and `normalize_pass(cfg)` (rewrites the four `MUTEX_KEYS` — `hours_old`, `job_type`, `is_remote`, `easy_apply` — to the value JobSpy will actually act on, dropping them when that value is falsy, so the several call sites that read them can't disagree; see the Configuration note below). `resolve_pass_sites` ([pipeline/scrape.py](pipeline/scrape.py)) and the UI's save-time validator both call these, so what the UI predicts can't drift from what a run does — the one place they deliberately differ is the *consequence* of a mutex conflict, written down at the UI's call site (see the Configuration note below). A dependency-free leaf module (imports nothing) so the jobspy-free UI venv can share it. `setup-profile.mjs` hand-mirrors the constant, since Node can't import it; `tests/test_app_onboard.py` guards both that mirror and the `onboard.html` checkboxes against drift, and `tests/test_example_config.py` guards the third mirror — the `sites:` blocks in `config/search.example.yml`, the config setup copies to `search.yml`. Adding or retiring a board starts here. |
| `config/search.yml`                     | **Edit this** — searches, filters, screening config. This is the **cloud** config (the daily decodes `SEARCH_CONFIG_B64` into it). |
| `config/search.local.yml`               | Optional **local-only** search override (gitignored). When present, local runs auto-prefer it over `search.yml` so you can search different terms locally than the cloud daily — the cloud never sees it. Manage it from the UI's "Edit local search config" panel (`/api/local-search`). Precedence in `resolve_search_config` ([orchestrate.py](orchestrate.py)): `--config` > a *custom* `SEARCH_CONFIG` env > `search.local.yml` > `search.yml` (a `SEARCH_CONFIG` equal to the shared default is treated as boilerplate, since `.env.example` ships it, so the override still wins). |
| `.env`                                  | Env vars: `RESUME_PATH`, `CAREER_OPS_PATH`, `BATCH_CLI`, `ANTHROPIC_API_KEY`, `BATCH_MODEL`, `SKILL_PATH_DEFAULT`; tailoring: `RESUME_DOCX_PATH` (default `resumes/resume.docx`), `APPLY_TAILOR_MIN_SCORE` (default 4.0), `TAILOR_MODEL`, `SOFFICE_PATH`; handoff: `HANDOFF_OUT_DIR` (where the per-site work-orders land — set it in the Setup wizard's "Browser-agent handoff folder" field, which writes the key and creates + seeds the folder with a `HANDOFF-README.md` (agent instructions) and a `PROFILE.md` (the living master, seeded from career-ops) the agent follows [`bootstrap_handoff_dir`]; `--handoff`/setup also seed it; default `output/handoff`), `HANDOFF_JOB_LOG` (optional prose log to reconcile); liveness recheck (`--recheck-liveness`): `RECHECK_BUDGET` (stalest roles re-checked per run, default 100), `RECHECK_MIN_AGE_HOURS` (don't re-check a role confirmed within this window, default 6) — together they cap the per-run fetch burst that trips LinkedIn's rate limiter; manual backlog drain (`--recheck-drain`, or auto in the UI when the backlog exceeds the budget): `RECHECK_DRAIN_COOLDOWN` (seconds between budgeted sweeps, default 60), `RECHECK_DRAIN_MAX_CYCLES` (hard cap on sweeps, default 20). The re-check only verifies sites with an unauthenticated liveness path: LinkedIn via the guest endpoint, and Indeed via the jobData GraphQL API (`screen.fetch_indeed_expiry` — the same `apis.indeed.com` endpoint + mobile-app key the JobSpy scraper uses; the Cloudflare wall only guards the website, so batched `expired`-flag lookups work where a page fetch can't). Glassdoor serves a JS anti-bot wall a fetch can't classify and has no API fallback, so it's reported `unverifiable` and skipped |
| `resumes/resume.{pdf,docx,odt}`         | Resume used to extract scoring keywords. Import any of the three (DOCX/ODT recommended — also feeds tailoring below); `pipeline/resume_text.py` dispatches extraction by extension. `extract_resume_text()` in [pipeline/filter.py](pipeline/filter.py) delegates to it; the filter auto-discovers `resume.{pdf,docx,odt}` when `RESUME_PATH` is unset. |
| `resumes/resume.docx`                   | The candidate's source résumé — feeds keyword scoring and is the default upload. **Per-job tailoring no longer slot-edits it:** `--handoff-tailor` now BUILDS a résumé from `PROFILE.md` (LLM → grounded content-JSON → one-page fit — `pipeline/resume_content.py` → `resume_render`/`resume_fit`/`resume_build.fit_to_page`, reusing `resume_tailor`'s LibreOffice/soffice helpers) and caches the gate-passed PDF at `career-ops/output/<Company> - resume.pdf`, **role-aware** via a `.role` sidecar (rebuilt when the role or `PROFILE.md` changes). Jobs scoring below `APPLY_TAILOR_MIN_SCORE` (or `--handoff-tailor-min-score`) get no pre-tailored file. The slot-edit tailor (`pipeline/resume_tailor.py`) is superseded on this path; its hand-edit-wins caching does not apply to the build-from-content model. |
| `output/handoff/next-roles-<site>.{jsonl,md}` | The **work-orders** for the browser agent — one session per job site the scraper searches from (`next-roles-linkedin.jsonl`, `next-roles-indeed.jsonl`, …; unrecognized domains go to `next-roles-other.jsonl`). Each holds that site's fresh scored roles ranked best-first, each with board/url/score/resume-base (and `resume_pdf` under `--handoff-tailor`) plus a `status` column the agent writes back (`claimed`/`applied`/`handoff`/`skip:<reason>`). Site is derived from the posting URL via `board_of()`. |
| `output/handoff/role-status.jsonl`      | The **shared status tracker** (one file across every site session) — one line per handled role keyed `company::role` (case/board/req-id normalized), so a role applied on one site never reappears on another. Seeded from a prose JOB_LOG when `HANDOFF_JOB_LOG`/`--job-log` points at one; extended by agent writeback each run. The machine source of truth for "what's done". |
| `output/handoff/PROFILE.md`             | The browser agent's **living master** — identity, a metric-carrying **fact bank** (résumé experience kept verbatim so numbers never get trimmed), an honesty-rated **skills inventory**, the **standing answers** applications keep asking (work auth, comp, location, EEO), and tailoring rules. Seeded once from `career-ops/cv.md` + `config/profile.yml` by `render_profile_md`/`bootstrap_handoff_dir`, then **grown by the agent** (append-only memory). Non-clobber — qualify + tailor every role against it. When present, it's also the **authoritative candidate profile for local LLM evaluation** — both `--evaluate-batch` and the UI's Add-Job — via the shared `eval_system_prompt` (`resolve_profile_md`), superseding the cv.md/profile.yml/_profile.md/article-digest.md seeds; the cloud evaluator picks it up too via the optional `PROFILE_MASTER_B64` secret (the Setup wizard encodes it; the daily workflow decodes it to `career-ops/PROFILE.md`). |
| `output/jobs.csv`                       | Raw scrape output. **Truncated to zero bytes when a scrape returns no rows** — rather than leave the previous run's rows to be re-processed as today's — so what `--skip-scrape` reuses may be nothing. JobSpy returns an empty result on a rate-limit (403/429/999) instead of raising, which makes a throttled run indistinguishable from a genuinely empty one. |
| `output/filtered_jobs.csv`              | Score-filtered and screened jobs. Truncated to zero bytes on every "nothing survived" exit — an empty scrape, a config that ages out every row, nothing scoring above `min_score`, every surviving URL already seen, or liveness dropping them all — for the same reason as `jobs.csv` above, so `--skip-filter` inherits the same caveat. Screen's two empty exits used to write a header-only file instead, which is not zero bytes and so read as "has content" to bridge; `pipeline/rowio.py` is now the single contract for both ends. |
| `career-ops/data/pipeline.md`           | Pending evaluation queue (checkbox list)                                                         |
| `career-ops/data/scan-history.tsv`      | All-time dedup record                                                                            |
| `career-ops/batch/batch-input.tsv`      | Batch evaluation queue                                                                           |
| `career-ops/batch/jds/*.txt`            | Cached job descriptions (inlined into evaluation prompts)                                        |
| `career-ops/batch/batch-api-state.json` | `--evaluate-batch` state — per-job metadata, report/tracker numbers, completion status           |
| `career-ops/config/profile.yml`         | Candidate profile (created by setup-profile.mjs)                                                |
| `career-ops/batch/batch-runner.sh`      | Interactive batch evaluator — `--cli claude\|opencode\|gemini\|qwen`, `--model`, `--skip-pdf`   |
| GHA Cache (key `pipeline-state-v1`)     | Per-fork runtime state (scan-history, applications.md, recheck-state, batch state). Never committed. |

---

## Configuration (`config/search.yml`)

```yaml
searches:
  - name: "pass name"
    search_terms: ["term1", "term2"]
    sites: [indeed, linkedin] # the only supported boards (see note below)
    results_wanted: 50
    location: "Dallas, TX"
    hours_old: 168 # mutually exclusive with job_type/is_remote/easy_apply on Indeed
    # is_remote: true   # a second group — uncommenting either of these makes the
    # easy_apply: true  # pass conflict with hours_old on Indeed, which is then
    #                   # dropped from the pass; LinkedIn still runs it as written
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

**JobSpy constraint**: On Indeed, `hours_old` is mutually exclusive with `job_type`, `is_remote`, and `easy_apply` — its filter builder is a precedence chain that silently drops the lower-priority filter, so the pass doesn't search what it says it searches. Split into separate search passes to use both. **LinkedIn has no such rule** and is not checked: its builder puts every filter into one params dict and sends them all (retired in #115; both readings are asserted against the installed library by `tests/test_jobspy_contract.py`, and `requirements.txt` pins the version they were read from). An option counts as set only when JobSpy would act on it — `easy_apply: false` and `hours_old: 0` send no filter, so they activate nothing and conflict with nothing. Because `ScraperInput` is a pydantic model that coerces in lax mode, **quoted scalars count too**: `easy_apply: "true"` is a live filter and `is_remote: "false"` is no filter at all, however truthy the raw string looks. `normalize_pass` ([pipeline/sites.py](pipeline/sites.py)) rewrites all four mutex keys to their JobSpy-effective value on the way into a run, before pass selection, kwarg forwarding or the per-row `easy_apply` tag reads any of them. `board_conflicts` normalizes its own input too — the same way it resolves its own boards rather than trusting `cfg["sites"]` — so the scraper, the UI's save endpoint and the example-config guard cannot get different answers by forgetting a preparatory call. A value JobSpy cannot read at all (`easy_apply: maybe`) is reported by `unreadable_options` and skips the pass with a warning, rather than reaching `scrape_jobs` and aborting the stage with a pydantic `ValidationError` after other passes have already scraped. (Worth knowing, since lax coercion is surprising here: `hours_old: true` is not a typo JobSpy rejects, it means *the last one hour*.) The scoping to those four keys is deliberate — the other 12 `OPTIONAL_PARAMS` keys have meaningful falsy values (`distance: 0`, `linkedin_fetch_description: false`) and are forwarded untouched. A pass that breaks the rule **loses the offending board, with a warning**, at scrape time rather than aborting the run — one stale pass no longer takes the healthy ones (in the cloud, the whole day) down with it. The degradation is per board because the rule is (#126): every pass the repo generates names `[indeed, linkedin]`, so `hours_old` plus `easy_apply` conflicts on Indeed and on nothing else, and skipping the pass discarded a LinkedIn search that would have run exactly as configured — verbatim the harm #115 cited, one level up. `resolve_pass_sites` ([pipeline/scrape.py](pipeline/scrape.py)) applies this and the retired-board rule in **one loop over one field** — they answer the same question, and split apart the second had to re-derive the first's answer to subtract from it. It drops the named board and keeps the rest; a pass left with no board is skipped, which is still every single-board case. An *unreadable* value skips the whole pass instead, because `scrape_jobs` builds one `ScraperInput` before it dispatches to any board — no board survives it. **The UI's local-config save still refuses a conflict outright (400)**, for any offending board, and that divergence from the runtime is deliberate: a conflict is repairable while the config is on screen, and splitting the pass costs neither board, where saving it costs the Indeed half for good and says so only in a log line. An unsupported board is warned about rather than refused precisely because it *isn't* repairable. The divergence is one-directional: on **this** rule, everything the endpoint accepts is a config a run acts on exactly as written. (That is scoped to the mutex rule on purpose — an unsupported board saves with a warning and *is* then stripped by the run, which is the same asymmetry seen from the other side.) The reasoning lives at the call site in `pipeline/app/server.py`.

**Supported scrape sites — indeed and linkedin only**: Glassdoor and ZipRecruiter sit behind a Cloudflare wall that 403s every scripted request (they contributed zero rows across months of daily runs), and Google Jobs serves degraded responses then drops the connection mid-body — jobspy's Google scraper doesn't catch that, so one truncated response used to kill the whole run and discard every row already scraped. `resolve_pass_sites` ([pipeline/scrape.py](pipeline/scrape.py)) removes anything else from a config at load time with a warning (protecting stale configs, e.g. an old `SEARCH_CONFIG_B64` cloud secret), and the UI/CLI wizards only offer these two boards.

**Description backfill**: `linkedin_fetch_description: true` makes JobSpy fetch each LinkedIn JD individually during scrape — a sequential per-job HTTP request that easily takes 30+ minutes on 1000 results. Keep it **false**. The screen stage backfills descriptions for the jobs that survive filtering:

- **LinkedIn** URLs are fetched via the public **guest job-posting endpoint** (`jobs-guest/jobs/api/jobPosting/{id}`), not the regular `/jobs/view/` page. The regular page is login-walled from datacenter IPs and inconsistently returns a sign-in preview that has no extractable JD (this caused ~17% of LinkedIn jobs to reach evaluation with an empty description). The guest endpoint returns the full JD reliably and gives a cleaner liveness signal (404 = gone, JD present = live). `linkedin_guest_jd_url()` in [pipeline/screen.py](pipeline/screen.py) does the URL mapping; `job_url` in the CSV stays the human-facing page.
- **Indeed** descriptions usually come from JobSpy directly; if missing, the screen stage extracts them from the same page it fetches for the liveness check (site-specific selectors + a generic `<body>` fallback).

Net effect: we pay ~dozens of fetches per run for the jobs that actually survive filtering, not thousands, and every LinkedIn role gets its complete JD.

---

## Filtering logic

Keywords are extracted from the resume (`resumes/resume.{pdf,docx,odt}`, no hardcoded vocab):

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

**`screened-dead` is permanent** — no run ever re-checks a URL carrying it — so only a verdict that a posting is *gone* may produce it. So **`expired` requires positive evidence** — a 404/410, an error redirect, a closure banner, or a listing page we landed on instead of the posting. `classify_liveness` ([pipeline/screen.py](pipeline/screen.py)) resolves *everything else* to `throttled`/`uncertain`, including the catch-all fallthrough, which used to be `expired`. That inversion is the point: 5xx, a 200-served bot wall and upstream's `redirected_off_posting` were all the same failure reaching the same branch, so requiring evidence retires the class instead of naming its members one at a time — the next thing upstream learns costs a re-fetch, not a role. Holding is bounded by the scraper: a held URL never enters scan-history, so it is re-checked only while the search still returns it. A `throttled` verdict is further split by `transient_throttle`: a limiter or an upstream API outage may clear, so the re-check leaves the role's timestamp unset and it returns to the front of the stalest-first queue; a wall, a 5xx or an unreadable body will not, so it is stamped and rotates through the normal cadence instead — still never expired, but not ahead of every other role forever, which would let `RECHECK_BUDGET` of them consume every run. The cases that reach the non-fatal side: a 403/429/999 wall (decided on status, before the body is touched), a **5xx** (the site is broken, not the posting removed), a **200-served anti-bot challenge** (Cloudflare "Just a moment…", hCaptcha — the status check can't see these), and any body too short to judge. The bot-challenge patterns are matched against the whole page, JD prose included, so they run *after* the apply-control check and are spelled to need Cloudflare's own wording — a false positive holds the row on every run, meaning the job is never evaluated at all. Upstream's `liveness-core.mjs` grew the same guard for the same reason; `screen.py` is a port of it, and a port drifts.

---

## The career-ops contract

career-ops is a separate upstream project on its own release cadence, and the pipeline reads, writes and executes files inside it. What that costs us, and the shape of each defence:

- **`node merge-tracker.mjs` needs `npm install`.** It used to import only Node builtins; it now reaches `js-yaml` via `tracker-utils.mjs`. A career-ops checkout updated with a bare `git pull`/rebase fails to resolve it, and `run_merge_tracker` catches that as a non-fatal failure — so evaluations pile up unmerged in `batch/tracker-additions/`. It prints an actionable hint on `ERR_MODULE_NOT_FOUND` rather than the raw stack.
- **Exit 0 no longer means "everything merged".** merge-tracker declines a row it can't read confidently (see the score cell below, or a report number marked `failed` in `batch-state.tsv`) by printing `Skipping …` — and then still archives the TSV into `merged/` and exits 0. Nothing retries it. `run_merge_tracker` compares the additions it saw pending before the merge against what actually reached the tracker afterwards — an addition that left `tracker-additions/` without its `company::role` landing in `applications.md` is lost — and prints merge-tracker's own reason lines alongside. Asking the filesystem rather than grepping upstream's prose is deliberate: those messages come in half a dozen phrasings and at least one of them is benign (a re-eval that produced no score keeps the row's existing score), so a `Skipping` grep cried wolf on intact rows.
- **The score cell must be exactly `N/5`.** merge-tracker decides which of columns 5-6 is the score by asking whether exactly one of them looks like one; when neither does it skips the row. `_normalize_score_cell` ([pipeline/_batch_common.py](pipeline/_batch_common.py)) coerces the model's output on the way out — an out-of-range value becomes `N/A` rather than being clamped, because the handoff work-order ranks by score and a fabricated 5 would put the role first. The older merge-tracker's fallback assumed this row order, so the prompt's "format X.X/5" rule was advisory; it is load-bearing now, and a prompt rule is not something a local model reliably honours.
- **Report links come back as `../reports/…`.** merge-tracker normalizes the Report cell relative to the tracker file, which the pipeline seeds at `data/applications.md`. Both shapes coexist in one file (older rows were copied verbatim). `_report_link` ([pipeline/app/data.py](pipeline/app/data.py)) strips the ascent at the single point both parsers extract it, so the three consumers that resolve it as `career_ops / report_path` — cover letters and both tailors — keep finding the report instead of silently building from the JD alone.
- **The tracker has two layouts.** career-ops supports an optional `Via` column (the agency a role comes through) after Company, migrated in with `merge-tracker.mjs --migrate-via`; its own readers map columns by header name. So do ours now (`_header_columns`, and the same anchoring in [pipeline/bridge.py](pipeline/bridge.py)), falling back to the positional order for a headerless table. Read positionally the extra cell puts the agency where Role belongs, and `company::role` is the key bridge dedup, handoff's `role_key`, the résumé-base picker and the tailored role all run on.
- **What career-ops ships as DATA is read, not mirrored.** Two contracts arrive as machine-readable files and both are loaded at runtime, cached per (path, mtime), with the baked constant as a fallback for when the checkout is absent (`run-ui.sh --data` points the UI at an extracted artifact): `templates/states.yml` → `canonical_states()`/`canonical_status()` ([pipeline/app/data.py](pipeline/app/data.py)), and `tracker-aliases.json` → `header_aliases()` ([pipeline/tracker_layout.py](pipeline/tracker_layout.py)). A 10th state now reaches the Kanban, the report-pane picker and `/api/status`'s validator with no code change — `app.js`'s `STATES` is a first-paint seed that `/api/capabilities` replaces at boot. This is the lesson of the v1.29.0 rebase written down: the hand-mirrored alias table was wrong the day it was written (missing `location`, `materials` and `apply link`; inventing eight Spanish spellings career-ops never emits), and `Hired` broke the status mirror the same week. A copy of a file that sits on disk is a bug with a delay on it.
- **`## Pendientes` is our spelling, not the only one.** career-ops writes the pending/processed headings in English and reads both; bridge matches whichever the file already uses, so it appends into the existing section instead of opening a second one.
- **`npm install` runs a Chromium download.** career-ops ships a `postinstall` of `npx playwright install chromium --with-deps`. Every install site here passes `--ignore-scripts`: `setup.sh`/`setup.ps1` install Chromium deliberately further down (and the Nix path needs a pinned version, not whatever postinstall fetches), and the daily workflow only ever shells out to `merge-tracker.mjs`, so it would be minutes of runner time plus a sudo-dependent apt step for a browser nothing there launches.

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

**Candidate profile source:** when a living `PROFILE.md` exists (handoff dir, else `career-ops/PROFILE.md` — see `resolve_profile_md`), it is the authoritative candidate profile fed to the evaluator, superseding the `cv.md`/`profile.yml`/`_profile.md`/`article-digest.md` seeds. The batch path and the UI's Add-Job eval share one builder (`eval_system_prompt`), so they resolve it identically. Absent a PROFILE.md, the four seeds are used as before. The cloud daily reads it too when the optional `PROFILE_MASTER_B64` secret is set — the Setup wizard encodes your living PROFILE.md and the workflow decodes it to `career-ops/PROFILE.md` (`resolve_profile_md`'s fallback); without the secret, the cloud uses the seed secrets exactly as before.

```powershell
./run.ps1 --evaluate-batch                         # auto-detects provider from env keys
./run.ps1 --evaluate-batch --batch-provider gemini # explicit provider
./run.ps1 --evaluate-batch --batch-concurrency 5   # more parallel workers
```

Provider auto-detection order: Gemini → Groq → DeepInfra → OpenRouter → DeepSeek → OpenAI → Anthropic (first env key found).

| Provider | Env var | Free tier |
|---|---|---|
| Gemini | `GEMINI_API_KEY` | 1,500 req/day, no credit card |
| Groq | `GROQ_API_KEY` | Yes — fast open-source models |
| DeepInfra | `DEEPINFRA_API_KEY` | No — pay-as-you-go open-weight models |
| OpenRouter | `OPENROUTER_API_KEY` | No — pay-as-you-go, many models via one key |
| DeepSeek | `DEEPSEEK_API_KEY` | No — pay-as-you-go, cheaper than frontier |
| OpenAI | `OPENAI_API_KEY` | No |
| Anthropic | `ANTHROPIC_API_KEY` | No |
| Ollama | `OLLAMA_BASE_URL` | Local — no API key needed |

Set in `.env` or as repository secrets for GitHub Actions. `BATCH_PROVIDER` overrides auto-detection.

---

## Cloud automation (GitHub Actions)

> **Privacy first**: every cloud workflow refuses to run unless your fork is private. The Actions tab of a public repo exposes workflow run history, schedule cadence, and durations — all of which reveal active job searching. See the privacy notice at the top of [README.md](README.md).

Cloud automation workflows:

| Workflow | Schedule | What it does |
|----------|----------|--------------|
| `daily-pipeline.yml` | Noon UTC (7 AM CDT) | Runs **every** search pass (including any `easy_apply: true` pass) once a day — no pass-selection flag. Scrape → filter → screen → bridge → evaluate. |
| `gc-actions-storage.yml` | Sundays 03:30 UTC | Garbage-collects the two things that consume the account's Actions **storage** quota: artifacts beyond the newest N *per artifact name*, and workflow **run logs** older than 30 days (retention settings don't cover logs at all, and nothing else here removes them). Skips in the template repo; no private-repo hard-stop, since pruning your own storage exposes nothing. `workflow_dispatch` takes `keep_artifacts` / `log_retention_days` / `dry_run`. |
| `export-reports.yml` | Manual (`workflow_dispatch`) | Packages the **full** `career-ops/reports/`, plus the current `data/applications.md` the Report links resolve out of, from the state cache as one downloadable artifact — the recovery path the per-run daily artifact removed (a fresh local install gets the whole tracker but only the last run's report files, so every Report link would 404). Restore-only, so unlike `seed-reports.yml` it's safe to run while a pipeline is in flight — which is why it's the one pipeline-state workflow with **no** `concurrency:` block (see the runtime-state bullet below). Dispatch-only on purpose: `tests/test_workflow_artifacts.py` forbids a **scheduled** upload from naming cached state, which is exactly the bug, but a human asking for the history once is bounded by the asking. Both path entries are load-bearing: `upload-artifact` roots the archive at the common ancestor of the paths it is given, so naming only `reports/` would re-root it and the download would stop extracting over a local `career-ops/`. |
| `edit-tracker.yml` | Manual (`workflow_dispatch`) | Replaces `applications.md` in the cache with a user-supplied base64 blob. Use after editing the tracker locally. |
| `update-from-template.yml` | Manual (`workflow_dispatch`) | Merges the upstream template's latest `main` into this copy and pushes (copies have no fork link). Skips in the template repo itself; aborts on conflict. The UI's **⬆ Update** button does the same merge+push locally (also refreshing the clone running the UI); `pipeline/app/self_update.py` holds the logic. |

**Storage model — no user data is ever committed:**

- **Setup data** (CV, profile, search config) → repository **Secrets** (encrypted at rest, not visible to forkers)
- **Runtime state** (scan-history, applications.md, pipeline.md, recheck-state, batch state, cached JDs) → **`actions/cache`** keyed `pipeline-state-v1`. Per-fork, restored at workflow start, saved at workflow end. Invisible to anyone but the fork owner. **Three workflows write it** — the daily, `edit-tracker.yml` and `seed-reports.yml` — and a save is a *whole snapshot*, not a merge, so two overlapping writers mean the later save wins outright and the other run's work is gone unannounced. All three therefore share the `pipeline-state` **concurrency group**; the daily sat in a group of its own until #135, which left that the one writer race nothing serialized and nothing documented (the UI's **Push** dispatches `edit-tracker.yml`, and the noon daily is long). A Push dispatched mid-daily now queues behind it. The serialized window is a writer's *whole run*, not its save step — each one restores at the start and saves at the end, so a save landing anywhere in between is overwritten. `export-reports.yml` is the deliberate exception: it restores and never saves, and a restore matches a complete, immutable entry and can clobber nothing, so joining the group would buy no safety and would make the recovery path queue behind the very run you're recovering alongside. The cost of the group is GitHub's **one pending run per group** rule — dispatch a second writer while one is queued and the queued one is cancelled. That bites hardest on Push, because `/api/push-status` clears a pending override the moment the dispatch is accepted, so an evicted Push loses those status edits from the cloud and pressing Push again sends nothing; re-drag the cards if a queued **Edit Tracker** run turns up cancelled. Worth taking: a cancelled run is visible in the Actions tab, a clobbered cache is visible nowhere and costs a day's evaluations, reports, scan-history rows and batch state.
- **Outputs** (**this run's** new reports + the current tracker) → **`actions/upload-artifact`**. Downloadable from the Actions tab for **7 days**. Both halves are load-bearing, and each is useless alone: `career-ops/reports/` is restored from the state cache every run and only grows, so uploading it whole put the entire report history into *every* artifact — and retention bounds how MANY artifacts are alive, never how big each one is. Ninety days of a monotonically growing directory made live storage grow with the *square* of how long the pipeline had run, which exhausted the account's Actions storage; an exhausted storage quota stops GitHub creating workflow runs at all, including the Tests run on every PR, and nothing in the Actions UI connects the two. So the daily now snapshots `reports/` right after the cache restore and uploads only what the run itself minted ([pipeline/run_artifact.py](pipeline/run_artifact.py)). `data/pipeline.md` and `data/easy-apply-urls.txt` no longer ride along either — both are append-only and nothing read them out of the artifact, so they reproduced the same shape at a smaller constant. What's left that grows is `applications.md`, which is irreducible: the UI's **↻ Refresh** *merges* the tracker, so a diff of it would be useless, and a row is ~200 bytes against ~4KB for a report. The artifact keeps career-ops' layout, so the Refresh merge is unchanged — it just accumulates the history into your local `career-ops/` a run at a time, and **Export Reports** pulls the whole backlog down in one go when that isn't enough. `tests/test_workflow_artifacts.py` guards the retention ceiling, the classification of every cached path, and the rule that no *scheduled* upload names cached state. It also guards the rule the cache itself rests on (#133): **every** workflow's `actions/cache` restore/save must name the same path set and the same key prefix as the daily's. GitHub hashes the path set into the cache *version* and matches a restore on prefix AND version, so a workflow whose list has drifted does not restore less — it matches nothing and silently starts a lineage of its own. `export-reports.yml` shipped naming two of the nine paths and so never restored anything; `seed-reports.yml` named seven and spent months repairing into a cache the daily never read. And the same rule from the other side (#135): every workflow that **saves** the cache must run under the `pipeline-state` concurrency group, and none of them may set `cancel-in-progress: true` — every save step is `if: always()`, which fires on cancellation too, so cancelling a daily mid-run would write its half-evaluated batch state and partly-merged tracker over the good snapshot on the way out. Anything under `actions/cache` that isn't explicitly the `restore` half counts as a writer, so a paste of the combined `actions/cache@v6` action fails the rule rather than escaping it.

**Pass selection flags** (mutually exclusive, for manual/local runs): `--only-pass "name1,name2"` (case-insensitive name match, errors on no match), `--easy-apply-only` (passes with `easy_apply: true`, no-ops if none), `--no-easy-apply` (passes without `easy_apply: true`). The daily cloud workflow runs **every** pass with no selection flag; these flags exist for ad-hoc local runs and route by the `easy_apply` field rather than name so user-renamed passes still work.

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

   Optional: `PROFILE_MD_B64` (`career-ops/modes/_profile.md`), `ARTICLE_DIGEST_B64` (`career-ops/article-digest.md`), `PROFILE_MASTER_B64` (your living `PROFILE.md` → `career-ops/PROFILE.md`, so the cloud evaluator scores against the same master as local eval).

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
