# Thin wrapper: activate venv + run orchestrator. Pass-through args.
# --batch            : evaluate pending jobs via career-ops batch runner (CLI set by BATCH_CLI, default: claude)
# --skip-pdf         : skip PDF generation (report + tracker only)
# --min-score <N>    : skip tracker for jobs scoring below N (0 = off)
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

if (-not (Test-Path "$root\career-ops\config\profile.yml")) {
    Write-Host "Profile not found — running first-time setup..."
    node "$root\setup-profile.mjs"
}

# Parse flags — index loop required to consume --min-score's value argument
$runBatch = $false
$skipPdf = $false
$minScore = $null
$orchestrateArgs = @()
$i = 0
while ($i -lt $args.Count) {
    switch ($args[$i]) {
        "--batch"     { $runBatch = $true }
        "--skip-pdf"  { $skipPdf = $true }
        "--min-score" { $i++; $minScore = $args[$i] }
        default       { $orchestrateArgs += $args[$i] }
    }
    $i++
}

& "$root\.venv\Scripts\python.exe" "$root\orchestrate.py" @orchestrateArgs

if ($runBatch) {
    $batchCli = if ($env:BATCH_CLI) { $env:BATCH_CLI } else { "claude" }
    $batchRunner = "$root\career-ops\batch\batch-runner.sh"
    $batchArgs = @("--cli", $batchCli)
    $displayStr = $batchCli
    if ($env:OLLAMA_MODEL) { $batchArgs += @("--model", $env:OLLAMA_MODEL); $displayStr += " / $($env:OLLAMA_MODEL)" }
    if ($skipPdf) { $batchArgs += "--skip-pdf" }
    if ($minScore) { $batchArgs += @("--min-score", $minScore) }
    Write-Host ""
    Write-Host "==> Running batch evaluation ($displayStr)..."
    # Prefer Git for Windows bash over WSL bash (WSL requires a VM via HCS).
    $gitBash = @(
        "$env:ProgramFiles\Git\bin\bash.exe",
        "${env:ProgramFiles(x86)}\Git\bin\bash.exe",
        "$env:LocalAppData\Programs\Git\bin\bash.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    $bashExe = if ($gitBash) { $gitBash } `
               elseif ((Get-Command bash -ErrorAction SilentlyContinue) -and
                       (Get-Command bash).Source -notmatch 'System32') { "bash" } `
               else { $null }
    if ($bashExe) {
        & $bashExe $batchRunner @batchArgs
    } else {
        Write-Host "ERROR: Git for Windows not found. Install from https://git-scm.com/download/win"
        exit 1
    }
}

