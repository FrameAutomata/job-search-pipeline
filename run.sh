#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$root/.venv/bin/python" "$root/orchestrate.py" "$@"
