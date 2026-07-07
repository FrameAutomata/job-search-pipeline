"""Local-config endpoint: writing eval/tailoring provider config + API keys to
the local .env (never to profile.yml or GitHub Secrets), with immediate
os.environ effect so the running server picks changes up without a restart."""
import importlib
import os

import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A reloaded server whose ROOT (and thus .env) points at a temp dir, with a
    clean provider env so each test starts from a known, isolated state.

    save_local_config mutates os.environ directly (so changes take effect without a
    restart) — and monkeypatch can't undo a write to a key that started absent. So
    snapshot the whole environ here and restore it on teardown, otherwise written
    config leaks into later tests."""
    co = tmp_path / "career-ops"
    (co / "data").mkdir(parents=True)
    monkeypatch.setenv("CAREER_OPS_PATH", str(co))
    from pipeline.app import server
    importlib.reload(server)                         # re-runs load_dotenv(real .env)...
    monkeypatch.setattr(server, "ROOT", tmp_path)    # .env writes land in tmp_path/.env
    for k in ("TAILOR_PROVIDER", "TAILOR_MODEL"):
        monkeypatch.delenv(k, raising=False)         # ...so clear the real models it pulled in
    snapshot = dict(os.environ)
    yield TestClient(server.app), tmp_path, server
    os.environ.clear()
    os.environ.update(snapshot)


def _post(client, **fields):
    return client.post("/api/onboard/local-config", json=fields)


class TestEvalProviderKey:
    """The eval provider's own API key goes to .env via the local-config endpoint —
    the path the Local-settings panel uses to add e.g. DEEPSEEK_API_KEY."""

    def test_writes_eval_provider_api_key(self, env):
        client, root, server = env
        r = _post(client, batch_provider="deepseek", api_key="sk-ds-eval")
        assert r.status_code == 200
        assert "DEEPSEEK_API_KEY=sk-ds-eval" in (root / ".env").read_text(encoding="utf-8")

    def test_api_key_without_provider_is_rejected_not_dropped(self, env):
        # Review bug: with the provider select on "auto-detect" (value ""), a
        # pasted key returned {"ok": true} while silently writing nothing.
        client, root, server = env
        r = _post(client, batch_provider="", api_key="sk-orphan")
        assert r.status_code == 400
        assert "provider" in r.json()["detail"].lower()
        # Rejected before any write — .env either untouched or never created.
        env_file = root / ".env"
        assert not env_file.exists() or "sk-orphan" not in env_file.read_text(encoding="utf-8")

    def test_tailor_key_without_tailor_provider_is_rejected(self, env):
        client, root, server = env
        r = _post(client, tailor_provider="", tailor_api_key="sk-orphan-tailor")
        assert r.status_code == 400
        env_file = root / ".env"
        assert not env_file.exists() or "sk-orphan-tailor" not in env_file.read_text(encoding="utf-8")


class TestTailorConfig:
    """Separate evaluation vs tailoring models/providers, set from the UI: writes
    TAILOR_PROVIDER/TAILOR_MODEL (+ the tailor provider's key) to .env; the config
    GET reports them so the form can pre-fill."""

    def test_writes_tailor_provider_and_model(self, env):
        client, root, server = env
        r = _post(client, tailor_provider="anthropic", tailor_model="claude-x")
        assert r.status_code == 200
        envtext = (root / ".env").read_text(encoding="utf-8")
        assert "TAILOR_PROVIDER=anthropic" in envtext
        assert "TAILOR_MODEL=claude-x" in envtext

    def test_writes_tailor_provider_api_key(self, env):
        client, root, server = env
        _post(client, tailor_provider="anthropic", tailor_api_key="sk-ant-xyz")
        envtext = (root / ".env").read_text(encoding="utf-8")
        assert "ANTHROPIC_API_KEY=sk-ant-xyz" in envtext

    def test_get_reports_tailor_config(self, env, monkeypatch):
        client, root, server = env
        monkeypatch.setenv("TAILOR_PROVIDER", "anthropic")
        monkeypatch.setenv("TAILOR_MODEL", "claude-x")
        cur = client.get("/api/onboard/providers").json()["current"]
        assert cur["tailor_provider"] == "anthropic"
        assert cur["tailor_model"] == "claude-x"


class TestHandoffOutDir:
    """The browser-agent handoff directory is configured from the wizard (not a
    hand-edited .env): the endpoint writes HANDOFF_OUT_DIR and bootstraps the dir
    (creates it + seeds the agent README)."""

    def test_writes_handoff_out_dir_and_seeds_readme(self, env):
        client, root, server = env
        target = root / "AutoApply Home Folder"      # a path WITH a space (the real case)
        r = _post(client, handoff_out_dir=str(target))
        assert r.status_code == 200
        # Round-trips through .env intact (space and all).
        assert dotenv_values(root / ".env")["HANDOFF_OUT_DIR"] == str(target)
        assert os.environ["HANDOFF_OUT_DIR"] == str(target)   # live, no restart
        # The directory is created + seeded with the agent instructions README.
        from pipeline import handoff
        assert (target / handoff.HANDOFF_README).exists()

    def test_blank_clears_handoff_out_dir(self, env):
        client, root, server = env
        _post(client, handoff_out_dir=str(root / "agent"))
        _post(client, handoff_out_dir="")                     # clear it
        assert "HANDOFF_OUT_DIR" not in dotenv_values(root / ".env")
        assert "HANDOFF_OUT_DIR" not in os.environ

    def test_get_reports_handoff_out_dir(self, env, monkeypatch):
        client, root, server = env
        monkeypatch.setenv("HANDOFF_OUT_DIR", r"C:\agent\home")
        cur = client.get("/api/onboard/providers").json()["current"]
        assert cur["handoff_out_dir"] == r"C:\agent\home"

    def test_uncreatable_handoff_dir_does_not_500(self, env):
        # A path that can't be created (here: a file, not a dir) must not 500 and
        # must still persist to .env — the seed is best-effort.
        client, root, server = env
        badfile = root / "not-a-dir"
        badfile.write_text("x", encoding="utf-8")
        r = _post(client, handoff_out_dir=str(badfile))
        assert r.status_code == 200
        assert r.json().get("warning")                                # reported, not swallowed
        assert dotenv_values(root / ".env")["HANDOFF_OUT_DIR"] == str(badfile)
