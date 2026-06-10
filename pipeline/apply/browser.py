"""Chrome/Playwright session for the apply engine.

Auto-apply needs a real, logged-in browser session — so this is inherently a
local capability (the cloud pipeline never applies). We launch a *persistent*
context against a dedicated user-data dir so the LinkedIn login survives between
runs: the user signs in once, by hand, and subsequent runs reuse the cookies.

Playwright is imported lazily so the pure modules (queue, answers, profile,
result) — and their tests — don't require it to be installed."""

from __future__ import annotations

import os
from contextlib import contextmanager
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


@contextmanager
def launch(headless: bool = False, user_data_dir: Path | None = None):
    """Yield a Playwright page backed by a persistent context.

    Raises a clear ImportError if Playwright isn't installed (it's an optional,
    local-only dependency — see requirements.txt)."""
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
    # Skip image loading by default — LinkedIn job pages are image-heavy and we
    # don't need them to fill a form, so this noticeably speeds up the initial
    # page load and the modal render. Re-enable with APPLY_LOAD_IMAGES=true.
    if os.environ.get("APPLY_LOAD_IMAGES", "").strip().lower() not in ("1", "true", "yes"):
        args.append("--blink-settings=imagesEnabled=false")

    pw = sync_playwright().start()
    context = pw.chromium.launch_persistent_context(
        user_data_dir=str(udd),
        headless=headless,
        args=args,
        viewport={"width": 1280, "height": 900},
    )
    try:
        page = context.pages[0] if context.pages else context.new_page()
        yield page
    finally:
        context.close()
        pw.stop()


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
