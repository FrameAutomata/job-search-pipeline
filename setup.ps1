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

Write-Host "==> Cloning career-ops (if missing)"
$careerOps = "$root\career-ops"
if (-not (Test-Path $careerOps)) {
    git clone --branch dev/batch-local-llm https://github.com/FrameAutomata/career-ops-1 "$careerOps"
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

Write-Host "==> Copying example configs"
if (-not (Test-Path "$root\.env")) { Copy-Item "$root\.env.example" "$root\.env" }
if (-not (Test-Path "$root\config\search.yml")) { Copy-Item "$root\config\search.example.yml" "$root\config\search.yml" }
if (-not (Test-Path "$root\resumes")) { New-Item -ItemType Directory "$root\resumes" | Out-Null }
if (-not (Test-Path "$root\output")) { New-Item -ItemType Directory "$root\output" | Out-Null }

Write-Host ""
Write-Host "==> Running profile setup"
node "$root\setup-profile.mjs"
