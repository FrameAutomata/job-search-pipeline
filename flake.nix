{
  description = "job-search-pipeline — Python 3.12 dev shell (NixOS)";

  # A NixOS release branch rather than nixpkgs-unstable: this shell only wants
  # a stable python312, and matching the branch the host already tracks means
  # the store paths are shared rather than duplicated.
  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-26.05";

  outputs =
    { nixpkgs, ... }:
    let
      # Linux only. The LD_LIBRARY_PATH fix below is a glibc-loader concern;
      # macOS users run setup.sh against a normal python3.12 and need none of
      # this.
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      # The triage UI as a runnable package, for hosting it as a service rather
      # than launching run-ui.sh by hand.
      #
      # This can be a plain `withPackages` where the dev shell cannot, and the
      # reason is the whole point: the UI imports NONE of the scraping stack.
      # No jobspy, so no numpy==1.26.3, so no python312 pin and no wheel-loader
      # LD_LIBRARY_PATH problem — every dependency it does have is in nixpkgs.
      # (`tests/test_app_data.py` and the digest's import guard are what keep
      # that true; a heavy import added to the UI path breaks them first.)
      #
      # It deliberately does NOT wrap run-ui.sh: that script pip-installs into
      # a .venv and is for a developer on a laptop. A service wants an argv it
      # can put in ExecStart, with the environment supplied by systemd.
      #
      #   nix run .#ui -- --host 127.0.0.1 --port 8801
      #
      # It takes uvicorn's argv and nothing else; the three environment
      # variables a hosted instance needs are the service's to set, and
      # run-ui.sh is where they are otherwise implied:
      #   UI_LAN=1          REQUIRED for any instance a person reaches over a
      #                     network. It accepts the browser's cross-origin POST
      #                     from the vhost AND is what turns the password check
      #                     on at all — omit it and every GET (`/api/jobs`,
      #                     `/api/reports/{n}`: the whole tracker and every
      #                     evaluation report) is served unauthenticated to
      #                     anyone who reaches the proxy.
      #   NOT UI_TRUST_LOOPBACK_PEER — a hosted instance behind a proxy must
      #                     leave it unset. The proxy is the TCP peer, so
      #                     setting it would hand the loopback-only routes
      #                     (reset, runs, the route that writes repository
      #                     secrets) to anyone with the password.
      #   UI_PASSWORD=...   required — the module refuses to import without it
      #   UI_ALLOWED_HOSTS  the vhost name the browser will send as Host/Origin;
      #                     unset, the server works out this machine's own
      #                     names, which will not include it
      #   CAREER_OPS_PATH   optional; defaults to ./career-ops under the cwd
      #
      # Behind a reverse proxy on the SAME host, pass
      # `--forwarded-allow-ips 127.0.0.1` (uvicorn's default, stated explicitly
      # because it is load-bearing): the proxy is the TCP peer, so without
      # X-Forwarded-For being honoured every request looks like loopback and
      # `server.py`'s loopback-only routes open to anyone who can reach the
      # proxy. The proxy must send that header — nginx's
      # `recommendedProxySettings = true` does.
      packages = forAllSystems (
        pkgs:
        let
          # One name per line of requirements-ui.txt — including the extra,
          # which nixpkgs already ships as data. Listing its six members by
          # hand (httptools, uvloop, websockets, watchfiles, python-dotenv,
          # pyyaml) was a second statement of `uvicorn[standard]` that froze
          # today's answer and made each look like an independent decision.
          # tests/test_ui_package_deps.py holds this list to that file.
          py = pkgs.python3.withPackages (
            ps:
            (with ps; [
              fastapi
              uvicorn
              markdown
              python-multipart
            ])
            ++ ps.uvicorn.optional-dependencies.standard
          );
          ui =
            pkgs.writeShellApplication {
              name = "job-search-ui";
              runtimeInputs = [
                py
                pkgs.gh
                pkgs.git
              ];
              # gh: the Refresh and Push routes shell out to it. git: the
              # self-update route. Neither is optional for a hosted instance.
              # `python -m`, not a bare `uvicorn`: it puts the working directory
              # on sys.path, which is what makes pipeline.app.server importable
              # from the checkout a service sets as its WorkingDirectory.
              text = ''
                exec python -m uvicorn pipeline.app.server:app "$@"
              '';
            };
        in
        {
          inherit ui;
          default = ui;
        }
      );

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          # python312, not pkgs.python3 (3.13 on this branch): python-jobspy
          # still pins numpy==1.26.3, which publishes no cp313 wheel. Same
          # constraint setup.sh calls out.
          #
          # Not python312.withPackages: yake and google-genai have no nixpkgs
          # attribute, so the dependency set cannot be expressed in Nix without
          # packaging them by hand. requirements.txt stays the source of truth,
          # and setup.sh / run.sh / run-ui.sh work verbatim inside this shell.
          packages = with pkgs; [
            python312
            nodejs_24 # setup-profile.mjs, career-ops npm deps
            gh # the Setup wizard pushes GitHub secrets
            git

            # bashInteractive, not the default bash: career-ops'
            # batch-runner.sh verifies a worker actually wrote its report with
            # `compgen -G`, and pkgs.bash is built --disable-progcomp, so
            # compgen is not a builtin there. It exits 127 "command not found",
            # the `[[ -z "$(compgen -G ...)" ]]` guard reads that as an empty
            # glob, and every SUCCESSFUL evaluation is recorded
            # "❌ Failed (no report file on disk)" — then retried MAX_RETRIES
            # times, tripling the spend for a result already on disk. A guard
            # written to fail closed was failing closed on completed work.
            bashInteractive

            # The agent CLIs pipeline/agent_cli.py launches — the browser agent
            # that applies for you, and `--batch`. Two of the three free entries
            # are packaged; the third is not, and that is worth writing down
            # because the obvious guess is wrong.
            #
            # Both of these LAG the versions the registry's flags were read off
            # (opencode 1.15.10 here vs 1.18.30 there; gemini-cli 0.42.0 vs
            # 0.59.0). Same major on both, so the interactive argv and the model
            # flag hold — but a pinned nixpkgs is the point, and if you need a
            # newer one, `npm install -g @google/gemini-cli` works inside this
            # shell because the shellHook sets NPM_CONFIG_PREFIX (npm's default
            # global prefix is inside the read-only store).
            #
            # gemini-cli's mainProgram is `gemini`, which is the `binary` the
            # registry names — not `gemini-cli`, so it resolves as written.
            opencode
            gemini-cli

            # `agy` is deliberately absent. `pkgs.antigravity` is NOT it: that
            # is buildVscode with executableName = "antigravity", the
            # Antigravity IDE (a VS Code fork). The `agy` CLI ships as a
            # prebuilt dynamically-linked binary from antigravity.google, which
            # will not start here without programs.nix-ld or an
            # autoPatchelfHook derivation — and note that the LD_LIBRARY_PATH
            # block below does not help with that: nix-ld is for foreign
            # EXECUTABLES, that block is for Python wheels dlopening bare
            # sonames. Its registry entry also carries no verified_against, and
            # its free tier is a small WEEKLY quota, so it is the last of the
            # three worth the effort rather than the first.

            # soffice, for --handoff-tailor's one-page resume fit and the
            # LibreOffice-gated tests in tests/test_resume_build.py. A ~2 GB
            # closure, and the stage degrades gracefully without it — but
            # --handoff-tailor is the résumé half of the apply path, so it is
            # in by default now. Drop this line if your system profile already
            # provides soffice, or if you never use that flag.
            libreoffice
          ];

          # Libraries the pip-installed manylinux wheels load at import time.
          #
          # A wheel's .so records its dependencies as bare sonames (libz.so.1),
          # which the loader resolves through the default search path — and on
          # NixOS there is no /usr/lib holding them. numpy is the one that bites
          # first, and it reports the miss as the thoroughly misleading "you
          # should not try to import numpy from its source directory"; the real
          # error is visible via
          #   ldd .venv/lib/python3.12/site-packages/numpy/core/_multiarray_umath*.so
          #
          # programs.nix-ld does NOT cover this. nix-ld replaces the
          # /lib64/ld-linux stub so foreign *executables* start, but python here
          # is a Nix build using the Nix loader, and nothing consults
          # NIX_LD_LIBRARY_PATH when it dlopens an extension module.
          LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath (
            with pkgs;
            [
              stdenv.cc.cc.lib # libstdc++.so.6 / libgcc_s — pandas, pydantic-core, tls-client
              zlib # libz.so.1 — numpy, pillow, lxml
              libxml2 # lxml, when its wheel does not vendor them
              libxslt
              openssl # cryptography, via pdfminer.six
            ]
          );

          # career-ops calls chromium.launch() with no executablePath, so it
          # takes whatever these point at. The chromium `npx playwright install`
          # downloads is a generic-linux build that will not start here, so skip
          # that download and hand it the nixpkgs browsers instead.
          #
          # The browsers are keyed to a driver version, and career-ops asks for
          # "playwright": "^1.58.1" with no committed lockfile — so a plain npm
          # install floats past whatever nixpkgs ships and chromium then
          # refuses to launch. setup.sh reconciles the two: BROWSERS_PATH is
          # its signal that the browsers are managed outside npm, and
          # DRIVER_VERSION is what it pins the npm side to. Both move together
          # on a nixpkgs bump, so there is nothing to re-pin by hand.
          PLAYWRIGHT_BROWSERS_PATH = "${pkgs.playwright-driver.browsers}";
          PLAYWRIGHT_DRIVER_VERSION = pkgs.playwright-driver.version;
          PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD = "1";
          PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS = "true";

          shellHook = ''
            # npm's default global prefix is inside the read-only store, so
            # `npm install -g` fails here. Pointing it at the user's home is
            # what makes a NEWER agent CLI than the one nixpkgs pins
            # installable from inside this shell.
            export NPM_CONFIG_PREFIX="''${NPM_CONFIG_PREFIX:-$HOME/.npm-global}"
            export PATH="$NPM_CONFIG_PREFIX/bin:$PATH"

            # Put an existing venv first, so `python` and `pytest` mean the
            # project's — whichever subdirectory the shell was entered from.
            root=$(git rev-parse --show-toplevel 2>/dev/null || echo "$PWD")
            if [ -x "$root/.venv/bin/python" ]; then
              export VIRTUAL_ENV="$root/.venv"
              export PATH="$root/.venv/bin:$PATH"
              echo "job-search-pipeline — $(python --version 2>&1) (.venv)"
            else
              echo "job-search-pipeline — no .venv yet, run ./setup.sh"
            fi
          '';
        };
      });
    };
}
