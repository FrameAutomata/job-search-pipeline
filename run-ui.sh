#!/usr/bin/env bash
# Launch the local triage UI (macOS / Linux).
#   ./run-ui.sh                       # serve on :8000, read ./career-ops
#   ./run-ui.sh --data path/to/dir    # read a different dir (e.g. an extracted artifact)
#   ./run-ui.sh --port 8123
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

port=8000
data=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) port="$2"; shift 2 ;;
    --data) data="$2"; shift 2 ;;
    *) echo "unknown arg: $1"; exit 1 ;;
  esac
done

py="$root/.venv/bin/python"
if ! "$py" -c "import uvicorn, fastapi, markdown" 2>/dev/null; then
  echo "UI dependencies missing. Installing requirements-ui.txt..."
  "$py" -m pip install -r "$root/requirements-ui.txt"
fi

[[ -n "$data" ]] && export CAREER_OPS_PATH="$data"
echo "==> Triage UI on http://localhost:$port  (reading: ${CAREER_OPS_PATH:-./career-ops})"
exec "$py" -m uvicorn pipeline.app.server:app --port "$port"
