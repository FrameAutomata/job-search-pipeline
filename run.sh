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

# Parse flags — while/case required to consume value arguments.
#
# Anything not matched here goes to orchestrate.py, which calls parse_args and
# exits on an unrecognized flag. That default is why batch-runner.sh's own
# options were unreachable through run.sh: `./run.sh --batch --parallel 4` sent
# --parallel to the orchestrator and died before scraping. --parallel is the
# flag that decides whether a few-hundred-job queue is finishable at all
# (evaluations run ~2-4 min each, serially by default), and --resume-paused is
# how you recover a batch the CLI rate-limited, so both being unreachable made
# the batch path look worse than it is.
run_batch=false
skip_pdf=false
min_score=""
batch_passthrough=()
orchestrate_args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --batch)      run_batch=true; shift ;;
    --skip-pdf)   skip_pdf=true; shift ;;
    --min-score)  min_score="$2"; shift 2 ;;
    # batch-runner.sh options, forwarded verbatim. Value-taking:
    --parallel|--limit|--start-from|--max-retries|--rate-limit-sleep)
                  batch_passthrough+=("$1" "$2"); shift 2 ;;
    # Boolean:
    --retry-failed|--resume-paused|--status|--watch)
                  batch_passthrough+=("$1"); shift ;;
    *)            orchestrate_args+=("$1"); shift ;;
  esac
done

# These only reach batch-runner.sh, so without --batch they would do nothing at
# all. Say so rather than running a full scrape and silently ignoring them.
if [[ ${#batch_passthrough[@]} -gt 0 && "$run_batch" != "true" ]]; then
  echo "run.sh: ${batch_passthrough[0]} applies to batch evaluation; pass --batch too." >&2
  exit 2
fi

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
  # Guarded: "${a[@]}" on an empty array is an unbound-variable error under
  # `set -u` on bash < 4.4, and this script runs wherever the user's bash is.
  [[ ${#batch_passthrough[@]} -gt 0 ]] && batch_args+=("${batch_passthrough[@]}")
  echo ""
  echo "==> Running batch evaluation ($display_str)..."
  bash "$root/career-ops/batch/batch-runner.sh" "${batch_args[@]}"
fi

