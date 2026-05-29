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
    git clone --branch dev/batch-local-llm https://github.com/FrameAutomata/career-ops "$careerOps"
} else {
    Write-Host "    career-ops already present, skipping clone"
}

Write-Host "==> Installing career-ops node deps"
Push-Location $careerOps
npm install
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

Write-Host "==> Registering the Playwright MCP server with Claude Code (for the apply skill)"
if (Get-Command claude -ErrorAction SilentlyContinue) {
    # `claude mcp add` errors if the server is already registered. That's a
    # benign re-run — log and continue rather than aborting the whole setup.
    try { claude mcp add playwright -- npx -y "@playwright/mcp@latest" }
    catch { Write-Host "    Playwright MCP already registered (or claude mcp add failed): $($_.Exception.Message). Continuing." -ForegroundColor Yellow }
} else {
    Write-Host "    'claude' CLI not found on PATH — skipping. Install Claude Code, then run:" -ForegroundColor Yellow
    Write-Host '      claude mcp add playwright -- npx -y @playwright/mcp@latest' -ForegroundColor Yellow
}

Write-Host "==> Copying example configs"
if (-not (Test-Path "$root\.env")) { Copy-Item "$root\.env.example" "$root\.env" }
if (-not (Test-Path "$root\config\search.yml")) { Copy-Item "$root\config\search.example.yml" "$root\config\search.yml" }
if (-not (Test-Path "$root\resumes")) { New-Item -ItemType Directory "$root\resumes" | Out-Null }
if (-not (Test-Path "$root\output")) { New-Item -ItemType Directory "$root\output" | Out-Null }

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
