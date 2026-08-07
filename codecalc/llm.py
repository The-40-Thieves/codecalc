"""Thin LLM client for the local LiteLLM gateway (OpenAI-compatible).

Used by translation and complexity refinement. Auth via LITELLM_AGENT_KEY
(or CODECALC_LLM_API_KEY override); model via CODECALC_LLM_MODEL, defaulting
to the gateway's healthy gpt-4o-mini deployment.
"""

from __future__ import annotations

import json
import os
import urllib.request

#: OpenAI-compatible chat-completions endpoint. NO DEFAULT, on purpose.
#:
#: This used to default to a private tailnet address (a LiteLLM gateway that
#: exists on exactly one machine) in a PUBLIC repo. Anyone else got a hang and
#: then a connection error from translate_code/optimize_code, with nothing
#: pointing at the cause.
#:
#: Defaulting to a real provider instead would be worse: it would send the
#: caller's source code to a third party that nobody configured. Unset means
#: the two LLM-backed tools report themselves unconfigured and the other 46
#: work untouched.
GATEWAY = os.environ.get("CODECALC_LLM_GATEWAY", "")
DEFAULT_MODEL = os.environ.get("CODECALC_LLM_MODEL", "gpt-4o-mini")


def _key() -> str:
    return os.environ.get("CODECALC_LLM_API_KEY") or os.environ.get("LITELLM_AGENT_KEY", "")


def chat(prompt: str, system: str | None = None, model: str | None = None,
         timeout: int = 60, temperature: float = 0.0,
         max_tokens: int = 4096) -> str:
    """One chat completion through the gateway. Raises on failure."""
    if not GATEWAY:
        # Named, actionable, and raised BEFORE any network call — the callers
        # (translate_code / optimize_code) turn this into
        # {"ok": false, "error": "LLM unavailable: ...", "llm_available": false}.
        raise RuntimeError(
            "no LLM gateway configured: set CODECALC_LLM_GATEWAY to an "
            "OpenAI-compatible /v1/chat/completions endpoint (and "
            "CODECALC_LLM_API_KEY if it needs auth). Only translate_code and "
            "optimize_code need it; every other tool works without it."
        )
    model = model or os.environ.get("CODECALC_LLM_MODEL") or DEFAULT_MODEL
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    body = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }).encode()
    headers = {"Content-Type": "application/json"}
    key = _key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(GATEWAY, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"]


def available() -> bool:
    """True when the gateway answers (cheap probe)."""
    try:
        chat("reply with the single word: ok", max_tokens=5, timeout=10)
        return True
    except Exception:
        return False
