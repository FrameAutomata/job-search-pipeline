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

# PLAYWRIGHT_BROWSERS_PATH means something outside npm already manages the
# browsers — the Nix dev shell (flake.nix) sets it, pointing at the set that
# nixpkgs pins, because the chromium `npx playwright install` downloads is a
# generic-linux build that will not start on NixOS.
#
# Those browsers are keyed to a driver version, while career-ops asks for
# "playwright": "^1.58.1" and commits no lockfile — so the npm install above
# resolves to whatever is newest, and chromium.launch() then fails looking for
# a browser revision that is not there. Pin the npm side to the version the
# browsers actually belong to, and skip the download entirely.
if [ -n "${PLAYWRIGHT_BROWSERS_PATH:-}" ] && [ -n "${PLAYWRIGHT_DRIVER_VERSION:-}" ]; then
  echo "==> Pinning Playwright to $PLAYWRIGHT_DRIVER_VERSION (browsers supplied by PLAYWRIGHT_BROWSERS_PATH)"
  # --no-save, deliberately: career-ops is a checkout the user may pull, and a
  # modified package.json there would conflict.
  #
  # The lockfile has to go first. The npm install above just wrote one naming
  # the floated version, and npm honours a lock over the range in package.json,
  # so the next install would drag that version back. It is generated, and
  # career-ops does not track it, so dropping it costs nothing. Afterwards a
  # plain `npm install` leaves the pin alone: the pinned version still
  # satisfies package.json's range, so npm sees nothing to do.
  if ! (cd "$root/career-ops" && rm -f package-lock.json && \
        npm install --no-save --no-audit --no-fund \
          "playwright@$PLAYWRIGHT_DRIVER_VERSION"); then
    echo "    Playwright pin failed — chromium will likely not launch." >&2
    echo "    See the PLAYWRIGHT_* block in flake.nix." >&2
  fi
else
  echo "==> Installing Playwright Chromium (used by the PDF + apply skills, ~150 MB)"
  # `npx playwright install` is idempotent: it no-ops if Chromium of the same
  # version is already on disk, so re-running setup is cheap.
  if ! (cd "$root/career-ops" && npx --yes playwright install chromium); then
    echo "    Playwright Chromium install failed. Re-run later from career-ops/." >&2
  fi
fi

echo "==> Registering the Playwright MCP server with Claude Code (for the apply skill)"
if command -v claude >/dev/null 2>&1; then
  # `claude mcp add` errors if the server is already registered. That's a
  # benign re-run — log and continue rather than aborting the whole setup.
  if ! claude mcp add playwright -- npx -y @playwright/mcp@latest; then
    echo "    Playwright MCP already registered (or claude mcp add failed). Continuing." >&2
  fi
else
  echo "    'claude' CLI not found on PATH — skipping. Install Claude Code, then run:" >&2
  echo "      claude mcp add playwright -- npx -y @playwright/mcp@latest" >&2
fi

echo "==> Copying example configs"
[ -f "$root/.env" ] || cp "$root/.env.example" "$root/.env"
[ -f "$root/config/search.yml" ] || cp "$root/config/search.example.yml" "$root/config/search.yml"
mkdir -p "$root/resumes" "$root/output"

echo "==> Preparing the browser-agent handoff folder (creates it + seeds a README)"
( cd "$root" && "$root/.venv/bin/python" -m pipeline.handoff --bootstrap-dir ) \
  || echo "    handoff bootstrap skipped" >&2

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
