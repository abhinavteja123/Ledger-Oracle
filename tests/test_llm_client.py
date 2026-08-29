"""Multi-provider fallback (llm_client._FallbackClient): tries providers in order,
falls through on failure, raises CerebrasError only if every provider fails.
"""
import pytest
from cerebras.cloud.sdk import CerebrasError

import llm_client
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


def test_get_client_wraps_single_provider_too(monkeypatch):
    """Regression: a lone configured provider must still go through
    _FallbackClient, so its model id gets remapped (Cerebras's bare
    'gpt-oss-120b' isn't Groq's real id) and its own exception types get
    normalized to CerebrasError -- a bare single-provider client skipped both,
    causing a live 404 model_not_found (uncaught 500) with Groq-only config."""
    monkeypatch.setattr(llm_client, "_client", None)
    monkeypatch.setenv("GROQ_API_KEY", "fake-key-for-test")
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    # get_client() calls the real _load_dotenv(), which uses setdefault() and would
    # otherwise leak LEDGER_BACKEND=supabase (and real Supabase URLs) from the repo's
    # .env into the shared pytest process env for every test running after this one --
    # pre-set it so that setdefault() is a no-op.
    monkeypatch.setenv("LEDGER_BACKEND", "sqlite")
    client = llm_client.get_client()
    assert isinstance(client, _FallbackClient)
    monkeypatch.setattr(llm_client, "_client", None)
