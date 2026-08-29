"""Shared LLM client construction, with provider fallback. See PRD sections 13, 14.

Lazy: constructing the module doesn't require a key. The key is only needed when a
real call is made, which lets tests import parser.py/agent.py and inject a fake client
without ever touching the environment.

Multi-provider fallback: tries Groq first (if GROQ_API_KEY is set), falls back to
Cerebras. Only providers with a configured key are tried at all -- with just
CEREBRAS_API_KEY set (this repo's current state), this degrades to exactly the
single-provider behaviour it always had. Every failure mode still surfaces as
CerebrasError to the caller, so agent.py's/app.py's existing `except CerebrasError`
handling needs no change when a second provider is added.
"""
import os
from pathlib import Path

from cerebras.cloud.sdk import Cerebras, CerebrasError

MODEL = "gpt-oss-120b"

# ponytail: same logical model across providers, but provider catalogs use different
# ID strings for what's nominally the same open model. Groq's exact current ID for
# gpt-oss-120b is unverified in this build (no GROQ_API_KEY was available to test
# against) -- confirm against console.groq.com/docs/models before relying on it live.
_PROVIDER_MODEL_IDS = {
    "groq": "openai/gpt-oss-120b",
    "cerebras": "gpt-oss-120b",
}

_client = None


def _load_dotenv():
    # ponytail: no python-dotenv dependency for two lines of parsing.
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


class _FallbackClient:
    """OpenAI-shaped `.chat.completions.create(...)`. Tries each configured provider
    in order; on any exception, falls through to the next. Raises CerebrasError
    (wrapping the last real error) if every provider fails."""

    def __init__(self, providers: list[tuple[str, object]]):
        self._providers = providers
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        last_err = None
        for name, client in self._providers:
            call_kwargs = {**kwargs, "model": _PROVIDER_MODEL_IDS.get(name, kwargs.get("model"))}
            try:
                return client.chat.completions.create(**call_kwargs)
            except Exception as e:  # noqa: broad -- normalize any provider's own error type
                last_err = e
                continue
        raise CerebrasError(f"all LLM providers failed; last error: {last_err}")


def get_client():
    global _client
    if _client is None:
        _load_dotenv()
        providers: list[tuple[str, object]] = []
        if os.environ.get("GROQ_API_KEY"):
            from groq import Groq
            providers.append(("groq", Groq()))
        if os.environ.get("CEREBRAS_API_KEY"):
            providers.append(("cerebras", Cerebras()))
        if not providers:
            raise CerebrasError(
                "no LLM provider configured -- set GROQ_API_KEY and/or CEREBRAS_API_KEY"
            )
        # Always wrap, even for one provider -- _FallbackClient is also what applies
        # _PROVIDER_MODEL_IDS's per-provider model remap and normalizes every
        # provider's own exception type to CerebrasError. A bare single-provider
        # client skips both: found live when Cerebras was disabled and Groq-only
        # requests 404'd on the un-remapped bare model id, uncaught by
        # `except CerebrasError` anywhere in agent.py/app.py.
        _client = _FallbackClient(providers)
    return _client
