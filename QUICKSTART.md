# Quick Start — Job Search Pipeline

Scrape jobs, filter them, and feed them into career-ops for evaluation via CLI.

---

## What This Repository Does

This is a **job scraper + filter** that prepares jobs for evaluation through the [career-ops CLI system](https://github.com/santifer/career-ops).

The workflow:
1. **Scrape** jobs from job boards into `career-ops/data/pipeline.md`
2. **Filter** by your target roles via `config/search.yml`
3. **Invoke career-ops CLI** to score and evaluate high-fit roles
4. **Track applications** in `career-ops/data/applications.md`

This repo does NOT contain the evaluation logic — that lives in career-ops.

---

## Setup (One-Time)

### 1. Install Dependencies

```bash
npm install
```

This installs:
- `yaml` — Parse your YAML config
- `pdf-parse` — Extract text from resume PDFs

### 2. Generate Your Profile

```bash
node setup-profile.mjs
```

**What it does:**
- Reads your resume (PDF or text)
- Extracts: name, email, phone, location, LinkedIn, GitHub
- Syncs `config/search.yml` target roles with your profile archetypes
- Creates:
  - `career-ops/config/profile.yml` — Your canonical profile
  - `career-ops/cv.md` — Your CV in markdown
  - `career-ops/modes/_profile.md` — Your career narrative & deal-breakers

**What you'll see:**
```
🚀 Career-Ops Profile Setup

📚 Loading career-ops context...
👤 Setting up profile for: Thomas Thirlwall

📋 Target roles:
   Primary: Software Engineer, DevOps Engineer
   Secondary: Full Stack, Backend, Frontend, Mobile

✅ Profile saved: career-ops/config/profile.yml
✅ CV saved: career-ops/cv.md
✅ Narrative saved: career-ops/modes/_profile.md
```

**Options:**
```bash
node setup-profile.mjs --resume path/to/resume.pdf  # Specify resume
node setup-profile.mjs --force                       # Overwrite existing
```

### 3. Verify Setup

Check these files exist:
```bash
ls career-ops/config/profile.yml
ls career-ops/cv.md
ls career-ops/modes/_profile.md
```

---

## Regular Workflow

### Step 1: Scrape Jobs

Use your scraper to populate `career-ops/data/pipeline.md`:

```
# Pipeline

## Pendientes

- [ ] {URL} | {Source} | {Title}
  <details><summary>Description</summary>
  {Job description HTML/text}
  </details>
```

Each job is a markdown checklist item with URL, source, and full description.

### Step 2: Invoke career-ops via CLI

Score and evaluate jobs using the career-ops CLI:

```bash
# Score all jobs (replace with your CLI command)
claude -p "I have 463 jobs to batch-score. Here's the pipeline:

$(cat career-ops/data/pipeline.md)

Use my profile at career-ops/config/profile.yml and narrative at career-ops/modes/_profile.md to score these 1-5 based on role fit, seniority level, location preference, and tech stack match. Output a markdown table sorted by score (descending) with columns: Score | Title | Company | URL."
```

Or using other CLIs:
```bash
# Opencode (Codex)
opencode run "..."

# Gemini
gemini -p "..."

# Copilot
copilot -p "..."
```

### Step 3: Identify High-Scoring Roles

The CLI will output jobs sorted by score. Roles scoring 4.0+ are worth full evaluation.

### Step 4: Evaluate Selected Roles

For each role you want to apply to, invoke career-ops evaluation mode:

```bash
claude -p "Please evaluate this job for me using the career-ops framework (A-G blocks):

**Job Title:** {title}
**Company:** {company}
**URL:** {url}
**Description:** {job description}

My profile: 
$(cat career-ops/config/profile.yml)

My CV:
$(cat career-ops/cv.md)

My narrative:
$(cat career-ops/modes/_profile.md)

Provide a detailed evaluation covering: Role match, CV alignment, seniority fit, compensation, customization potential, interview prep needs, and posting legitimacy."
```

The CLI will return a detailed report you can save to `career-ops/reports/{number}-{company-slug}-{date}.md`.

### Step 5: Track Applications

Update `career-ops/data/applications.md` as you apply:

```markdown
| # | Date | Company | Role | Status | Score | PDF | Report | Notes |
|---|------|---------|------|--------|-------|-----|--------|-------|
| 1 | 2026-05-14 | Crossing Hurdles | Software Engineer (Fullstack - React, Node.js) | Applied | 4.7 | ✅ | [1](reports/001-crossing-hurdles-2026-05-14.md) | Strong remote match |
```

---

## Customizing Your Profile

After initial setup, edit these files to improve targeting:

**`career-ops/config/profile.yml`**
- Your target roles and seniority levels
- Compensation expectations
- Location flexibility
- Visa/sponsorship needs

**`career-ops/modes/_profile.md`**
- Your narrative for each archetype
- Deal-breakers (manager roles, on-site, senior roles, etc.)
- Location scoring policy
- Negotiation scripts

**`career-ops/cv.md`**
- Skills, experience, projects
- Proof points with metrics
- Education

Changes take effect immediately on next CLI invocation.

---

## File Structure

```
job-search-pipeline/
├── QUICKSTART.md                    # This file
├── package.json                     # Dependencies
├── setup-profile.mjs                # Profile generation script
├── config/
│   └── search.yml.example           # Job scraper config template
│   └── search.yml                   # (generated, not committed)
├── career-ops/
│   ├── config/
│   │   └── profile.yml              # Your profile (generated)
│   ├── cv.md                        # Your CV (generated)
│   ├── modes/
│   │   ├── _profile.md              # Your narrative (generated)
│   │   └── [career-ops modes...]    # System files (auto-updated)
│   ├── data/
│   │   ├── pipeline.md              # Jobs to evaluate
│   │   ├── applications.md          # Application tracker
│   │   └── [session outputs...]     # Temporary scoring results
│   ├── reports/                     # Evaluation reports (saved manually)
│   └── output/                      # Generated PDFs (saved manually)
└── resumes/                         # Your resume (optional)
```

---

## Tips

**Q: What should I use this for vs. career-ops directly?**

- **This repo:** Scraping, filtering, organizing jobs into pipeline.md
- **career-ops:** Evaluating individual roles (scoring, reports, applications, interviews)
- **Together:** Scrape → Filter → Evaluate via CLI

**Q: Where do I put my resume?**

Place it at `resumes/resume.pdf` or set `RESUME_PATH` in `.env`:

```bash
echo "RESUME_PATH=path/to/your/resume.pdf" >> .env
```

**Q: How do I update my profile after setup?**

Edit `career-ops/config/profile.yml` and `career-ops/modes/_profile.md` directly. Changes apply immediately to next CLI invocation.

**Q: Should I commit scored-jobs files?**

No — they're session-specific and gitignored. Commit only:
- `career-ops/config/profile.yml` (once generated)
- `career-ops/cv.md` (once generated)
- `career-ops/modes/_profile.md` (once generated)
- `career-ops/data/applications.md` (your tracker — update as you apply)

**Q: Can I use different CLIs (Claude, Gemini, Copilot, etc.)?**

Yes — the pipeline doesn't depend on any specific CLI. You can switch between them per invocation. See career-ops/AGENTS.md for multi-CLI patterns.

---

## Next Steps

1. Run `node setup-profile.mjs` to generate your profile
2. Review `career-ops/config/profile.yml` and `career-ops/modes/_profile.md`
3. Adjust target roles or deal-breakers as needed
4. Use your job scraper to populate `career-ops/data/pipeline.md`
5. Invoke career-ops CLI to score and evaluate

---

## Resources

- **Career-ops system:** See `career-ops/AGENTS.md` for full documentation
- **Job scraping:** Implement your scraper or use existing tools (ScraperJS, Puppeteer, etc.)
- **CLI options:** Try Claude, Opencode, Gemini, Copilot, Qwen — they all work
