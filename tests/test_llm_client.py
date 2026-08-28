"""Multi-provider fallback (llm_client._FallbackClient): tries providers in order,
falls through on failure, raises CerebrasError only if every provider fails.
"""
import pytest
from cerebras.cloud.sdk import CerebrasError

from llm_client import _FallbackClient


class _FakeProvider:
    """Mimics client.chat.completions.create(**kwargs)."""
    def __init__(self, result=None, raises=None):
        self.result = result
        self.raises = raises
        self.calls = []
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises:
            raise self.raises
        return self.result


def test_first_provider_success_never_tries_second():
    p1 = _FakeProvider(result="ok-from-p1")
    p2 = _FakeProvider(result="ok-from-p2")
    client = _FallbackClient([("groq", p1), ("cerebras", p2)])
    assert client.chat.completions.create(model="whatever") == "ok-from-p1"
    assert p1.calls and not p2.calls


def test_first_provider_fails_falls_through_to_second():
    p1 = _FakeProvider(raises=RuntimeError("groq down"))
    p2 = _FakeProvider(result="ok-from-p2")
    client = _FallbackClient([("groq", p1), ("cerebras", p2)])
    assert client.chat.completions.create(model="whatever") == "ok-from-p2"
    assert p1.calls and p2.calls


def test_all_providers_fail_raises_cerebras_error():
    p1 = _FakeProvider(raises=RuntimeError("groq down"))
    p2 = _FakeProvider(raises=RuntimeError("cerebras down"))
    client = _FallbackClient([("groq", p1), ("cerebras", p2)])
    with pytest.raises(CerebrasError):
        client.chat.completions.create(model="whatever")


def test_each_provider_gets_its_own_model_id():
    p1 = _FakeProvider(raises=RuntimeError("groq down"))
    p2 = _FakeProvider(result="ok")
    client = _FallbackClient([("groq", p1), ("cerebras", p2)])
    client.chat.completions.create(model="gpt-oss-120b")
    assert p1.calls[0]["model"] == "openai/gpt-oss-120b"
    assert p2.calls[0]["model"] == "gpt-oss-120b"
