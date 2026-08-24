"""Route tests for the local search-config override (config/search.local.yml).

The UI panel lets a user keep a full standalone search config for LOCAL runs that
diverges from the cloud-shared config/search.yml (the SEARCH_CONFIG_B64 secret).
These pin the read/write/delete contract and the validation that keeps a bad
paste from landing an unparseable file the next run would choke on.

Skips if FastAPI isn't installed (optional UI dependency)."""

import importlib

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A TestClient whose server ROOT is a tmp repo with config/search.yml."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "search.yml").write_text(
        "searches:\n  - name: cloud pass\n    search_terms: [python]\n", encoding="utf-8"
    )
    career_ops = tmp_path / "career-ops"
    (career_ops / "data").mkdir(parents=True)
    monkeypatch.setenv("CAREER_OPS_PATH", str(career_ops))

    from pipeline.app import server
    importlib.reload(server)
    monkeypatch.setattr(server, "ROOT", tmp_path)
    return TestClient(server.app), tmp_path


def _local_file(root):
    return root / "config" / "search.local.yml"


def test_get_seeds_from_shared_when_no_override(client):
    c, root = client
    r = c.get("/api/local-search")
    assert r.status_code == 200
    body = r.json()
    assert body["active"] is False
    assert "cloud pass" in body["content"]        # seeded from search.yml
    assert not _local_file(root).exists()          # GET must not create it


def test_get_returns_override_when_present(client):
    c, root = client
    _local_file(root).write_text(
        "searches:\n  - name: local pass\n    search_terms: [rust]\n", encoding="utf-8"
    )
    body = c.get("/api/local-search").json()
    assert body["active"] is True
    assert "local pass" in body["content"]


def test_post_writes_valid_override(client):
    c, root = client
    content = "searches:\n  - name: local pass\n    search_terms: [go]\n"
    r = c.post("/api/local-search", json={"content": content})
    assert r.status_code == 200
    assert r.json()["active"] is True
    assert _local_file(root).read_text(encoding="utf-8") == content
    # GET now reflects the override
    assert "local pass" in c.get("/api/local-search").json()["content"]


def test_post_accepts_legacy_search_form(client):
    # load_searches (the runtime) accepts a single `search:` mapping; the UI
    # validator must too, else a valid cloud config is rejected on save.
    c, root = client
    r = c.post("/api/local-search", json={"content": "search:\n  search_terms: [go]\n"})
    assert r.status_code == 200
    assert _local_file(root).exists()


def test_post_rejects_scalar_search_entry(client):
    # `search: python` loads, but the pipeline reads dict fields off it and would
    # crash — the validator must reject it, not "accept then choke on next run".
    c, root = client
    r = c.post("/api/local-search", json={"content": "search: python\n"})
    assert r.status_code == 400
    assert not _local_file(root).exists()


def test_post_rejects_scalar_in_searches_list(client):
    c, root = client
    r = c.post("/api/local-search", json={"content": "searches: [python, rust]\n"})
    assert r.status_code == 400
    assert not _local_file(root).exists()


def test_post_rejects_null_search(client):
    c, root = client
    r = c.post("/api/local-search", json={"content": "search:\n"})
    assert r.status_code == 400
    assert not _local_file(root).exists()


def test_post_rejects_empty_searches_list(client):
    # An empty override silently scrapes nothing every run — reject so the user
    # gets a clear error instead of a mystery zero-result pipeline.
    c, root = client
    r = c.post("/api/local-search", json={"content": "searches: []\n"})
    assert r.status_code == 400
    assert not _local_file(root).exists()


def test_post_rejects_unparseable_yaml(client):
    c, root = client
    r = c.post("/api/local-search", json={"content": "searches: [unterminated\n"})
    assert r.status_code == 400
    assert not _local_file(root).exists()


def test_post_rejects_missing_searches_list(client):
    c, root = client
    r = c.post("/api/local-search", json={"content": "filter:\n  min_score: 5\n"})
    assert r.status_code == 400
    assert not _local_file(root).exists()


