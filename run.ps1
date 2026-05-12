# Thin wrapper: activate venv + run orchestrator. Pass-through args.
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
& "$root\.venv\Scripts\python.exe" "$root\orchestrate.py" @args
