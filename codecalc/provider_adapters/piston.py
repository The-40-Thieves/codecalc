"""Piston v2 HTTP wire transport."""

from __future__ import annotations

import json
from collections.abc import Callable
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class PistonHTTPTransport:
    """Small JSON transport for Piston, injectable at the network boundary."""

    def __init__(self, base_url: str, *, opener: Callable = urlopen) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Piston base_url must be an absolute HTTP(S) URL")
        self.base_url = base_url.rstrip("/")
        self._opener = opener

    def __call__(self, method: str, path: str, headers: dict[str, str],
                 payload: dict | None, timeout: int) -> object:
        body = json.dumps(payload).encode() if payload is not None else None
        request_headers = dict(headers)
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        request = Request(  # noqa: S310 -- constructor rejects non-HTTP(S) URLs
            f"{self.base_url}/{path.lstrip('/')}",
            data=body,
            headers=request_headers,
            method=method,
        )
        with self._opener(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
