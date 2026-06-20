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
import time
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


def login_linkedin(*, timeout_s: int = 300) -> bool:
    """One-time bootstrap: open the LinkedIn apply browser and park for sign-in so
    the session persists in the apply profile for auto-apply. Standalone version
    of the login ensure_logged_in does lazily at --apply time. Returns whether we
    ended up signed in."""
    try:
        with launch(headless=False) as page:
            if ensure_logged_in(page, headless=False, timeout_s=timeout_s):
                print("[apply] Signed in to LinkedIn — session saved for auto-apply.", flush=True)
                return True
            print("[apply] LinkedIn sign-in not completed (timed out).", flush=True)
            return False
    except ImportError as e:  # pragma: no cover - only without Playwright installed
        print(f"[apply] {e}", flush=True)
        return False


# ── Indeed ───────────────────────────────────────────────────────────────────
# Indeed sits behind a Cloudflare anti-bot wall that flags automation; the ONLY
# proxyless way past it is a warm, real-Chrome profile that has signed in by hand
# once (which also clears Cloudflare). These mirror the LinkedIn probes; the exact
# URLs/selectors are tuned by manual --apply-mode dry-run, per this module's
# verify-manually convention.

_INDEED_AUTH_MARKERS = ("/account/login", "secure.indeed.com/auth", "/auth?", "/auth/")


def _is_cf_challenge(page) -> bool:
    """True while Cloudflare's interstitial is showing. It's served at the TARGET
    url (myjobs.indeed.com shows 'Just a moment…'), so the url alone can't tell
    'cleared' from 'still challenging' — we read the title/body."""
    try:
        if any(s in (page.title() or "").lower() for s in ("just a moment", "attention required")):
            return True
        body = (page.locator("body").inner_text(timeout=800) or "").lower()
        return any(s in body for s in ("verify you are human", "additional verification",
                                       "checking your browser"))
    except Exception:
        return False


def is_logged_in_indeed(page, *, timeout_s: int = 20) -> bool:
    """Load a member page (My Jobs) and decide whether we're signed in — redirect-
    based, so it doesn't depend on Indeed's A/B-tested DOM.

    Polls through patchright's Cloudflare clearance rather than a single fixed wait:
    the interstitial is served at the same url, so a too-short wait (the old 2s)
    could read the challenge as 'not signed in' and wrongly abort a valid run. A
    bounce to the auth flow = logged out; the member page (cleared, not the CF
    challenge) = logged in."""
    try:
        page.goto("https://myjobs.indeed.com/", wait_until="domcontentloaded")
    except Exception:
        return False
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        page.wait_for_timeout(1000)
        url = page.url.lower()
        if any(m in url for m in _INDEED_AUTH_MARKERS):
            return False          # bounced to the auth flow → logged out
        if _is_cf_challenge(page):
            continue              # patchright still clearing Cloudflare → wait
        if "myjobs.indeed.com" in url:
            return True           # cleared and on the member page → logged in
    # Timed out without a clear verdict — fall back to authenticated-only chrome.
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


# ── Indeed apply session (patchright) ──────────────────────────────────────────
# The apply browser for Indeed is patchright (stealth real Chrome), NOT the
# bundled-Chromium LinkedIn path: it's the only thing that clears Indeed's
# Cloudflare challenge (proven). The login can't be done in the automated browser
# (Google/Indeed block it), so it's captured once from a normal Chrome by
# capture_indeed_login and injected here as cookies.

def default_indeed_profile_dir() -> Path:
    env = os.environ.get("APPLY_INDEED_BROWSER_DIR")
    return Path(env) if env else ROOT / "output" / ".chrome-apply-indeed"


@contextmanager
def launch_indeed(*, headless: bool = False, user_data_dir: Path | None = None):
    """Yield a patchright (stealth) Chrome page on the persistent Indeed apply
    profile. patchright clears Cloudflare; the injected captured session keeps us
    logged in. Raises a clear ImportError if patchright isn't installed."""
    try:
        from patchright.sync_api import sync_playwright
    except ImportError as e:  # pragma: no cover - exercised only without the dep
        raise ImportError(
            "patchright is required for Indeed auto-apply. Install it locally:\n"
            "  pip install patchright"
        ) from e

    udd = Path(user_data_dir) if user_data_dir else default_indeed_profile_dir()
    udd.mkdir(parents=True, exist_ok=True)
    pw = sync_playwright().start()
    context = None
    try:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(udd), channel="chrome", headless=headless, no_viewport=True)
        page = context.pages[0] if context.pages else context.new_page()
        yield page
    finally:
        if context is not None:
            context.close()
        pw.stop()


def _system_chrome_path() -> str | None:
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
    ]
    return next((c for c in candidates if Path(c).exists()), None)


def _read_indeed_cookies_over_cdp(cdp_url: str) -> list[dict]:
    """Read all indeed.com cookies (incl. session-scoped) from a Chrome with an
    open debug port. Does NOT close the remote browser."""
    try:
        from patchright.sync_api import sync_playwright
    except ImportError:
        from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(cdp_url)
        cks: list[dict] = []
        for ctx in browser.contexts:
            cks.extend(ctx.cookies())
        return [c for c in cks if "indeed" in (c.get("domain", "") or "")]


def capture_indeed_login(*, debug_port: int = 9222, prompt_fn=input) -> int:
    """One-time bootstrap: open a NORMAL Chrome (where Indeed/Google accept the
    login), wait for the user to sign in, capture the live session cookies over
    the debug port, and inject them into the patchright apply profile so the apply
    browser is logged in. Returns the number of cookies injected.

    Login can't happen in the automated apply browser (Google/Indeed block it),
    and Indeed's auth cookie is session-scoped — so the session must be captured
    live from a normal browser, not cloned from a closed profile."""
    chrome = _system_chrome_path()
    if not chrome:
        raise RuntimeError("Google Chrome not found — install it or set the path.")
    login_dir = ROOT / "output" / ".chrome-indeed-login"
    login_dir.mkdir(parents=True, exist_ok=True)
    _kill_on_port(debug_port)
    proc = subprocess.Popen(
        [chrome, f"--remote-debugging-port={debug_port}", f"--user-data-dir={login_dir}",
         "--no-first-run", "--no-default-browser-check", "--hide-crash-restore-bubble"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(3)
        print("[apply] A Chrome window opened. Go to https://www.indeed.com and SIGN IN "
              "(Google or email both work in this normal browser).", flush=True)
        prompt_fn("[apply] Press Enter here once you are signed in to Indeed... ")
        raw = _read_indeed_cookies_over_cdp(f"http://localhost:{debug_port}")
        if not raw:
            print("[apply] no Indeed cookies captured — were you signed in?", flush=True)
            return 0
        from pipeline.apply.indeed import prepare_indeed_cookies
        cookies = prepare_indeed_cookies(raw)
        with launch_indeed(headless=True) as page:
            page.context.add_cookies(cookies)
        print(f"[apply] captured + injected {len(cookies)} Indeed cookies into the apply profile.",
              flush=True)
        return len(cookies)
    finally:
        if proc.poll() is None:
            _kill_process_tree(proc.pid)
