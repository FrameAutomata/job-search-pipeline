"""Desktop notifications for pipeline progress."""

from plyer import notification


def notify(title: str, message: str) -> None:
    """Send a desktop notification.

    Args:
        title: Notification title
        message: Notification message body
    """
    try:
        notification.notify(
            title=title,
            message=message,
            timeout=10,  # Display for 10 seconds
        )
    except Exception as e:
        # Silently fail if notifications aren't available (e.g. headless environment)
        print(f"[notify] skipped: {e}")
