#!/usr/bin/env bash
# Launch the local triage UI (macOS / Linux).
#   ./run-ui.sh                       # serve on :8000, read ./career-ops
#   ./run-ui.sh --data path/to/dir    # read a different dir (e.g. an extracted artifact)
#   ./run-ui.sh --port 8123
#   ./run-ui.sh --lan                 # also serve to your home network (needs UI_PASSWORD)
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

port=8000
data=""
lan=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) port="$2"; shift 2 ;;
    --data) data="$2"; shift 2 ;;
    --lan) lan="1"; shift ;;
    *) echo "unknown arg: $1"; exit 1 ;;
  esac
done

py="$root/.venv/bin/python"
if ! "$py" -c "import uvicorn, fastapi, markdown" 2>/dev/null; then
  echo "UI dependencies missing. Installing requirements-ui.txt..."
  "$py" -m pip install -r "$root/requirements-ui.txt"
fi

[[ -n "$data" ]] && export CAREER_OPS_PATH="$data"

# LAN mode binds every interface, so anyone on the network can reach the port.
# A password is therefore not optional, and refusing here — before uvicorn ever
# binds — is the only refusal the user sees as a shell error. The server refuses
# again at import for the same reason from the other side: this script cannot
# read .env, and the server can, so a UI_PASSWORD that lives only in .env
# satisfies the server's check and not this one. Export it (or put it in .env
# and export UI_PASSWORD= from the same value) before using --lan.
host_args=()
if [[ -n "$lan" ]]; then
  if [[ -z "${UI_PASSWORD:-}" ]]; then
    echo "--lan needs UI_PASSWORD: this puts the UI on every interface of this" >&2
    echo "machine, so it refuses to start without one. Try:" >&2
    echo "  UI_PASSWORD='something long' ./run-ui.sh --lan" >&2
    exit 1
  fi
  export UI_LAN=1
  host_args=(--host 0.0.0.0)
  echo "==> LAN mode: sign in with any username and UI_PASSWORD. Basic auth over"
  echo "    plain HTTP is for a network you trust — stop the server before joining"
  echo "    another one. From the LAN this UI can move a card and push it, nothing else."
fi

echo "==> Triage UI on http://localhost:$port  (reading: ${CAREER_OPS_PATH:-./career-ops})"
# `${a[@]+"${a[@]}"}` rather than `"${a[@]}"`: under `set -u` an empty array is
# an unbound variable on bash 3.2, which is what macOS still ships.
exec "$py" -m uvicorn pipeline.app.server:app --port "$port" ${host_args[@]+"${host_args[@]}"}
