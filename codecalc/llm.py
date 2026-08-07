"""Thin LLM client for the local LiteLLM gateway (OpenAI-compatible).

Used by translation and complexity refinement. Auth via LITELLM_AGENT_KEY
(or CODECALC_LLM_API_KEY override); model via CODECALC_LLM_MODEL, defaulting
to the gateway's healthy gpt-4o-mini deployment.
"""

from __future__ import annotations

import json
import os
import urllib.request

GATEWAY = os.environ.get(
    "CODECALC_LLM_GATEWAY", "http://100.78.123.100:4001/v1/chat/completions"
)
DEFAULT_MODEL = "gpt-4o-mini"


def _key() -> str:
    return os.environ.get("CODECALC_LLM_API_KEY") or os.environ.get("LITELLM_AGENT_KEY", "")


def chat(prompt: str, system: str | None = None, model: str | None = None,
         timeout: int = 60, temperature: float = 0.0,
         max_tokens: int = 4096) -> str:
    """One chat completion through the gateway. Raises on failure."""
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
