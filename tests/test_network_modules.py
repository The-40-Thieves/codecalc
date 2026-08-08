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
import os
import pathlib
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import context7, llm

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


def online() -> bool:
    try:
        urllib.request.urlopen("https://context7.com/api/v2/search?query=numpy", timeout=10)
    except (urllib.error.URLError, OSError):
        return False
    return True


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

if online():
    r = context7.discover("numpy")
    check("discover() actually returns results", r.get("ok") is True,
          f"-> {str(r.get('error'))[:80]}")
    check("  ...and they look like a catalog", "results" in (r.get("results") or {}),
          f"-> {str(r.get('results'))[:70]}")
    d = context7.docs("/numpy/numpy", "array creation")
    check("docs() flags a clipped answer",
          d.get("ok") and d.get("truncated") is True and d.get("content_chars") == context7.MAX_CONTENT_CHARS,
          f"-> truncated={d.get('truncated')} chars={d.get('content_chars')}")
else:
    skip("live context7 checks", "no network")

# ═══ llm: the endpoint was frozen at import ═══════════════════════════════
# CODECALC_LLM_MODEL was re-read per call and CODECALC_LLM_GATEWAY was not, so
# the two halves of one configuration disagreed about when they were read.
saved = os.environ.get("CODECALC_LLM_GATEWAY")
try:
    os.environ.pop("CODECALC_LLM_GATEWAY", None)
    check("no gateway configured -> configured() is False", llm.configured() is False)
    os.environ["CODECALC_LLM_GATEWAY"] = "http://127.0.0.1:9/v1/chat/completions"
    check("a gateway set AFTER import is picked up", llm.configured() is True)

    # ═══ llm: urlopen speaks file:, ftp: and data: too ════════════════════
    # Verified before the fix: a gateway of file:///etc/hostname was opened and
    # read, returning the machine's hostname as the "model response".
    for bad in ("file:///etc/hostname", "ftp://example.com/x", "data:,hello", "/etc/hostname"):
        os.environ["CODECALC_LLM_GATEWAY"] = bad
        try:
            llm.chat("hi", timeout=5)
            ok = False
            detail = "*** the call went through"
        except RuntimeError as exc:
            ok = "refusing to use" in str(exc)
            detail = str(exc)[:60]
        except Exception as exc:
            ok = False
            detail = f"{type(exc).__name__}: {exc}"
        check(f"gateway {bad!r} is refused", ok, f"-> {detail}")
        check(f"  ...and configured() agrees about {bad!r}", llm.configured() is False)

    # An allowlist checked only at the configured endpoint is one HTTP 302 away
    # from irrelevant: urllib follows an http:// redirect to ftp:// happily.
    class Redirector(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(302)
            self.send_header("Location", "ftp://example.com/x")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    rsrv = HTTPServer(("127.0.0.1", 0), Redirector)
    threading.Thread(target=rsrv.serve_forever, daemon=True).start()
    os.environ["CODECALC_LLM_GATEWAY"] = f"http://127.0.0.1:{rsrv.server_address[1]}/v1"
    try:
        llm.chat("hi", timeout=8)
        ok, detail = False, "followed it"
    except Exception as exc:
        ok, detail = "refusing to follow a redirect" in str(exc), str(exc)[:70]
    check("a redirect to a disallowed scheme is refused", ok, f"-> {detail}")
    rsrv.shutdown()

    # ═══ llm: a 200 carrying an error object raised KeyError('choices') ═══
    # LiteLLM answers this way for budget and rate-limit conditions, so the
    # message that mattered was the one being discarded.
    CASES = [
        ("error object", {"error": {"message": "budget exceeded", "type": "billing"}},
         "budget exceeded"),
        ("no choices", {"id": "x", "model": "m"}, "no usable choices"),
        ("null content",
         {"choices": [{"message": {"content": None}, "finish_reason": "tool_calls"}]},
         "no text content"),
    ]
    # A gateway is a third party: every level of the response is type-checked.
    # `choices` as a dict raised KeyError(0), `[1]` raised AttributeError, and a
    # numeric content was returned as-is from a function annotated `-> str`.
    MALFORMED = [
        ("choices is a list of ints", {"choices": [1]}, "not an object"),
        ("choices is a string", {"choices": "x"}, "no usable choices"),
        ("choices is a dict", {"choices": {"0": {}}}, "no usable choices"),
        ("message is a string", {"choices": [{"message": "x"}]}, "not an object"),
        ("content is a number", {"choices": [{"message": {"content": 3}}]}, "not a string"),
    ]
    for label, payload, expect in MALFORMED:
        try:
            llm._content(payload, "m")
            ok, detail = False, "returned normally"
        except RuntimeError as exc:
            ok, detail = expect in str(exc), str(exc)[:70]
        except Exception as exc:
            ok, detail = False, f"*** {type(exc).__name__}: {exc}"
        check(f"malformed response: {label}", ok, f"-> {detail}")

    for label, payload, expect in CASES:
        srv = stub_gateway(payload)
        os.environ["CODECALC_LLM_GATEWAY"] = f"http://127.0.0.1:{srv.server_address[1]}/v1/chat/completions"
        try:
            llm.chat("hi", timeout=5)
            ok, detail = False, "returned normally"
        except RuntimeError as exc:
            ok, detail = expect in str(exc), str(exc)[:80]
        except Exception as exc:
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        check(f"{label}: the gateway's own message survives", ok, f"-> {detail}")
        srv.shutdown()

    srv = stub_gateway({"choices": [{"message": {"content": "hello"}}]})
    os.environ["CODECALC_LLM_GATEWAY"] = f"http://127.0.0.1:{srv.server_address[1]}/v1/chat/completions"
    check("a normal completion still works", llm.chat("hi", timeout=5) == "hello")
    srv.shutdown()

    # ═══ llm: available() billed for inference and called itself cheap ════
    os.environ["CODECALC_LLM_GATEWAY"] = "http://127.0.0.1:9/v1/chat/completions"
    asrc = inspect.getsource(llm.available)
    check("available() does not call the model by default", "probe" in asrc)
    check("available() is a config check unless asked otherwise",
          llm.available() is True, "-> nothing listening on port 9, yet True")
    check("available(probe=True) really tries", llm.available(probe=True) is False)

    # Fixing gateway() was not enough: complexity.py still gated LLM refinement
    # on the frozen module constant, so a gateway configured after import left
    # refinement silently off with the variable plainly set.
    from codecalc import complexity

    csrc = inspect.getsource(complexity)
    check("complexity.py gates on llm.configured(), not the frozen constant",
          "llm.GATEWAY" not in csrc.replace("# llm.configured(), not llm.GATEWAY", ""),
          "-> a stale read of the import-time constant remains")
finally:
    if saved is None:
        os.environ.pop("CODECALC_LLM_GATEWAY", None)
    else:
        os.environ["CODECALC_LLM_GATEWAY"] = saved

print(f"\n=== {len(FAILS)} FAILURE(S), {len(SKIPS)} skipped ===" if FAILS else
      f"\n=== NETWORK-MODULE REGRESSIONS FIXED ({len(SKIPS)} skipped) ===")
sys.exit(1 if FAILS else 0)
