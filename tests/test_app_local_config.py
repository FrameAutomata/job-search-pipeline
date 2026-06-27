"""Local-config endpoint: writing off-site ATS account credentials to .env.

These are LOCAL-ONLY secrets (auto-apply never runs in the cloud), so the UI
setup writes them to the local .env via the same dotenv path as the provider
keys — never to profile.yml or GitHub Secrets. The config GET reports only
whether a password is set (a boolean), never the secret value itself, so the
browser never receives the passwords back.
"""
import importlib
import os

import pytest
from fastapi.testclient import TestClient

_ATS_KEYS = ("APPLY_ATS_EMAIL", "APPLY_ATS_PASSWORD", "APPLY_IMAP_HOST",
            "APPLY_IMAP_PORT", "APPLY_IMAP_PASSWORD")


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A reloaded server whose ROOT (and thus .env) points at a temp dir, with a
    clean ATS env so each test starts from a known, isolated state.

    save_local_config mutates os.environ directly (so changes take effect without a
    restart) — and monkeypatch can't undo a write to a key that started absent. So
    snapshot the whole environ here and restore it on teardown, otherwise written
    ATS creds leak into later tests (e.g. the profile default-email test)."""
    co = tmp_path / "career-ops"
    (co / "data").mkdir(parents=True)
    monkeypatch.setenv("CAREER_OPS_PATH", str(co))
    from pipeline.app import server
    importlib.reload(server)                         # re-runs load_dotenv(real .env)...
    monkeypatch.setattr(server, "ROOT", tmp_path)    # .env writes land in tmp_path/.env
    for k in _ATS_KEYS:
        monkeypatch.delenv(k, raising=False)         # ...so clear the real creds it pulled in
    snapshot = dict(os.environ)
    yield TestClient(server.app), tmp_path, server
    os.environ.clear()
    os.environ.update(snapshot)


def _post(client, **fields):
    return client.post("/api/onboard/local-config", json=fields)


class TestWriteAtsCreds:
    def test_post_writes_all_ats_creds_to_env(self, env):
        client, root, server = env
        r = _post(client, ats_email="apply@example.com", ats_password="s3cret-pw",
                  imap_host="imap.gmail.com", imap_port="993",
                  imap_password="abcd efgh ijkl mnop")
        assert r.status_code == 200
        envtext = (root / ".env").read_text(encoding="utf-8")
        assert "APPLY_ATS_EMAIL=apply@example.com" in envtext
        assert "APPLY_ATS_PASSWORD=s3cret-pw" in envtext
        assert "APPLY_IMAP_HOST=imap.gmail.com" in envtext
        assert "APPLY_IMAP_PORT=993" in envtext
        assert "APPLY_IMAP_PASSWORD=abcd efgh ijkl mnop" in envtext
        # and visible to the running server immediately
        import os
        assert os.environ["APPLY_ATS_PASSWORD"] == "s3cret-pw"

    def test_blank_password_preserves_existing(self, env):
        client, root, server = env
        _post(client, ats_email="apply@example.com", ats_password="keep-me",
              imap_host="imap.gmail.com", imap_password="keep-imap")
        # A later settings save that doesn't re-enter the passwords must NOT wipe
        # them (the form never receives the existing secret to echo back).
        r = _post(client, ats_email="apply@example.com", ats_password="",
                  imap_host="imap.gmail.com", imap_password="")
        assert r.status_code == 200
        envtext = (root / ".env").read_text(encoding="utf-8")
        assert "APPLY_ATS_PASSWORD=keep-me" in envtext
        assert "APPLY_IMAP_PASSWORD=keep-imap" in envtext

    def test_blank_port_not_written(self, env):
        # No port -> the profile loader defaults it to 993; don't write an empty one.
        client, root, server = env
        _post(client, ats_email="a@b.com", ats_password="pw",
              imap_host="imap.gmail.com", imap_port="", imap_password="ip")
        envtext = (root / ".env").read_text(encoding="utf-8")
        assert "APPLY_IMAP_PORT" not in envtext


class TestConfigGetReportsStatus:
    def test_get_reports_ats_status_without_leaking_secrets(self, env, monkeypatch):
        client, root, server = env
        monkeypatch.setenv("APPLY_ATS_EMAIL", "apply@example.com")
        monkeypatch.setenv("APPLY_ATS_PASSWORD", "TOP-SECRET-PW")
        monkeypatch.setenv("APPLY_IMAP_HOST", "imap.gmail.com")
        monkeypatch.setenv("APPLY_IMAP_PASSWORD", "TOP-SECRET-IMAP")
        body = client.get("/api/onboard/providers").json()
        cur = body["current"]
        assert cur["ats_email"] == "apply@example.com"
        assert cur["imap_host"] == "imap.gmail.com"
        assert cur["ats_password_set"] is True
        assert cur["imap_password_set"] is True
        # the actual passwords must never be echoed back to the browser
        import json
        assert "TOP-SECRET-PW" not in json.dumps(body)
        assert "TOP-SECRET-IMAP" not in json.dumps(body)

    def test_get_reports_unset_when_absent(self, env):
        client, root, server = env
        cur = client.get("/api/onboard/providers").json()["current"]
        assert cur["ats_password_set"] is False
        assert cur["imap_password_set"] is False
        assert cur["ats_email"] == ""
