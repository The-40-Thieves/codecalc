"""Network-capable auth adapters, isolated the same way `provider_adapters/` is.

`tests/test_offline.py` structurally bans a socket-capable import from any
top-level `codecalc/*.py` module — it globs `codecalc/*.py`, not
`codecalc/**/*.py`, so a subdirectory is the isolation boundary, the same one
`codecalc/provider_adapters/piston.py` already uses for its own outbound HTTP.

`codecalc/server.py` never imports this package at module scope. It is
imported lazily, inside `_http_auth()`, only when `CODECALC_OAUTH_ISSUER` (or
`--oauth-issuer`) is actually set — activated only by explicit configuration,
same as the Piston/strict-execution adapters.
"""

from __future__ import annotations
