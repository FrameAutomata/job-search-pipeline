"""Chrome/Playwright session for the apply engine.

Auto-apply needs a real, logged-in browser session — so this is inherently a
local capability (the cloud pipeline never applies). We launch a *persistent*
context against a dedicated user-data dir so the LinkedIn login survives between
runs: the user signs in once, by hand, and subsequent runs reuse the cookies.

Playwright is imported lazily so the pure modules (queue, answers, profile,
result) — and their tests — don't require it to be installed."""

from __future__ import annotations

import os
import platform
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

# Persistent profile dir. Lives under the gitignored output/ tree so the login
# session is never committed. Override with APPLY_BROWSER_DIR.
def default_user_data_dir() -> Path:
    env = os.environ.get("APPLY_BROWSER_DIR")
    return Path(env) if env else ROOT / "output" / ".chrome-apply"


_LAUNCH_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-blink-features=AutomationControlled",
    "--disable-notifications",
    "--deny-permission-prompts",
    # After a crash / power loss Chrome shows a "didn't shut down correctly"
    # restore bubble that can sit over the page; suppress it.
    "--hide-crash-restore-bubble",
]


def _kill_process_tree(pid: int) -> None:
    """Kill a process and its children (Chrome spawns many). Best-effort."""
    try:
        if platform.system() == "Windows":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        else:
            import signal as _signal
            try:
                os.killpg(os.getpgid(pid), _signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    os.kill(pid, _signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
    except Exception:
        pass


def _kill_on_port(port: int) -> None:
    """Evict any process listening on `port` before we open it. A stale Chrome
    from a crashed run holds the CDP port; a fresh launch can't rebind it, so the
    agent's MCP (and connect_over_cdp) would silently attach to the OLD browser
    instead of the warm, logged-in one. Best-effort — a missing netstat/lsof is
    fine (we just skip the sweep)."""
    try:
        if platform.system() == "Windows":
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                                 capture_output=True, text=True, timeout=10).stdout
            for line in out.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    pid = line.split()[-1]
                    if pid.isdigit():
                        _kill_process_tree(int(pid))
        else:
            out = subprocess.run(["lsof", "-ti", f":{port}"],
                                 capture_output=True, text=True, timeout=10).stdout
            for pid in out.split():
                if pid.isdigit():
                    _kill_process_tree(int(pid))
    except Exception:
        pass


@dataclass
class Session:
    """A live apply browser session. `page` drives the deterministic engines;
    `cdp_endpoint` (http://localhost:{port} or None) is where the agentic engine's
    `@playwright/mcp` attaches to the SAME Chrome, so both share one warm, logged-in,
    Cloudflare-cleared session."""
    page: object
    cdp_endpoint: str | None


@contextmanager
def launch_session(*, headless: bool = False, user_data_dir: Path | None = None,
                   cdp_port: int | None = None, channel: str | None = "chrome"):
    """Yield a `Session` backed by a persistent context.

    Defaults to REAL Chrome (`channel="chrome"`): Indeed's Cloudflare wall flags
    Playwright's bundled Chromium, but a real Chrome on a warm, logged-in profile
    clears it — so the apply/agentic path needs the real binary. Pass
    `cdp_port` to open a CDP endpoint an external MCP/agent can attach to.
    `channel=None` falls back to bundled Chromium (the LinkedIn-only deterministic
    path, which never meets Cloudflare).

    Raises a clear ImportError if Playwright isn't installed (optional local dep)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:  # pragma: no cover - exercised only without the dep
        raise ImportError(
            "Playwright is required for auto-apply. Install it locally:\n"
            "  pip install playwright && playwright install chromium"
        ) from e

    udd = Path(user_data_dir) if user_data_dir else default_user_data_dir()
    udd.mkdir(parents=True, exist_ok=True)

    args = list(_LAUNCH_ARGS)
    # Skip image loading by default — job pages are image-heavy and we don't need
    # them to fill a form. Re-enable with APPLY_LOAD_IMAGES=true.
    if os.environ.get("APPLY_LOAD_IMAGES", "").strip().lower() not in ("1", "true", "yes"):
        args.append("--blink-settings=imagesEnabled=false")
    endpoint = None
    if cdp_port:
        _kill_on_port(cdp_port)  # evict a stale Chrome holding the port (see _kill_on_port)
        args.append(f"--remote-debugging-port={cdp_port}")
        endpoint = f"http://localhost:{cdp_port}"

    launch_kwargs: dict = dict(user_data_dir=str(udd), headless=headless, args=args,
                               viewport={"width": 1280, "height": 900})
    if channel:
        launch_kwargs["channel"] = channel  # real Chrome (vs bundled Chromium)

    pw = sync_playwright().start()
    context = None
    try:
        # Inside the try so a launch failure (e.g. channel="chrome" with no Chrome
        # installed) still runs pw.stop() and doesn't leak the driver process.
        context = pw.chromium.launch_persistent_context(**launch_kwargs)
        page = context.pages[0] if context.pages else context.new_page()
        yield Session(page=page, cdp_endpoint=endpoint)
    finally:
        if context is not None:
            context.close()
        pw.stop()


@contextmanager
def launch(headless: bool = False, user_data_dir: Path | None = None):
    """Back-compat: yield just the page, on bundled Chromium with no CDP port — the
    existing LinkedIn deterministic path (never meets Cloudflare), unchanged."""
    with launch_session(headless=headless, user_data_dir=user_data_dir,
                        cdp_port=None, channel=None) as session:
        yield session.page


def is_logged_in(page) -> bool:
    """LinkedIn login check: load the feed and look for AUTHENTICATED-only chrome.

    Important: the bare global nav (#global-nav) renders logged-out too (it holds
    the Sign in / Join buttons), so we must NOT key on it — doing so lets a guest
    session pass as logged in, after which every job page is a guest view with no
    Easy Apply button. The global search box and the 'Me' avatar menu only exist
    once signed in."""
    page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
    url = page.url.lower()
    # Primary signal — redirect, not DOM: a logged-out session bounces /feed to
    # authwall/login/signup (or the public homepage); a logged-in one stays on
    # /feed. This is layout-independent, unlike element selectors that LinkedIn
    # A/B-tests and renames.
    if any(x in url for x in ("/login", "/uas/login", "/checkpoint", "/authwall", "/signup")):
        return False
    if "/feed" in url:
        return True
    # Ambiguous URL (rare) — fall back to authenticated-only chrome.
    try:
        return page.locator(
            "input.search-global-typeahead__input, .global-nav__me, "
            "img.global-nav__me-photo, button[aria-label*='Me' i]"
        ).count() > 0
    except Exception:
        return False


def ensure_logged_in(page, *, headless: bool, timeout_s: int = 240) -> bool:
    """Make sure we have a logged-in LinkedIn session.

    If already logged in → True. If not and we're headed (visible window), park
    on the login page and poll until the user signs in (or the timeout). If not
    and headless → False: you can't complete a login flow without a window, so
    the caller should abort with a clear message."""
    if is_logged_in(page):
        return True
    if headless:
        return False

    import time
    page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
    print(
        "[apply] Not signed in to LinkedIn. A browser window is open — log in "
        f"there. Waiting up to {timeout_s}s...",
        flush=True,
    )
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(3)
        cur = page.url.lower()
        if any(x in cur for x in ("/login", "/uas/login", "/checkpoint", "/authwall", "/signup")):
            continue  # still on a login / challenge page
        # Landed on any authenticated section → signed in (layout-independent).
        if any(s in cur for s in ("/feed", "/jobs", "/in/", "/mynetwork", "/messaging")):
            return True
        try:
            if page.locator("input.search-global-typeahead__input, .global-nav__me, "
                            "img.global-nav__me-photo").count() > 0:
                return True
        except Exception:
            pass
    return is_logged_in(page)


# ── Indeed ───────────────────────────────────────────────────────────────────
# Indeed sits behind a Cloudflare anti-bot wall that flags automation; the ONLY
# proxyless way past it is a warm, real-Chrome profile that has signed in by hand
# once (which also clears Cloudflare). These mirror the LinkedIn probes; the exact
# URLs/selectors are tuned by manual --apply-mode dry-run, per this module's
# verify-manually convention.

_INDEED_AUTH_MARKERS = ("/account/login", "secure.indeed.com/auth", "/auth?", "/auth/")


def is_logged_in_indeed(page) -> bool:
    """Load a member-only page (My Jobs) and see whether it bounces to the auth
    flow — redirect-based, like the LinkedIn probe, so it doesn't depend on
    Indeed's A/B-tested DOM. NOTE: on a COLD profile the navigation itself hits
    Cloudflare, so this only reads true once the profile is warm (signed in once
    via ensure_logged_in_indeed)."""
    try:
        page.goto("https://myjobs.indeed.com/", wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
    except Exception:
        return False
    url = page.url.lower()
    if any(m in url for m in _INDEED_AUTH_MARKERS):
        return False
    if "myjobs.indeed.com" in url:
        return True
    try:
        return page.locator("[data-gnav-element-name='AccountMenu'], "
                            "[aria-label*='Account' i], a[href*='/account']").count() > 0
    except Exception:
        return False


def ensure_logged_in_indeed(page, *, headless: bool, timeout_s: int = 300) -> bool:
    """Ensure a logged-in Indeed session in the warm real-Chrome profile.

    The one-time bootstrap that matters for Indeed: signing in by hand in a
    visible window ALSO clears Cloudflare's challenge (which automation can't),
    and the session + cf_clearance cookies persist in the profile for later runs.
    Headless can't complete it → returns False so the caller aborts clearly."""
    if is_logged_in_indeed(page):
        return True
    if headless:
        return False

    import time
    try:
        page.goto("https://secure.indeed.com/auth", wait_until="domcontentloaded")
    except Exception:
        pass
    print(
        "[apply] Not signed in to Indeed. A browser window is open — sign in there "
        f"(this also clears Cloudflare). Waiting up to {timeout_s}s...",
        flush=True,
    )
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(3)
        cur = page.url.lower()
        if any(m in cur for m in _INDEED_AUTH_MARKERS):
            continue  # still on the auth / challenge flow
        # Left the auth flow and landed on indeed.com → signed in.
        if "indeed.com" in cur:
            return True
    return is_logged_in_indeed(page)
