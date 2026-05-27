#!/usr/bin/env bash
# Job-search-pipeline setup (macOS / Linux).
# Creates Python venv, installs deps, clones career-ops, copies example configs.

set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Creating Python venv at .venv (Python 3.12 — jobspy pins numpy==1.26.3 which has no 3.13 wheel)"
# Prefer python3.12 if available; otherwise python3 and hope for the best.
if command -v python3.12 >/dev/null 2>&1; then
  python3.12 -m venv "$root/.venv"
else
  python3 -m venv "$root/.venv"
fi
"$root/.venv/bin/python" -m pip install --upgrade pip
"$root/.venv/bin/pip" install -r "$root/requirements.txt"

echo "==> Installing local UI deps (triage + onboarding app)"
"$root/.venv/bin/pip" install -r "$root/requirements-ui.txt"

echo "==> Cloning career-ops (if missing)"
if [ ! -d "$root/career-ops" ]; then
  git clone --branch dev/batch-local-llm https://github.com/FrameAutomata/career-ops "$root/career-ops"
else
  echo "    career-ops already present, skipping clone"
fi

echo "==> Installing career-ops node deps"
(cd "$root/career-ops" && npm install)

echo "==> Installing pipeline node deps (yaml, pdf-parse)"
(cd "$root" && npm install)

echo "==> Copying example configs"
[ -f "$root/.env" ] || cp "$root/.env.example" "$root/.env"
[ -f "$root/config/search.yml" ] || cp "$root/config/search.example.yml" "$root/config/search.yml"
mkdir -p "$root/resumes" "$root/output"

echo ""
echo "==> Setup complete."
echo ""
echo "Next — finish setup in your browser:"
echo "    ./run-ui.sh        then open http://localhost:8000  and click  'Setup'"
echo ""
echo "The Setup wizard collects your resume + preferences and writes them to your"
echo "private repo's GitHub secrets, so the pipeline can run in the cloud."
echo "It needs the GitHub CLI: install gh (https://cli.github.com), then 'gh auth login'."
echo ""
echo "Prefer the terminal instead? Run:  node setup-profile.mjs"
