"""Tests for pipeline/notify.py — cross-platform notification routing."""

from pipeline import notify as notify_mod


class TestNotifyMacOS:
    """macOS routes through osascript / terminal-notifier, never plyer."""

    def test_uses_osascript_when_no_terminal_notifier(self, monkeypatch):
        """Without terminal-notifier on PATH, fall back to built-in osascript."""
        monkeypatch.setattr(notify_mod.sys, "platform", "darwin")
        monkeypatch.setattr(notify_mod.shutil, "which", lambda _: None)
        calls = []
        monkeypatch.setattr(notify_mod.subprocess, "run", lambda *a, **k: calls.append(a[0]))

        notify_mod.notify("Pipeline", "Scraping complete")

        assert len(calls) == 1
        cmd = calls[0]
        assert cmd[0] == "osascript"
        script = cmd[2]
        assert "Scraping complete" in script
        assert "Pipeline" in script

    def test_prefers_terminal_notifier_when_present(self, monkeypatch):
        """terminal-notifier is used when it is on PATH."""
        monkeypatch.setattr(notify_mod.sys, "platform", "darwin")
        monkeypatch.setattr(notify_mod.shutil, "which", lambda _: "/opt/homebrew/bin/terminal-notifier")
        calls = []
        monkeypatch.setattr(notify_mod.subprocess, "run", lambda *a, **k: calls.append(a[0]))

        notify_mod.notify("Pipeline", "Done")

        cmd = calls[0]
        assert cmd[0] == "/opt/homebrew/bin/terminal-notifier"
        assert "Done" in cmd

    def test_escapes_quotes_in_osascript(self, monkeypatch):
        """A double quote in the message must not break the AppleScript string."""
        monkeypatch.setattr(notify_mod.sys, "platform", "darwin")
        monkeypatch.setattr(notify_mod.shutil, "which", lambda _: None)
        calls = []
        monkeypatch.setattr(notify_mod.subprocess, "run", lambda *a, **k: calls.append(a[0]))

        notify_mod.notify("Pipeline", 'found "5" offers')

        assert '\\"5\\"' in calls[0][2]

    def test_failure_is_swallowed(self, monkeypatch, capsys):
        """A failing backend degrades gracefully with a skip message."""
        monkeypatch.setattr(notify_mod.sys, "platform", "darwin")
        monkeypatch.setattr(notify_mod.shutil, "which", lambda _: None)

        def boom(*a, **k):
            raise RuntimeError("nope")

        monkeypatch.setattr(notify_mod.subprocess, "run", boom)

        notify_mod.notify("Pipeline", "x")  # must not raise

        assert "[notify] skipped" in capsys.readouterr().out
