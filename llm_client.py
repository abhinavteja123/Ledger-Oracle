"""Shared LLM client construction, with provider fallback. See PRD sections 13, 14.

Lazy: constructing the module doesn't require a key. The key is only needed when a
real call is made, which lets tests import parser.py/agent.py and inject a fake client
without ever touching the environment.

Multi-provider fallback: tries Groq first (if GROQ_API_KEY is set), falls back to
Gemini (via its OpenAI-compatible endpoint). Only providers with a configured key are
tried at all -- with just one key set, this degrades to exactly the single-provider
behaviour it always had. Every failure mode still surfaces as LLMProviderError to the
caller, so agent.py's/app.py's existing `except LLM_ERRORS` handling needs no change
when a provider is added or swapped.
"""
import os
from pathlib import Path

from openai import OpenAI

MODEL = "gpt-oss-120b"


class LLMProviderError(Exception):
    """Raised when every configured LLM provider fails. Normalizes every provider
    SDK's own exception type so callers need exactly one except clause."""


# ponytail: same logical model across providers, but provider catalogs use different
# ID strings for what's nominally the same open model. Groq's id confirmed live this
# session (real tool-calling response through get_client()'s wrapper, see the Playwright
# E2E pass this session). Gemini's id was NOT confirmed live in an earlier session and
# had since gone stale: both gemini-2.5-flash and gemini-2.0-flash now 404 live
# ("no longer available to new users... use models/gemini-3.6-flash") -- found running
# a real end-to-end test, not from docs. gemini-3.6-flash confirmed live (real content
# returned) before landing this change. Re-verify against ai.google.dev if this 404s
# again later -- Google's deprecation cadence here is not this repo's to control.
_PROVIDER_MODEL_IDS = {
    "groq": "openai/gpt-oss-120b",
    "gemini": "gemini-3.6-flash",
}

_client = None


def load_dotenv():
    # ponytail: no python-dotenv dependency for two lines of parsing.
    #
    # Public (was module-private): previously only called lazily from get_client(),
    # so SUPABASE_DB_URL/SUPABASE_READONLY_DB_URL (read directly via os.environ in
    # db.py, which never called this) stayed unset for any process whose first
    # request was /admin/* or /review/* rather than /verify -- a real KeyError hit
    # live testing the Supabase-backed admin console cold. app.py now calls this
    # once at import time so every route sees a populated environment regardless of
    # request order.
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
    in order; on any exception, falls through to the next. Raises LLMProviderError
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
        raise LLMProviderError(f"all LLM providers failed; last error: {last_err}")


def get_client():
    global _client
    if _client is None:
        load_dotenv()
        providers: list[tuple[str, object]] = []
        if os.environ.get("GROQ_API_KEY"):
            from groq import Groq
            providers.append(("groq", Groq()))
        if os.environ.get("GEMINI_API_KEY"):
            providers.append(("gemini", OpenAI(
                api_key=os.environ["GEMINI_API_KEY"],
                base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            )))
        if not providers:
            raise LLMProviderError(
                "no LLM provider configured -- set GROQ_API_KEY and/or GEMINI_API_KEY"
            )
        # Always wrap, even for one provider -- _FallbackClient is also what applies
        # _PROVIDER_MODEL_IDS's per-provider model remap and normalizes every
        # provider's own exception type to LLMProviderError. A bare single-provider
        # client skips both: found live when only one provider key was configured and
        # Groq-only requests 404'd on the un-remapped bare model id, uncaught by
        # `except LLM_ERRORS` anywhere in agent.py/app.py.
        _client = _FallbackClient(providers)
    return _client
