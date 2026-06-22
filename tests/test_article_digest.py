"""Tests for pipeline/article_digest.py — grounded, honesty-constrained
generation of career-ops/article-digest.md during onboarding.

Everything external is injected: a fake `caller` stands in for the LLM and a
fake `fetch` stands in for GitHub, so the suite is fully offline and
deterministic (no provider key, no network). The contract under test:

  * the prompt forbids fabrication and mandates the [TODO: confirm] convention;
  * generation is BEST-EFFORT — it never raises into the onboarding flow, and
    it refuses to call the LLM when there's nothing real to ground on (so we
    can't hallucinate a digest out of thin air);
  * README fetching is best-effort and GitHub-only;
  * the digest is written only when non-empty.
"""

from pipeline import article_digest


class TestBuildPrompt:
    def test_system_prompt_is_honesty_constrained(self):
        system, _ = article_digest.build_prompt("resume", [], {})
        s = system.lower()
        # Grounding: facts only from the provided sources.
        assert "only" in s
        # Anti-fabrication: must explicitly forbid inventing.
        assert "invent" in s or "fabricat" in s
        # The [TODO: confirm] convention for unknowns (so gaps are flagged, not guessed).
        assert "[todo" in s
        # Specifically must not estimate numeric metrics.
        assert "metric" in s or "number" in s

    def test_user_message_includes_all_grounding_sources(self):
        _, user = article_digest.build_prompt(
            "RESUME BODY TEXT",
            ["https://github.com/me/proj"],
            {"https://github.com/me/proj": "README CONTENT HERE"},
        )
        assert "RESUME BODY TEXT" in user          # resume is grounding
        assert "README CONTENT HERE" in user        # repo docs are grounding
        assert "github.com/me/proj" in user          # portfolio URL referenced


class TestGenerate:
    def test_uses_injected_caller_and_returns_its_output(self):
        captured = {}

        def fake_caller(system, user):
            captured["system"], captured["user"] = system, user
            return "## Proj\n**Hero metrics:** real, sourced stuff\n"

        out = article_digest.generate(
            "RESUME BODY", ["https://github.com/me/proj"],
            caller=fake_caller,
            repo_docs={"https://github.com/me/proj": "README"},
        )
        assert out.startswith("## Proj")
        # The caller saw the real source material.
        assert "RESUME BODY" in captured["user"]
        assert "README" in captured["user"]

    def test_returns_empty_on_caller_failure(self):
        def boom(system, user):
            raise RuntimeError("provider down")

        # Best-effort: a failed LLM call must NOT break onboarding.
        out = article_digest.generate(
            "RESUME", ["https://github.com/me/p"], caller=boom, repo_docs={})
        assert out == ""

    def test_skips_llm_when_nothing_to_ground_on(self):
        # No resume text and no repo docs → calling the LLM could only fabricate,
        # so skip it entirely and return "".
        called = False

        def caller(system, user):
            nonlocal called
            called = True
            return "anything"

        out = article_digest.generate("", [], caller=caller, repo_docs={})
        assert out == ""
        assert called is False


class TestFetchRepoDocs:
    def test_best_effort_keeps_successes_skips_failures(self):
        def fake_fetch(url):
            if "good" in url:
                return "# Good README"
            raise RuntimeError("404")

        docs = article_digest.fetch_repo_docs(
            ["https://github.com/me/good", "https://github.com/me/bad"],
            fetch=fake_fetch,
        )
        assert docs == {"https://github.com/me/good": "# Good README"}

    def test_ignores_non_github_urls(self):
        def fake_fetch(url):
            raise AssertionError("should not fetch a non-github URL")

        assert article_digest.fetch_repo_docs(
            ["https://example.com/x", "not a url"], fetch=fake_fetch) == {}

    def test_never_raises(self):
        def boom(url):
            raise RuntimeError("network")

        assert article_digest.fetch_repo_docs(
            ["https://github.com/me/p"], fetch=boom) == {}


class TestWriteArticleDigest:
    def test_writes_when_nonempty(self, tmp_path):
        co = tmp_path / "career-ops"
        co.mkdir()
        p = article_digest.write_article_digest(co, "## Proj\ncontent\n")
        assert p == co / "article-digest.md"
        assert p.read_text(encoding="utf-8").startswith("## Proj")

    def test_noop_when_empty(self, tmp_path):
        co = tmp_path / "career-ops"
        co.mkdir()
        assert article_digest.write_article_digest(co, "") is None
        assert not (co / "article-digest.md").exists()


class TestNeedsDigest:
    """We never clobber a non-empty article-digest.md — it may be hand-curated."""

    def test_true_when_missing(self, tmp_path):
        co = tmp_path / "career-ops"
        co.mkdir()
        assert article_digest.needs_digest(co) is True

    def test_true_when_empty_or_whitespace(self, tmp_path):
        co = tmp_path / "career-ops"
        co.mkdir()
        (co / "article-digest.md").write_text("   \n\n", encoding="utf-8")
        assert article_digest.needs_digest(co) is True

    def test_false_when_present_and_nonempty(self, tmp_path):
        co = tmp_path / "career-ops"
        co.mkdir()
        (co / "article-digest.md").write_text("## Proj\ncontent", encoding="utf-8")
        assert article_digest.needs_digest(co) is False


class TestGenerateAndWrite:
    """Onboarding entry point: skip if a digest exists, else generate + write."""

    def test_skips_and_preserves_existing_digest(self, tmp_path):
        co = tmp_path / "career-ops"
        co.mkdir()
        (co / "article-digest.md").write_text("## Existing\ncurated", encoding="utf-8")
        called = False

        def caller(system, user):
            nonlocal called
            called = True
            return "## Fresh draft that should NOT be written"

        out = article_digest.generate_and_write(co, "RESUME", [], caller=caller)
        assert out is None
        assert called is False  # didn't even call the LLM
        assert (co / "article-digest.md").read_text(encoding="utf-8") == "## Existing\ncurated"

    def test_generates_and_writes_when_missing(self, tmp_path):
        co = tmp_path / "career-ops"
        co.mkdir()

        def caller(system, user):
            return "## Proj\ncontent"

        out = article_digest.generate_and_write(co, "RESUME BODY", [], caller=caller)
        assert out == co / "article-digest.md"
        assert "## Proj" in out.read_text(encoding="utf-8")
