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

            # soffice, for --handoff-tailor's one-page resume fit and the
            # LibreOffice-gated tests in tests/test_resume_build.py. Left out
            # by default: it is a ~2 GB closure and the stage degrades
            # gracefully without it. Uncomment unless your system profile
            # already provides it.
            # libreoffice
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
          # The catch: browsers are keyed to the driver version, and career-ops
          # asks for "playwright": "^1.58.1", which floats well past what
          # nixpkgs ships. Pin the npm side to match:
          #   nix eval --raw nixpkgs#playwright-driver.version
          #   cd career-ops && npm install --save-exact playwright@<that>
          # Re-pin whenever playwright-driver moves.
          PLAYWRIGHT_BROWSERS_PATH = "${pkgs.playwright-driver.browsers}";
          PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD = "1";
          PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS = "true";

          shellHook = ''
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
