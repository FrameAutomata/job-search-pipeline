# Launch the local triage UI (Windows).
#   ./run-ui.ps1                      # serve on :8000, read ./career-ops
#   ./run-ui.ps1 -Data path\to\dir    # read a different dir (e.g. an extracted artifact)
#   ./run-ui.ps1 -Port 8123
#   ./run-ui.ps1 -Lan                 # also serve to your home network (needs UI_PASSWORD)
param(
    [int]$Port = 8000,
    [string]$Data = "",
    [switch]$Lan
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

# LAN mode binds every interface, so anyone on the network can reach the port.
# A password is therefore not optional, and refusing here — before uvicorn ever
# binds — is the only refusal the user sees as a shell error. The server refuses
# again at import for the same reason from the other side: this script cannot
# read .env, and the server can, so a UI_PASSWORD that lives only in .env
# satisfies the server's check and not this one.
$hostArgs = @()
if ($Lan) {
    if (-not $env:UI_PASSWORD) {
        Write-Error ("-Lan needs UI_PASSWORD: this puts the UI on every interface of " +
                     "this machine, so it refuses to start without one. Try: " +
                     "`$env:UI_PASSWORD = 'something long'; ./run-ui.ps1 -Lan")
        exit 1
    }
    $env:UI_LAN = "1"
    $hostArgs = @("--host", "0.0.0.0")
    Write-Host "==> LAN mode: sign in with any username and UI_PASSWORD. Basic auth over"
    Write-Host "    plain HTTP is for a network you trust — stop the server before joining"
    Write-Host "    another one. From the LAN this UI can move a card and push it, nothing else."
}

Write-Host "==> Triage UI on http://localhost:$Port  (reading: $reading)"
& $py -m uvicorn pipeline.app.server:app --port $Port @hostArgs
