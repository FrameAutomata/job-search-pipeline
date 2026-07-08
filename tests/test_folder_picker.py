"""Native folder picker for the local onboarding UI: pick_directory() shells out
to a subprocess Tk dialog, and POST /api/onboard/pick-folder wraps it so the
wizard's "Browse…" button returns a chosen absolute path. The browser can't hand
a page a folder's real path, but the UI is local, so the server pops the dialog."""
import subprocess
from types import SimpleNamespace

import pytest

from pipeline.app import folder_picker


def _run(returncode=0, stdout=""):
    return lambda *a, **k: SimpleNamespace(returncode=returncode, stdout=stdout)


class TestPickDirectory:
    def test_picker_script_is_valid_python(self):
        # The inline Tk child only runs at Browse-time with a display, so
        # syntax-check it here — a typo would otherwise surface only as a 503.
        compile(folder_picker._PICKER, "<picker>", "exec")

    def test_returns_selected_path_stripped(self, monkeypatch):
        # askdirectory returns forward-slash paths on Windows; a trailing newline is
        # stripped and a path with a space survives.
        monkeypatch.setattr(folder_picker.subprocess, "run",
                            _run(0, "C:/Users/me/Agent Folder\n"))
        assert folder_picker.pick_directory("t") == "C:/Users/me/Agent Folder"

    def test_utf8_so_non_ascii_paths_survive(self, monkeypatch):
        # A non-cp1252 folder path (non-Latin username / emoji) must round-trip —
        # the child writes UTF-8 via PYTHONIOENCODING and the parent decodes UTF-8.
        captured = {}

        def fake_run(cmd, **kw):
            captured.update(kw)
            return SimpleNamespace(returncode=0, stdout="C:/Users/李/応募\n")

        monkeypatch.setattr(folder_picker.subprocess, "run", fake_run)
        assert folder_picker.pick_directory("t") == "C:/Users/李/応募"
        assert captured.get("encoding") == "utf-8"
        assert captured["env"]["PYTHONIOENCODING"] == "utf-8"

    def test_cancel_returns_empty_string(self, monkeypatch):
        # Cancelling the dialog prints nothing → "" (distinct from None = no picker).
        monkeypatch.setattr(folder_picker.subprocess, "run", _run(0, "\n"))
        assert folder_picker.pick_directory("t") == ""

    def test_picker_unavailable_returns_none(self, monkeypatch):
        # tkinter import failed / no display → non-zero exit → None.
        monkeypatch.setattr(folder_picker.subprocess, "run", _run(1, ""))
        assert folder_picker.pick_directory("t") is None

    def test_oserror_returns_none(self, monkeypatch):
        def boom(*a, **k):
            raise OSError("python not found")
        monkeypatch.setattr(folder_picker.subprocess, "run", boom)
        assert folder_picker.pick_directory("t") is None

    def test_timeout_returns_none(self, monkeypatch):
        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="x", timeout=1)
        monkeypatch.setattr(folder_picker.subprocess, "run", boom)
        assert folder_picker.pick_directory("t") is None


pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402
from pipeline.app import server            # noqa: E402


class TestPickFolderEndpoint:
    def _client(self):
        return TestClient(server.app)

    def test_returns_chosen_path(self, monkeypatch):
        monkeypatch.setattr("pipeline.app.folder_picker.pick_directory",
                            lambda *a, **k: "C:\\Agent Folder")
        r = self._client().post("/api/onboard/pick-folder")
        assert r.status_code == 200
        assert r.json()["path"] == "C:\\Agent Folder"

    def test_cancel_returns_empty_path(self, monkeypatch):
        monkeypatch.setattr("pipeline.app.folder_picker.pick_directory", lambda *a, **k: "")
        r = self._client().post("/api/onboard/pick-folder")
        assert r.status_code == 200
        assert r.json()["path"] == ""

    def test_503_when_no_picker_available(self, monkeypatch):
        monkeypatch.setattr("pipeline.app.folder_picker.pick_directory", lambda *a, **k: None)
        r = self._client().post("/api/onboard/pick-folder")
        assert r.status_code == 503
