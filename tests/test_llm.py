import pytest

from agent import llm


def test_gemini_payment_error_uses_configured_fallback(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setattr(llm, "available", lambda: True)

    def gemini(*args, **kwargs):
        raise llm.LLMUnavailable("HTTP 402: quota unavailable")

    fallback_calls = []

    def fallback(contents, **kwargs):
        fallback_calls.append(contents)
        return '{"answers": []}'

    monkeypatch.setattr(llm, "_gemini_generate", gemini)
    monkeypatch.setattr(llm, "_openai_generate", fallback)

    result = llm.generate_json("application answer prompt", {"type": "object"})

    assert result == {"answers": []}
    assert fallback_calls == [["application answer prompt"]]


def test_gemini_payment_error_stays_unavailable_without_fallback(monkeypatch):
    for name in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "LLM_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(llm, "_failures", 0)
    monkeypatch.setattr(llm, "_open_until", 0.0)
    monkeypatch.setattr(llm, "gemini_settings", lambda: ("test-gemini-key", "test-model"))
    posts = []
    monkeypatch.setattr(llm, "_post_payload", lambda *a, **k: (posts.append(a[0]) or 402,
                                                                 "Payment Required"))

    with pytest.raises(llm.LLMUnavailable, match="HTTP 402"):
        llm.generate_json("application answer prompt", {"type": "object"})
    with pytest.raises(llm.LLMUnavailable, match="circuit breaker open"):
        llm.generate_json("second prompt", {"type": "object"})
    assert len(posts) == 1


def test_gemini_payment_error_opens_breaker_and_uses_fallback_for_next_call(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setattr(llm, "_failures", 0)
    monkeypatch.setattr(llm, "_open_until", 0.0)
    monkeypatch.setattr(llm, "gemini_settings", lambda: ("test-gemini-key", "test-model"))
    posts = []
    fallbacks = []

    def post(*args, **kwargs):
        posts.append(args[0])
        return 402, "Payment Required"

    def fallback(contents, **kwargs):
        fallbacks.append(contents)
        return '{"answers": []}'

    monkeypatch.setattr(llm, "_post_payload", post)
    monkeypatch.setattr(llm, "_openai_generate", fallback)

    first = llm.generate_json("first prompt", {"type": "object"})
    second = llm.generate_json("second prompt", {"type": "object"})

    assert first == second == {"answers": []}
    assert len(posts) == 1
    assert fallbacks == [["first prompt"], ["second prompt"]]
