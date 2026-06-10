"""Gap #2: reasoning models' <think> phase is disabled for short/direct outputs
(apply answers, cover letters) via chat_template_kwargs — but only for providers
whose backend accepts it (deepinfra/openrouter/ollama), never OpenAI/Groq."""

import types

import pipeline.batch_evaluate as be


def _fake_openai_client(captured: dict):
    """A stand-in OpenAI client whose chat.completions.create records kwargs."""
    def create(**kwargs):
        captured.update(kwargs)
        msg = types.SimpleNamespace(content="answer")
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])
    return types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create))
    )


class TestThinkingToggle:
    def _patch(self, monkeypatch, captured):
        monkeypatch.setattr("openai.OpenAI", lambda **kw: _fake_openai_client(captured))

    def test_disable_thinking_sets_extra_body(self, monkeypatch):
        captured: dict = {}
        self._patch(monkeypatch, captured)
        caller = be._build_openai_compat_caller("m", api_key="k", disable_thinking=True)
        assert caller("sys", "user") == "answer"
        assert captured["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}

    def test_default_has_no_extra_body(self, monkeypatch):
        captured: dict = {}
        self._patch(monkeypatch, captured)
        be._build_openai_compat_caller("m", api_key="k")("sys", "user")
        assert captured.get("extra_body") is None

    def test_toggle_applied_for_deepinfra(self, monkeypatch):
        captured: dict = {}
        self._patch(monkeypatch, captured)
        monkeypatch.setenv("DEEPINFRA_API_KEY", "k")
        be._build_caller("deepinfra", "m", disable_thinking=True)("sys", "user")
        assert captured["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}

    def test_toggle_ignored_for_openai(self, monkeypatch):
        # OpenAI rejects unknown body params, so it never gets the toggle even
        # when disable_thinking=True is requested.
        captured: dict = {}
        self._patch(monkeypatch, captured)
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        be._build_caller("openai", "m", disable_thinking=True)("sys", "user")
        assert captured.get("extra_body") is None
