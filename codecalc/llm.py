"""Thin LLM client for the local LiteLLM gateway (OpenAI-compatible).

Used by translation and complexity refinement. Auth via LITELLM_AGENT_KEY
(or CODECALC_LLM_API_KEY override); model via CODECALC_LLM_MODEL, defaulting
to the gateway's healthy gpt-4o-mini deployment.
"""

from __future__ import annotations

import json
import os
import urllib.parse
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

#: Only these may be fetched. `urllib.request.urlopen` also speaks file:, ftp:
#: and data:, so an endpoint of `file:///etc/hostname` is opened and READ —
#: verified, it came back with the machine's hostname. The endpoint comes from
#: configuration, and configuration is exactly the thing that gets typo'd,
#: templated, or pasted from the wrong place.
_ALLOWED_SCHEMES = ("http", "https")


def gateway() -> str:
    """The configured endpoint, read at CALL time.

    The module-level constant is captured at import, so anything that sets
    CODECALC_LLM_GATEWAY afterwards — a server that loads its config after
    importing its modules, a test, a shell that exports it into an already
    running process — got an empty gateway and the "not configured" error, with
    the variable plainly set. CODECALC_LLM_MODEL was already re-read per call,
    so the two halves of the same configuration disagreed about when they were
    read; now they do not.
    """
    return os.environ.get("CODECALC_LLM_GATEWAY", "") or GATEWAY


def _key() -> str:
    return os.environ.get("CODECALC_LLM_API_KEY") or os.environ.get("LITELLM_AGENT_KEY", "")


def chat(prompt: str, system: str | None = None, model: str | None = None,
         timeout: int = 60, temperature: float = 0.0,
         max_tokens: int = 4096) -> str:
    """One chat completion through the gateway. Raises on failure."""
    endpoint = gateway()
    if not endpoint:
        # Named, actionable, and raised BEFORE any network call — the callers
        # (translate_code / optimize_code) turn this into
        # {"ok": false, "error": "LLM unavailable: ...", "llm_available": false}.
        raise RuntimeError(
            "no LLM gateway configured: set CODECALC_LLM_GATEWAY to an "
            "OpenAI-compatible /v1/chat/completions endpoint (and "
            "CODECALC_LLM_API_KEY if it needs auth). Only translate_code and "
            "optimize_code need it; every other tool works without it."
        )
    scheme = urllib.parse.urlparse(endpoint).scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise RuntimeError(
            f"refusing to use CODECALC_LLM_GATEWAY with scheme {scheme or '(none)'!r}: "
            f"expected one of {', '.join(_ALLOWED_SCHEMES)}. urlopen would otherwise "
            f"open a local file or an ftp/data URL as if it were the model gateway."
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
    req = urllib.request.Request(endpoint, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    return _content(data, model)


def _content(data: object, model: str) -> str:
    """The assistant message, or a RuntimeError that says what went wrong.

    An OpenAI-compatible gateway can answer HTTP 200 with an error OBJECT —
    LiteLLM does this for budget and rate-limit conditions. Indexing straight
    into `choices` then raised `KeyError: 'choices'` and threw away the actual
    message; a caller saw an opaque KeyError where the gateway had said
    "budget exceeded". Verified against a stub gateway returning exactly that.
    """
    if not isinstance(data, dict):
        raise RuntimeError(f"gateway returned {type(data).__name__}, not an object")
    err = data.get("error")
    if err:
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise RuntimeError(f"gateway rejected the request for model {model!r}: {msg}")
    choices = data.get("choices")
    if not choices:
        raise RuntimeError(
            f"gateway returned no choices for model {model!r}; "
            f"keys present: {sorted(data)}"
        )
    content = (choices[0].get("message") or {}).get("content")
    if content is None:
        # A tool-call-only or filtered response has no text. Returning None from
        # a function annotated `-> str` pushes the failure into the caller's
        # string handling, several frames from the cause.
        finish = choices[0].get("finish_reason")
        raise RuntimeError(
            f"gateway returned a choice with no text content "
            f"(finish_reason={finish!r}) for model {model!r}"
        )
    return content


def configured() -> bool:
    """True when a usable gateway URL is set. Costs nothing, touches nothing."""
    if not gateway():
        return False
    return urllib.parse.urlparse(gateway()).scheme.lower() in _ALLOWED_SCHEMES


def available(probe: bool = False) -> bool:
    """Whether the gateway can be used.

    `probe=False` (the default) only checks configuration. The old behaviour was
    to run a real chat completion every call while the docstring called it a
    "cheap probe" — it is a billable inference request, and nothing in the repo
    called it, so the cost was latent rather than absent. Ask for the live check
    explicitly when the answer is worth paying for.
    """
    if not configured():
        return False
    if not probe:
        return True
    try:
        chat("reply with the single word: ok", max_tokens=5, timeout=10)
        return True
    except Exception:
        return False
