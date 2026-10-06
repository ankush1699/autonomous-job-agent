"""
core/llm.py — single place where models are chosen.

Two tiers:

  cheap    should-apply scoring, keyword extraction, strategist, editor,
           strategy object. Chain (first available wins): Claude Haiku →
           Groq → local Ollama.

           Haiku is first ON PURPOSE, not as a fallback. Groq's free
           openai/gpt-oss-120b was the original first choice, but measured
           against 218 real cached verdicts it produced internally
           inconsistent scores 44% (role_type) / 34% (seniority) / 67%
           (sponsorship) of the time, fabricated a "senior" label against a
           JD that said "1 year of experience", emitted out-of-range and
           malformed tool calls, and exhausted its daily quota mid-cycle —
           five distinct failure modes, each patched with a guard, and the
           guards only ever caught symptoms. Because it failed so often,
           nearly every call was already falling back to Haiku anyway, so
           putting Haiku first costs roughly nothing extra and removes a
           wasted failed round-trip per job. Groq/Ollama stay in the chain
           strictly as fallbacks for an Anthropic outage.

  quality  writer + cover letter prose only. Claude Sonnet.

Env overrides:
  LLM_CHEAP_PROVIDER = anthropic | groq | ollama   (skip auto-detection)
  GROQ_MODEL         (default: openai/gpt-oss-120b)
  OLLAMA_MODEL       (default: llama3.1:8b)
  OLLAMA_HOST        (default: http://localhost:11434)
  QUALITY_MODEL      (default: claude-sonnet-4-6)
"""

import os
import functools

import requests

_GROQ_MODEL    = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
_OLLAMA_MODEL  = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
_OLLAMA_HOST   = os.getenv("OLLAMA_HOST", "http://localhost:11434")
_QUALITY_MODEL = os.getenv("QUALITY_MODEL", "claude-sonnet-4-6")
_HAIKU_MODEL   = os.getenv("HAIKU_MODEL", "claude-haiku-4-5-20251001")


def _ollama_running() -> bool:
    try:
        return requests.get(f"{_OLLAMA_HOST}/api/tags", timeout=1.5).status_code == 200
    except Exception:
        return False


# Preference order for the cheap tier. See the module docstring for why
# Anthropic (Haiku) is deliberately first rather than the free Groq tier.
_CHEAP_CHAIN = ["anthropic", "groq", "ollama"]


def _provider_available(name: str) -> bool:
    if name == "anthropic":
        return bool(os.getenv("ANTHROPIC_API_KEY"))
    if name == "groq":
        return bool(os.getenv("GROQ_API_KEY"))
    if name == "ollama":
        return _ollama_running()
    return False


@functools.lru_cache(maxsize=1)
def cheap_provider() -> str:
    """Resolve the cheap-tier provider once per process."""
    forced = os.getenv("LLM_CHEAP_PROVIDER", "").strip().lower()
    if forced in _CHEAP_CHAIN:
        return forced
    for name in _CHEAP_CHAIN:
        if _provider_available(name):
            if name != "anthropic":
                print(f"[llm] WARNING: ANTHROPIC_API_KEY not set — cheap tier is using {name}, "
                      "which scored unreliably in testing (see core/llm.py docstring).")
            return name
    raise RuntimeError(
        "No cheap-tier LLM provider available: set ANTHROPIC_API_KEY (recommended), "
        "GROQ_API_KEY, or run `ollama serve`."
    )


def get_chat_model(tier: str, *, temperature: float = 0.0, max_tokens: int = 2048,
                    provider_override: str = None):
    """
    Return a LangChain chat model for the given tier ("cheap" | "quality").
    Lazy-imports providers so missing packages only matter if selected.

    provider_override bypasses cheap_provider()'s cached resolution — used by
    callers that just caught a quota-exhaustion error from the normal
    provider and want to build a model for the NEXT one in the chain instead,
    without permanently changing the process-wide default (see
    next_cheap_provider() below).
    """
    if tier == "quality":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=_QUALITY_MODEL, temperature=temperature, max_tokens=max_tokens)

    if tier != "cheap":
        raise ValueError(f"Unknown LLM tier: {tier!r} (expected 'cheap' or 'quality')")

    provider = provider_override or cheap_provider()
    if provider == "groq":
        from langchain_groq import ChatGroq
        return ChatGroq(model=_GROQ_MODEL, temperature=temperature, max_tokens=max_tokens)
    if provider == "ollama":
        from langchain_ollama import ChatOllama
        # num_predict is Ollama's max_tokens equivalent
        return ChatOllama(model=_OLLAMA_MODEL, temperature=temperature, num_predict=max_tokens)

    from langchain_anthropic import ChatAnthropic
    return ChatAnthropic(model=_HAIKU_MODEL, temperature=temperature, max_tokens=max_tokens)


def is_quota_exhausted(exc: Exception) -> bool:
    """
    True if `exc` looks like a hard, provider-side blocker that retrying the
    SAME provider won't fix — a daily/quota limit that won't clear for tens
    of minutes (e.g. Groq's "tokens per day" cap), a model that's gone
    entirely (decommissioned/renamed server-side, e.g. Groq's June 2026
    deprecation of llama-3.3-70b-versatile returning a 404 model_not_found
    with zero warning), or a model that can't reliably produce a valid tool
    call at all (Groq's openai/gpt-oss-120b intermittently emits malformed
    or schema-violating tool-call JSON — "Failed to parse tool call
    arguments as JSON", "did not match schema", "Tool choice is required,
    but model did not call a tool" — all of which carry Groq's own
    'code': 'tool_use_failed'). All three are properties of the PROVIDER/
    MODEL for this call, not a one-off fluke, so they call for hopping to
    the next provider in the chain instead of burning retries (and tokens —
    the tool_use_failed case was quietly eating into the daily token quota,
    contributing to real 429s later in the same run) against the same dead
    end. Real examples this was written for: Groq 429 "Limit 100000, Used
    99898... Please try again in 26m59s", Groq 404 "The model `...` does
    not exist or you do not have access to it", and Groq 400 "code":
    "tool_use_failed".
    """
    text = str(exc).lower()
    return (
        "rate_limit_exceeded" in text
        or "tokens per day" in text
        or " tpd" in text
        or "requests per day" in text
        or "model_not_found" in text
        or "tool_use_failed" in text
        or "does not exist or you do not have access to it" in text
    )


def next_cheap_provider(after: str) -> str | None:
    """
    Returns the next usable provider in the chain after `after`, skipping
    `after` itself — for use when `after` just returned a quota-exhaustion
    error. Returns None if nothing further is available (caller should give
    up rather than loop).
    """
    if after not in _CHEAP_CHAIN:
        return None
    for candidate in _CHEAP_CHAIN[_CHEAP_CHAIN.index(after) + 1:]:
        if _provider_available(candidate):
            return candidate
    return None


def describe() -> str:
    """One-line summary of the active configuration, for startup logs."""
    provider = cheap_provider()
    cheap_model = {"groq": _GROQ_MODEL, "ollama": _OLLAMA_MODEL, "anthropic": _HAIKU_MODEL}[provider]
    return f"cheap tier: {provider}/{cheap_model} · quality tier: anthropic/{_QUALITY_MODEL}"
