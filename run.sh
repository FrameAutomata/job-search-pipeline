#!/usr/bin/env bash
# --batch            : evaluate pending jobs via career-ops batch runner (CLI set by BATCH_CLI, default: claude)
# --skip-pdf         : skip PDF generation (report + tracker only)
# --min-score <N>    : skip tracker for jobs scoring below N (0 = off)
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ ! -f "$root/career-ops/config/profile.yml" ]; then
  echo "Profile not found — running first-time setup..."
  node "$root/setup-profile.mjs"
fi

# Parse flags — while/case required to consume --min-score's value argument
run_batch=false
skip_pdf=false
min_score=""
orchestrate_args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --batch)      run_batch=true; shift ;;
    --skip-pdf)   skip_pdf=true; shift ;;
    --min-score)  min_score="$2"; shift 2 ;;
    *)            orchestrate_args+=("$1"); shift ;;
  esac
done

"$root/.venv/bin/python" "$root/orchestrate.py" "${orchestrate_args[@]}"

if [[ "$run_batch" == "true" ]]; then
  batch_cli="${BATCH_CLI:-claude}"
  batch_args=(--cli "$batch_cli")
  display_str="$batch_cli"
  if [[ -n "${OLLAMA_MODEL:-}" ]]; then
    batch_args+=(--model "$OLLAMA_MODEL")
    display_str="$batch_cli / $OLLAMA_MODEL"
  fi
  [[ "$skip_pdf" == "true" ]] && batch_args+=(--skip-pdf)
  [[ -n "$min_score" ]] && batch_args+=(--min-score "$min_score")
  echo ""
  echo "==> Running batch evaluation ($display_str)..."
  bash "$root/career-ops/batch/batch-runner.sh" "${batch_args[@]}"
fi

