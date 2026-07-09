"""Search-config resolution: local override vs. the cloud-shared search.yml.

A local run should auto-prefer config/search.local.yml when it exists, so a user
can search different terms locally than the cloud daily runs — without touching
the SEARCH_CONFIG_B64 secret that drives the cloud (which only ever lands in
config/search.yml). Explicit --config and the SEARCH_CONFIG env var still win, so
the cloud (and any deliberate override) is unaffected.
"""

from pathlib import Path

import pytest

import orchestrate


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    """A tmp repo root with a config/ dir holding the shared search.yml."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "search.yml").write_text("searches: []\n", encoding="utf-8")
    monkeypatch.delenv("SEARCH_CONFIG", raising=False)
    return tmp_path


def test_prefers_local_override_when_present(config_dir):
    local = config_dir / "config" / "search.local.yml"
    local.write_text("searches: []\n", encoding="utf-8")
    assert orchestrate.resolve_search_config(None, root=config_dir) == local.resolve()


def test_falls_back_to_shared_when_no_local(config_dir):
    resolved = orchestrate.resolve_search_config(None, root=config_dir)
    assert resolved == (config_dir / "config" / "search.yml").resolve()


def test_explicit_config_beats_local_override(config_dir):
    (config_dir / "config" / "search.local.yml").write_text("searches: []\n", encoding="utf-8")
    explicit = config_dir / "config" / "search.yml"
    assert orchestrate.resolve_search_config(explicit, root=config_dir) == explicit.resolve()


def test_custom_env_var_beats_local_override(config_dir, monkeypatch):
    (config_dir / "config" / "search.local.yml").write_text("searches: []\n", encoding="utf-8")
    (config_dir / "config" / "custom.yml").write_text("searches: []\n", encoding="utf-8")
    monkeypatch.setenv("SEARCH_CONFIG", "config/custom.yml")
    resolved = orchestrate.resolve_search_config(None, root=config_dir)
    assert resolved == (config_dir / "config" / "custom.yml").resolve()


def test_boilerplate_env_pointing_at_shared_does_not_defeat_override(config_dir, monkeypatch):
    """.env.example ships SEARCH_CONFIG=./config/search.yml — the shared default.
    That must be treated as unset so the local override still wins; otherwise the
    feature is dead for every user whose .env carries the boilerplate line."""
    local = config_dir / "config" / "search.local.yml"
    local.write_text("searches: []\n", encoding="utf-8")
    monkeypatch.setenv("SEARCH_CONFIG", "./config/search.yml")
    assert orchestrate.resolve_search_config(None, root=config_dir) == local.resolve()
