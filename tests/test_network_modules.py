"""Regressions for the context7.py / llm.py sweep of 2026-08-08.

These were the last two modules with no test of any kind. Both talk to the
network, which is presumably why: a test that needs the internet is a test that
fails in CI for reasons unrelated to the code.

So almost nothing here needs it. The llm cases run against a stub HTTP server
started in-process, and the context7 cases exercise pure functions. Only two
checks are live, and they are SKIPPED rather than failed when the network is
absent — the point being that "we could not reach context7" and "context7
rejected our request" must not look the same, which is exactly the bug that let
`discover()` ship broken.
"""

from __future__ import annotations

import json
import pathlib
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import context7

FAILS: list[str] = []
SKIPS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


def skip(name: str, why: str) -> None:
    print(f"SKIP {name} ({why})")
    SKIPS.append(name)


def stub_gateway(payload: dict) -> HTTPServer:
    """A gateway that answers HTTP 200 with `payload`, whatever it contains."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


#: HTTP statuses that mean "the service answered, but not now" — our problem or
#: a passing server-side one, either way not something to fail a build over.
_TRY_AGAIN_LATER = {429, 500, 502, 503, 504}


def classify(exc: BaseException | None) -> str | None:
    """Why context7 is unusable, or None when it is usable.

    Split out as a pure function of the exception so the SUBCLASS TRAP below can
    be tested without a network. `HTTPError` is a subclass of `URLError`, so a
    single `except (URLError, OSError)` swallows a perfectly healthy server
    answering 429 and reports it as "no network" — which is what this suite did,
    while the service was replying in 91ms. A skip reason that misattributes the
    cause is how a rate limit gets read as an outage.
    """
    if exc is None:
        return None
    if isinstance(exc, urllib.error.HTTPError):
        # Reached it. The service is up; it declined.
        if exc.code in _TRY_AGAIN_LATER:
            return f"context7 answered HTTP {exc.code} (reachable, try again later)"
        return f"context7 answered HTTP {exc.code}"
    if isinstance(exc, (urllib.error.URLError, OSError)):
        return f"context7 unreachable: {exc}"
    return f"context7 probe failed: {type(exc).__name__}: {exc}"


def unusable() -> str | None:
    """Probe context7; None when the live checks can run."""
    try:
        urllib.request.urlopen("https://context7.com/api/v2/search?query=numpy", timeout=10)
    except Exception as exc:
        # Broad on purpose: classify() decides what it was, and a probe that
        # only catches the exceptions it expects is how the 429 got mislabelled.
        return classify(exc)
    return None


# ═══ context7: imports came from a regex, in a repo that has a parser ══════
# The regex matched any line beginning with `import`, INCLUDING inside a
# triple-quoted string — so a docstring mentioning `import pandas` made
# translate_code fetch pandas documentation for a program that never imports it.
DOCSTRING_TRAP = 'DOC = """\nimport pandas\nfrom numpy import array\n"""\nimport json\n'
got = context7._extract_imports(DOCSTRING_TRAP, "python3")
check("imports inside a string literal are not imports",
      not ({"pandas", "numpy", "array"} & set(got)), f"-> {got}")
check("  ...and the real import is still found", "json" in got, f"-> {got}")

# The regex version is kept as the fallback for languages with no grammar, so
# the difference stays visible rather than becoming folklore.
legacy = context7._extract_imports_regex(DOCSTRING_TRAP, "python3")
check("the regex fallback still shows the defect it was replaced for",
      bool({"pandas", "numpy"} & set(legacy)), f"-> {legacy}")

# A grouped Go import block yielded only its FIRST entry: the pattern matched
# once and `re.findall` never re-entered the parenthesised list.
GO = 'package main\n\nimport (\n\t"fmt"\n\t"strings"\n\t"net/http"\n\t"encoding/json"\n)\n'
go_imports = set(context7._extract_imports(GO, "go"))
for want in ("fmt", "strings", "net/http", "encoding/json"):
    check(f"go: grouped import {want!r} is found", want in go_imports, f"-> {sorted(go_imports)}")

check("node: both require() and import are found",
      {"lodash", "react"} <= set(context7._extract_imports(
          'const _ = require("lodash");\nimport R from "react";\n', "node")))
check("rust: use declarations are found",
      "serde" in context7._extract_imports("use serde::Serialize;\nuse tokio;\n", "rust"))
# require is a call; an ordinary call is not an import.
check("node: a plain function call is not an import",
      "compute" not in context7._extract_imports('const x = compute("lodash");\n', "node"))

# ═══ aliased and from-imports: found in review, both plainly wrong ════════
# `import numpy as np` wraps the name in an `aliased_import`, which a scan of an
# import node's DIRECT children walks straight past — the single most common way
# anyone imports numpy returned nothing at all.
check("aliased imports are found",
      context7._extract_imports("import numpy as np\n", "python3") == ["numpy"],
      f"-> {context7._extract_imports('import numpy as np', 'python3')}")

# `from myapp import numpy` has two dotted_name children and only the module is
# the dependency. Counting both made numpy a dependency of a program with none —
# and a false hit can fill the max_libs budget and crowd out a real library.
frm = context7._extract_imports("from myapp import numpy\n", "python3")
check("from-imports count the MODULE, not the imported names",
      "numpy" not in frm and "myapp" in frm, f"-> {frm}")
check("  ...and a dotted module keeps its root",
      set(context7._extract_imports("from a.b import c\n", "python3")) == {"a.b", "a"},
      f"-> {context7._extract_imports('from a.b import c', 'python3')}")

# ═══ context7: max_libs bounded a list whose [0] was the only one used ═════
import inspect

src = inspect.getsource(context7.docs_for_code)
check("docs_for_code no longer fetches only libs[0]", "libs[0]" not in src)
check("  ...and reports which libraries it used", '"libraries"' in src)
check("  ...and which ones failed", '"failed"' in src)

# Fetching every library was the fix for max_libs doing nothing — but giving
# each request the full timeout turned a 25s worst case into 75s, inside a
# caller with a 120s deadline that still has an LLM round-trip to make. The
# budget is shared, and libraries it cannot afford are REPORTED as skipped.
# A local socket that LISTENS but never replies. Better than pointing at an
# unroutable address: no external dependency, deterministic, and it does not put
# a hardcoded IP in the tree for scripts/check_portability.py to flag (it caught
# the first attempt, which used a 10/8 address).
_sink = socket.socket()
_sink.bind(("127.0.0.1", 0))
_sink.listen(8)
_api = context7.API
try:
    context7.API = f"http://127.0.0.1:{_sink.getsockname()[1]}/api/v2/context"
    _t0 = time.monotonic()
    r = context7.docs_for_code("import numpy\nimport pandas\nimport scipy\n",
                               "python3", max_libs=3, timeout=12)
    _elapsed = time.monotonic() - _t0
finally:
    context7.API = _api
    _sink.close()
check("docs_for_code shares one budget across libraries",
      _elapsed < 24, f"-> {_elapsed:.1f}s for 3 libraries at timeout=12")
check("  ...and says which ones it skipped",
      any("budget spent" in str(f) for f in (r.get("failed") or [])),
      f"-> {[str(f)[:40] for f in (r.get('failed') or [])]}")

# ═══ context7: docs() truncates, so it has to say so ══════════════════════
check("docs() reports truncation", "truncated" in inspect.getsource(context7.docs))
check("MAX_CONTENT_CHARS is named, not a literal buried in a slice",
      isinstance(context7.MAX_CONTENT_CHARS, int))

# ═══ context7: the C mapping pointed at the Linux kernel ══════════════════
check("c maps to the C standard library, not the kernel",
      "torvalds/linux" not in context7._LANG_LIBRARIES["c"],
      f"-> {context7._LANG_LIBRARIES['c']}")

# ═══ context7: discover() sent the wrong parameter name and always 400'd ══
dsrc = inspect.getsource(context7.discover)
check('discover() sends "query", the name the API requires',
      '"query": query' in dsrc and '"q": query' not in dsrc)

#: An error that means "we could not reach the service", as opposed to "the
#: service rejected us". The whole point of the discover() bug was that those
#: two looked identical; a test that conflates them is flaky rather than wrong,
#: which is worse — it trains you to ignore it. Observed once as a transient
#: failure between online() succeeding and the call being made.
UNREACHABLE = ("timed out", "temporary failure", "name resolution",
               "connection refused", "connection reset", "unreachable",
               "ssl", "eof occurred", "429", "too many requests",
               "500", "502", "503", "504")


def transient(err: object) -> bool:
    return any(s in str(err).lower() for s in UNREACHABLE)


def why(err: object) -> str:
    """The same distinction classify() makes, for an error that arrived as text.

    The probe was fixed to stop calling a 429 "no network" and these two skips
    were left saying `context7 unreachable: ... HTTP Error 429` — the identical
    misattribution one layer down. Fixing the first occurrence and not sweeping
    for the rest is how a corrected message survives as a wrong one.
    """
    m = re.search(r"HTTP Error (\d+)", str(err))
    if m:
        return f"context7 answered HTTP {m.group(1)} (reachable, try again later)"
    return f"context7 unreachable: {str(err)[:60]}"


# ── the probe must not call a rate limit an outage ────────────────────────
# Synthetic exceptions, so this pins the subclass trap with no network at all —
# the case that produced the wrong message needs a 429, which is not something
# to arrange on demand.
_fake_429 = urllib.error.HTTPError("https://context7.com/", 429, "Too Many Requests", {}, None)
_fake_down = urllib.error.URLError("Temporary failure in name resolution")
check("a 429 is reported as reachable, not as no network",
      "reachable" in (classify(_fake_429) or "") and "unreachable" not in (classify(_fake_429) or ""),
      f"-> {classify(_fake_429)}")
check("a real connection failure is reported as unreachable",
      "unreachable" in (classify(_fake_down) or ""), f"-> {classify(_fake_down)}")
check("a healthy probe classifies as usable", classify(None) is None)
check("HTTPError really is a URLError subclass (the trap this guards)",
      issubclass(urllib.error.HTTPError, urllib.error.URLError))
# The SAME distinction has to hold for errors that arrive as text from the
# context7 helpers, not just for exceptions from the probe.
check("a 429 in an error STRING is not called unreachable either",
      "unreachable" not in why("context7 search unavailable: HTTP Error 429: Too Many Requests"),
      f"-> {why('context7 search unavailable: HTTP Error 429: Too Many Requests')}")
check("a genuine connection error string still says unreachable",
      "unreachable" in why("context7 unavailable: <urlopen error [Errno -3] Temporary failure>"))

_why = unusable()
if _why:
    skip("live context7 checks", _why)
else:
    r = context7.discover("numpy")
    if not r.get("ok") and transient(r.get("error")):
        skip("discover() live check", why(r.get("error")))
        r = None
    else:
        check("discover() actually returns results", r.get("ok") is True,
              f"-> {str(r.get('error'))[:80]}")
    if r is not None:
        check("  ...and they look like a catalog", "results" in (r.get("results") or {}),
              f"-> {str(r.get('results'))[:70]}")
    d = context7.docs("/numpy/numpy", "array creation")
    if not d.get("ok") and transient(d.get("error")):
        skip("docs() live check", why(d.get("error")))
    else:
        check("docs() flags a clipped answer",
              d.get("ok") and d.get("truncated") is True
              and d.get("content_chars") == context7.MAX_CONTENT_CHARS,
              f"-> truncated={d.get('truncated')} chars={d.get('content_chars')}")

# ═══ the llm client is GONE, and must stay gone ═══════════════════════════
# codecalc no longer calls a language model anywhere. Its caller IS one, so a
# calculator that needed a second, separately configured model to run its two
# most distinctive tools had the dependency backwards — and those were the only
# two tools that did not work on a fresh install.
#
# The value was never the generation; it was the executor proving the result.
# That half is now callable directly as verify_translation / verify_optimization.
check("codecalc.llm no longer exists",
      not (REPO_ROOT / "codecalc" / "llm.py").exists())
try:
    from codecalc import llm  # noqa: F401
    check("importing codecalc.llm fails", False, "-> it imported")
except ImportError:
    check("importing codecalc.llm fails", True)

_src = " ".join(
    p.read_text(encoding="utf-8") for p in (REPO_ROOT / "codecalc").glob("*.py"))
for gone in ("CODECALC_LLM_GATEWAY", "CODECALC_LLM_API_KEY", "CODECALC_COMPLEXITY_LLM"):
    check(f"{gone} is not read anywhere", f'"{gone}"' not in _src and f"'{gone}'" not in _src)

# The one remaining network call is context7, which is a documentation API and
# not a model. Nothing else in the package opens a socket.
_net = [p.name for p in (REPO_ROOT / "codecalc").glob("*.py")
        if "urllib.request" in p.read_text(encoding="utf-8")]
check("only context7 makes network calls", _net == ["context7.py"], f"-> {_net}")

print(f"\n=== {len(FAILS)} FAILURE(S), {len(SKIPS)} skipped ===" if FAILS else
      f"\n=== NETWORK-MODULE REGRESSIONS FIXED ({len(SKIPS)} skipped) ===")
sys.exit(1 if FAILS else 0)
