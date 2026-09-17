# Job-search-pipeline setup (Windows / PowerShell).
# Creates Python venv, installs deps, clones career-ops, copies example configs.

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

Write-Host "==> Creating Python venv at .venv (Python 3.12 — jobspy pins numpy==1.26.3 which has no 3.13 wheel)"
# Prefer py launcher with 3.12; fall back to bare python.
if (Get-Command py -ErrorAction SilentlyContinue) {
    py -3.12 -m venv "$root\.venv"
} else {
    python -m venv "$root\.venv"
}
& "$root\.venv\Scripts\python.exe" -m pip install --upgrade pip
& "$root\.venv\Scripts\pip.exe" install -r "$root\requirements.txt"

Write-Host "==> Installing local UI deps (triage + onboarding app)"
& "$root\.venv\Scripts\pip.exe" install -r "$root\requirements-ui.txt"

Write-Host "==> Cloning career-ops (if missing)"
$careerOps = "$root\career-ops"
if (-not (Test-Path $careerOps)) {
    git clone --branch main https://github.com/career-ops-hq/career-ops "$careerOps"
} else {
    Write-Host "    career-ops already present, skipping clone"
    # career-ops used to be cloned from a fork, for two features upstream has
    # since merged. A checkout's git state is the person's, so setup never
    # rewrites a remote — it prints the move instead. The bare path, not a URL,
    # so an ssh remote matches too. try/catch, not just 2>$null: under
    # ErrorActionPreference Stop, Windows PowerShell turns a redirected native
    # stderr line into a terminating error, and a missing origin writes one.
    # `checkout -b`, not `-B`: a local main of the person's own is refused,
    # loudly, not reset.
    $origin = ""
    if (Test-Path "$careerOps\.git") {
        try { $origin = "$(git -C "$careerOps" remote get-url origin 2>$null)" } catch { }
    }
    if ($origin -match 'FrameAutomata/career-ops') {
        Write-Host "    NOTE: career-ops/ still tracks the retired fork. To move it to upstream main" -ForegroundColor Yellow
        Write-Host "    (cv.md, data/, reports/ and config/profile.yml are untracked and stay put):" -ForegroundColor Yellow
        Write-Host "      git -C career-ops remote set-url origin https://github.com/career-ops-hq/career-ops"
        Write-Host "      git -C career-ops fetch origin --prune"
        Write-Host "      git -C career-ops checkout -b main origin/main"
        Write-Host "      Push-Location career-ops; npm install --ignore-scripts; Pop-Location"
    }
}

Write-Host "==> Installing career-ops node deps"
Push-Location $careerOps
# --ignore-scripts: career-ops ships a postinstall that runs
# `npx playwright install chromium --with-deps`. Chromium is installed
# deliberately below; `--with-deps` is a Linux package-manager step that has
# nothing to do here.
npm install --ignore-scripts
Pop-Location

Write-Host "==> Installing pipeline node deps (yaml, pdf-parse)"
Push-Location $root
npm install
Pop-Location

Write-Host "==> Installing Playwright Chromium (used by the PDF + apply skills, ~150 MB)"
Push-Location $careerOps
# `npx playwright install` is idempotent: it no-ops if Chromium of the same
# version is already on disk, so re-running setup is cheap.
try { npx --yes playwright install chromium }
catch { Write-Host "    Playwright Chromium install failed: $($_.Exception.Message). Re-run later from career-ops/." -ForegroundColor Yellow }
Pop-Location

Write-Host "==> Registering the Playwright MCP server with every installed agent CLI (for the apply skill)"
# The registry in pipeline/agent_cli.py knows each CLI's registration (an
# `mcp add` for Gemini CLI / Qwen Code / Claude Code, a JSON config merge for
# OpenCode), treats a "server already registered" error as the benign re-run
# it is, and prints the free-first install hints itself when nothing is
# installed. It always exits 0, so a missing CLI never aborts setup.
Push-Location $root
try { & "$root\.venv\Scripts\python.exe" -m pipeline.agent_cli --register-mcp-all-installed }
catch { Write-Host "    Playwright MCP registration skipped: $($_.Exception.Message)" -ForegroundColor Yellow }
Pop-Location

# Tailored resumes (--apply): python-docx comes from requirements.txt; the
# one-page verification additionally uses LibreOffice when present.
if (-not (Test-Path "C:\Program Files\LibreOffice\program\soffice.exe") -and
    -not (Get-Command soffice -ErrorAction SilentlyContinue)) {
    Write-Host "    Note: LibreOffice not found - tailored resumes will upload as .docx" -ForegroundColor Yellow
    Write-Host "    without one-page verification. Install LibreOffice to enable it." -ForegroundColor Yellow
}

Write-Host "==> Copying example configs"
if (-not (Test-Path "$root\.env")) { Copy-Item "$root\.env.example" "$root\.env" }
if (-not (Test-Path "$root\config\search.yml")) { Copy-Item "$root\config\search.example.yml" "$root\config\search.yml" }
if (-not (Test-Path "$root\resumes")) { New-Item -ItemType Directory "$root\resumes" | Out-Null }
if (-not (Test-Path "$root\output")) { New-Item -ItemType Directory "$root\output" | Out-Null }

Write-Host "==> Preparing the browser-agent handoff folder (creates it + seeds a README)"
Push-Location $root
try { & "$root\.venv\Scripts\python.exe" -m pipeline.handoff --bootstrap-dir }
catch { Write-Host "    handoff bootstrap skipped: $($_.Exception.Message)" -ForegroundColor Yellow }
Pop-Location

Write-Host ""
Write-Host "==> Setup complete."
Write-Host ""
Write-Host "Next — finish setup in your browser:"
Write-Host "    .\run-ui.ps1        then open http://localhost:8000  and click  'Setup'"
Write-Host ""
Write-Host "The Setup wizard collects your resume + preferences and writes them to your"
Write-Host "private repo's GitHub secrets, so the pipeline can run in the cloud."
Write-Host "It needs the GitHub CLI:  install gh (https://cli.github.com), then 'gh auth login'."
Write-Host ""
Write-Host "Prefer the terminal instead? Run:  node setup-profile.mjs"
