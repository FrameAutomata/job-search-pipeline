"""Desktop notifications for pipeline progress.

Cross-platform: macOS uses its native notification path (terminal-notifier if
installed, else the built-in `osascript` — no dependency to install), every
other OS uses plyer. plyer's own macOS backend requires `pyobjus`, which does
not build on current Python / Apple Silicon and raises "No usable
implementation found!", so we never route macOS through plyer.
"""

import shutil
import subprocess
import sys


def _notify_macos(title: str, message: str) -> None:
    """Fire a native macOS notification.

    Prefers terminal-notifier (nicer banners, its own app identity) when it is
    on PATH; otherwise falls back to `osascript`, which ships with every macOS
    install so no `pip`/`brew` step is required.
    """
    tn = shutil.which("terminal-notifier")
    if tn:
        subprocess.run(
            [tn, "-title", title, "-message", message],
            check=True,
            capture_output=True,
        )
        return

    def _esc(s: str) -> str:
        # Escape backslashes and quotes for the AppleScript string literal.
        return s.replace("\\", "\\\\").replace('"', '\\"')

    script = f'display notification "{_esc(message)}" with title "{_esc(title)}"'
    subprocess.run(["osascript", "-e", script], check=True, capture_output=True)


def notify(title: str, message: str) -> None:
    """Send a desktop notification.

    Args:
        title: Notification title
        message: Notification message body
    """
    try:
        if sys.platform == "darwin":
            _notify_macos(title, message)
        else:
            # Import lazily so macOS never touches plyer's broken backend.
            from plyer import notification

            notification.notify(
                title=title,
                message=message,
                timeout=10,  # Display for 10 seconds
            )
    except Exception as e:
        # Silently fail if notifications aren't available (e.g. headless environment)
        print(f"[notify] skipped: {e}")