def test_delete_reverts_to_shared(client):
    c, root = client
    _local_file(root).write_text("searches: []\n", encoding="utf-8")
    r = c.delete("/api/local-search")
    assert r.status_code == 200
    assert r.json()["active"] is False
    assert not _local_file(root).exists()
    # and a GET now re-seeds from the shared config
    assert "cloud pass" in c.get("/api/local-search").json()["content"]


def test_delete_is_idempotent_when_absent(client):
    c, root = client
    r = c.delete("/api/local-search")
    assert r.status_code == 200
    assert r.json()["active"] is False


# ── supported-board validation ──────────────────────────────────────────────
# A config listing only retired boards parses fine and loads fine — it just
# scrapes nothing, silently. These pin the save-time feedback.


def test_post_rejects_config_that_would_scrape_nothing(client):
    c, root = client
    content = "searches:\n  - name: dead\n    search_terms: [go]\n    sites: [glassdoor, google]\n"
    r = c.post("/api/local-search", json={"content": content})
    assert r.status_code == 400
    assert "supported board" in r.json()["detail"]
    assert not _local_file(root).exists()      # nothing written on reject


def test_post_saves_with_warning_when_some_boards_unsupported(client):
    c, root = client
    content = ("searches:\n  - name: mixed\n    search_terms: [go]\n"
               "    sites: [indeed, glassdoor]\n")
    r = c.post("/api/local-search", json={"content": content})
    assert r.status_code == 200
    body = r.json()
    assert body["active"] is True
    assert "glassdoor" in body["warning"]
    assert _local_file(root).read_text(encoding="utf-8") == content


def test_post_has_no_warning_when_every_board_supported(client):
    c, root = client
    content = "searches:\n  - name: ok\n    search_terms: [go]\n    sites: [indeed, linkedin]\n"
    r = c.post("/api/local-search", json={"content": content})
    assert r.status_code == 200
    assert r.json()["warning"] is None


def test_post_accepts_pass_without_sites_key(client):
    # No `sites` key inherits the supported boards at scrape time, so it is
    # survivable and must not be rejected as scraping nothing.
    c, root = client
    r = c.post("/api/local-search",
               json={"content": "searches:\n  - name: p\n    search_terms: [go]\n"})
    assert r.status_code == 200
    assert r.json()["warning"] is None


def test_post_survives_one_good_pass_among_dead_ones(client):
    c, root = client
    content = ("searches:\n"
               "  - name: dead\n    search_terms: [go]\n    sites: [glassdoor]\n"
               "  - name: live\n    search_terms: [go]\n    sites: [indeed]\n")
    r = c.post("/api/local-search", json={"content": content})
    assert r.status_code == 200
    assert "glassdoor" in r.json()["warning"]


def test_post_warns_when_a_pass_has_an_empty_sites_list(client):
    # `sites: []` names no unsupported board, so it contributed nothing to the
    # dropped-board list and saved with no warning at all — then vanished at run
    # time. The user must be told the pass will be skipped.
    c, root = client
    content = ("searches:\n"
               "  - name: empty\n    search_terms: [go]\n    sites: []\n"
               "  - name: live\n    search_terms: [go]\n    sites: [indeed]\n")
    r = c.post("/api/local-search", json={"content": content})
    assert r.status_code == 200
    warning = r.json()["warning"]
    assert "empty" in warning and "skipped" in warning


def test_post_accepts_comma_separated_scalar_sites(client):
    # The CLI wizard prompts comma-separated; unbracketed this is one YAML
    # string that matched no board, so a config naming both supported boards
    # was rejected as scraping nothing.
    c, root = client
    r = c.post("/api/local-search", json={
        "content": "searches:\n  - name: p\n    search_terms: [go]\n    sites: indeed, linkedin\n"})
    assert r.status_code == 200
    assert r.json()["warning"] is None


def test_post_rejects_non_iterable_sites_with_400_not_500(client):
    # `sites: 5` raised TypeError out of the endpoint — an unhandled 500, when
    # the whole point of this route is to reject bad configs with a message.
    c, root = client
    r = c.post("/api/local-search", json={
        "content": "searches:\n  - name: p\n    search_terms: [go]\n    sites: 5\n"})
    assert r.status_code == 400
    assert "supported board" in r.json()["detail"]
    assert not _local_file(root).exists()
