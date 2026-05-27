# Launch the local triage UI (Windows).
#   ./run-ui.ps1                      # serve on :8000, read ./career-ops
#   ./run-ui.ps1 -Data path\to\dir    # read a different dir (e.g. an extracted artifact)
#   ./run-ui.ps1 -Port 8123
param(
    [int]$Port = 8000,
    [string]$Data = ""
)
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$py = "$root\.venv\Scripts\python.exe"

& $py -c "import uvicorn, fastapi, markdown" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "UI dependencies missing. Installing requirements-ui.txt..."
    & $py -m pip install -r "$root\requirements-ui.txt"
}

if ($Data) { $env:CAREER_OPS_PATH = $Data }
$reading = if ($env:CAREER_OPS_PATH) { $env:CAREER_OPS_PATH } else { "./career-ops" }
Write-Host "==> Triage UI on http://localhost:$Port  (reading: $reading)"
& $py -m uvicorn pipeline.app.server:app --port $Port
