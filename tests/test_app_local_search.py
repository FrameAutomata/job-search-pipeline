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


# ── JobSpy mutual-exclusion validation ──────────────────────────────────────
# A pass combining options JobSpy refuses together parses fine, loads fine, and
# names supported boards — the run then skips it. Refuse the save instead, while
# the config is still in front of the user.


def test_post_rejects_indeed_hours_old_with_is_remote(client):
    c, root = client
    content = ("searches:\n  - name: US Remote\n    search_terms: [python]\n"
               "    sites: [indeed]\n    hours_old: 168\n    is_remote: true\n")
    r = c.post("/api/local-search", json={"content": content})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "Indeed limitation" in detail and "US Remote" in detail
    assert not _local_file(root).exists()      # nothing written on reject


def test_post_rejects_conflict_on_a_pass_that_never_names_indeed(client):
    # The issue's second repro: an omitted `sites` inherits the supported boards
    # upstream, so the rule binds a pass whose own YAML says nothing about Indeed.
    c, root = client
    content = ("searches:\n  - name: p\n    search_terms: [python]\n"
               "    hours_old: 168\n    is_remote: true\n")
    r = c.post("/api/local-search", json={"content": content})
    assert r.status_code == 400
    assert "Indeed limitation" in r.json()["detail"]
    assert not _local_file(root).exists()


def test_post_accepts_linkedin_hours_old_with_easy_apply(client):
    # LinkedIn sends both filters; only Indeed drops one. Refusing the save was
    # rejecting a config that scrapes exactly as written (#115).
    c, root = client
    content = ("searches:\n  - name: p\n    search_terms: [python]\n"
               "    sites: [linkedin]\n    hours_old: 168\n    easy_apply: true\n")
    r = c.post("/api/local-search", json={"content": content})
    assert r.status_code == 200
    assert _local_file(root).read_text(encoding="utf-8") == content


def test_post_still_rejects_that_combination_when_indeed_is_named_too(client):
    c, root = client
    content = ("searches:\n  - name: p\n    search_terms: [python]\n"
               "    sites: [linkedin, indeed]\n    hours_old: 168\n    easy_apply: true\n")
    r = c.post("/api/local-search", json={"content": content})
    assert r.status_code == 400
    assert "Indeed limitation" in r.json()["detail"]
    # The half the unit test cannot cover: a refused save writes nothing.
    assert not _local_file(root).exists()


def test_post_rejects_a_value_jobspy_cannot_read(client):
    # Saving this would break the endpoint's promise that what it accepts is a
    # config the next run can load: jobspy rejects the value while building
    # ScraperInput, which aborts the scrape stage rather than skipping a pass.
    c, root = client
    content = ("searches:\n  - name: p\n    search_terms: [python]\n"
               "    sites: [indeed]\n    easy_apply: maybe\n")
    r = c.post("/api/local-search", json={"content": content})
    assert r.status_code == 400
    assert "easy_apply" in r.json()["detail"]
    assert not _local_file(root).exists()


def test_post_accepts_a_quoted_off_option(client):
    # A quoted scalar is what a templating tool or a hand edit produces, and
    # `"false"` is a truthy str to a raw read but a falsy bool to jobspy's
    # pydantic model. Saving must go through the same normalization a run does,
    # or the UI 400s a config that scrapes perfectly well.
    c, root = client
    content = ("searches:\n  - name: p\n    search_terms: [python]\n"
               "    sites: [indeed]\n    hours_old: 168\n    is_remote: \"false\"\n")
    r = c.post("/api/local-search", json={"content": content})
    assert r.status_code == 200
    assert _local_file(root).read_text(encoding="utf-8") == content


def test_post_still_rejects_a_quoted_on_option(client):
    c, root = client
    content = ("searches:\n  - name: p\n    search_terms: [python]\n"
               "    sites: [indeed]\n    hours_old: 168\n    easy_apply: \"true\"\n")
    r = c.post("/api/local-search", json={"content": content})
    assert r.status_code == 400
    assert "Indeed limitation" in r.json()["detail"]
    assert not _local_file(root).exists()


def test_post_rejects_when_only_one_pass_conflicts(client):
    # One healthy pass doesn't excuse the broken one — saved, the run would drop
    # it and the user would never learn why that search returned nothing.
    c, root = client
    content = ("searches:\n"
               "  - name: fine\n    search_terms: [go]\n    sites: [indeed]\n    hours_old: 168\n"
               "  - name: broken\n    search_terms: [go]\n    sites: [indeed]\n"
               "    hours_old: 168\n    easy_apply: true\n")
    r = c.post("/api/local-search", json={"content": content})
    assert r.status_code == 400
    assert "broken" in r.json()["detail"]
    assert not _local_file(root).exists()


def test_post_accepts_the_split_passes_that_fix_a_conflict(client):
    # The documented workaround — split the filters across passes — must save.
    c, root = client
    content = ("searches:\n"
               "  - name: recent\n    search_terms: [go]\n    sites: [indeed]\n"
               "    hours_old: 168\n"
               "  - name: remote\n    search_terms: [go]\n    sites: [indeed]\n"
               "    is_remote: true\n")
    r = c.post("/api/local-search", json={"content": content})
    assert r.status_code == 200
    assert r.json()["warning"] is None
    assert _local_file(root).read_text(encoding="utf-8") == content


def test_post_accepts_indeed_job_type_with_is_remote(client):
    # Both sit in the same Indeed group, so together they are legal.
    c, root = client
    content = ("searches:\n  - name: p\n    search_terms: [go]\n    sites: [indeed]\n"
               "    job_type: fulltime\n    is_remote: true\n")
    r = c.post("/api/local-search", json={"content": content})
    assert r.status_code == 200


def test_post_accepts_a_conflict_on_a_retired_board_only(client):
    # zip_recruiter never runs, so the combination it would have accepted is
    # moot — but the pass has no supported board left, which is the older
    # rejection, and it must be the one reported.
    c, root = client
    content = ("searches:\n  - name: p\n    search_terms: [go]\n"
               "    sites: [zip_recruiter]\n    hours_old: 168\n    is_remote: true\n")
    r = c.post("/api/local-search", json={"content": content})
    assert r.status_code == 400
    assert "supported board" in r.json()["detail"]
