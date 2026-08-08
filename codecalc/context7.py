"""Context7 integration: up-to-date library documentation for any language.

Context7 (context7.com) indexes public library docs and serves LLM-reranked
snippets via a public API (keyless for basic use). This lets translate_code
hand the translating model *current* API knowledge for the target language
instead of relying on training-data memory of library APIs.

Library IDs follow /owner/repo (or /<source>/<id> for non-GitHub sources).
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request

from . import parsing

API = "https://context7.com/api/v2/context"

#: Context7 truncates nothing; this does. Reported alongside the content so a
#: caller can tell a complete answer from a clipped one — a live call against
#: /numpy/numpy returns exactly this many bytes, which is the tell that it was
#: cut rather than merely short.
MAX_CONTENT_CHARS = 12000

#: Floor for a single request once the shared budget is nearly spent. Below this
#: a request cannot realistically succeed, so the library is reported skipped
#: rather than issued and timed out.
_MIN_REQUEST_SECONDS = 5

#: language -> best-guess context7 library id for language-level docs
_LANG_LIBRARIES: dict[str, str] = {
    "python3": "/python/cpython",
    "node": "/nodejs/node",
    "bun": "/oven-sh/bun",
    "deno": "/denoland/deno",
    "typescript": "/microsoft/TypeScript",
    "go": "/golang/go",
    "rust": "/rust-lang/rust",
    "java": "/openjdk/jdk",
    "kotlin": "/JetBrains/kotlin",
    "swift": "/swiftlang/swift",
    "ruby": "/ruby/ruby",
    "php": "/php/php-src",
    # NOT /torvalds/linux: asking it for "core standard library usage for c"
    # returns kernel internals, which is the opposite of what a C translation
    # target needs. cppreference documents the C standard library itself.
    "c": "/cplusplus/cppreference",
    "cpp": "/gcc-mirror/gcc",
    "c++": "/gcc-mirror/gcc",
    "csharp": "/dotnet/runtime",
    "r": "/wch/r-source",
    "zig": "/ziglang/zig",
    "elixir": "/elixir-lang/elixir",
    "erlang": "/erlang/otp",
    "gleam": "/gleam-lang/gleam",
    "haskell": "/ghc/ghc",
    # flang is a COMPILER; the language's own docs are what a translator wants.
    "fortran": "/j3-fortran/fortran_proposals",
    "mojo": "/modularml/mojo",
}

#: common library/package names -> context7 library id (for import extraction)
_LIB_ALIASES: dict[str, str] = {
    # python
    "numpy": "/numpy/numpy",
    "pandas": "/pandas-dev/pandas",
    "scipy": "/scipy/scipy",
    "matplotlib": "/matplotlib/matplotlib",
    "requests": "/psf/requests",
    "flask": "/pallets/flask",
    "django": "/django/django",
    "fastapi": "/fastapi/fastapi",
    "pydantic": "/pydantic/pydantic",
    "sympy": "/sympy/sympy",
    "z3": "/Z3Prover/z3",
    "sqlalchemy": "/sqlalchemy/sqlalchemy",
    "asyncio": "/python/cpython",
    "re": "/python/cpython",
    "json": "/python/cpython",
    "os": "/python/cpython",
    "sys": "/python/cpython",
    # js / ts
    "lodash": "/lodash/lodash",
    "express": "/expressjs/express",
    "react": "/facebook/react",
    "axios": "/axios/axios",
    "moment": "/moment/moment",
    # go
    "fmt": "/golang/go",
    "strings": "/golang/go",
    "net/http": "/golang/go",
    "encoding/json": "/golang/go",
    # rust
    "serde": "/serde-rs/serde",
    "tokio": "/tokio-rs/tokio",
    "reqwest": "/seanmonstar/reqwest",
    "clap": "/clap-rs/clap",
}


#: tree-sitter node types that introduce a dependency, per grammar family.
_IMPORT_NODES = (
    "import_statement", "import_from_statement", "import_declaration",
    "import_spec", "use_declaration", "call_expression",
)


def _string_text(node, src: bytes) -> str | None:
    """Contents of a string node, quotes stripped."""
    text = src[node.start_byte:node.end_byte].decode("utf8", errors="replace")
    return text.strip("\"'`") or None


def _imports_via_parser(code: str, language: str) -> list[str] | None:
    """Imports according to a real parser, or None when no grammar covers it.

    Field-driven rather than "any dotted_name under an import node". That
    distinction is the whole point:

      * `import numpy as np` wraps the name in an `aliased_import`, which a
        direct-children scan walks straight past — the single most common way
        anyone imports numpy returned NOTHING.
      * `from myapp import numpy` has two `dotted_name` children, and only the
        `module_name` field is the dependency. Treating both as imports made
        `numpy` a dependency of a program that has none, and a false hit like
        that can fill the max_libs budget and crowd out a real one.

    The regex this replaced matched a line beginning with `import` anywhere,
    including inside a triple-quoted string, and took only the first entry of a
    grouped Go `import ( ... )` block.
    """
    parser = parsing.parser_for(language)
    if parser is None:
        return None
    try:
        tree = parser.parse(code.encode("utf8"))
    except Exception:
        return None

    src = code.encode("utf8")
    found: list[str] = []

    def text(node) -> str:
        return src[node.start_byte:node.end_byte].decode("utf8", errors="replace")

    def add(name: str | None) -> None:
        if not name:
            return
        name = name.strip("\"'`")
        if not name:
            return
        found.append(name)
        # Both the full path and its root: _LIB_ALIASES keys on either
        # ("net/http" and "numpy" are both entries, "serde::Serialize" is not).
        for sep in (".", "::", "/"):
            if sep in name:
                found.append(name.split(sep)[0])
                break

    def names_field(node, field: str):
        """Every child under `field`, across tree-sitter binding versions."""
        getter = getattr(node, "children_by_field_name", None)
        if getter is not None:
            return list(getter(field))
        one = node.child_by_field_name(field)
        return [one] if one is not None else []

    def walk(node) -> None:
        kind = node.type
        if node.is_named:
            if kind == "import_statement":
                # JS/TS: the module is the `source` string. Python: one or more
                # `name` fields, each possibly an aliased_import.
                source = node.child_by_field_name("source")
                if source is not None:
                    add(text(source))
                for n in names_field(node, "name"):
                    if n.type == "aliased_import":
                        inner = n.child_by_field_name("name")
                        add(text(inner if inner is not None else n))
                    else:
                        add(text(n))
            elif kind == "import_from_statement":
                mod = node.child_by_field_name("module_name")
                if mod is not None:
                    add(text(mod))
            elif kind == "import_spec":          # go, inside import_spec_list
                for child in node.children:
                    if child.is_named and "string" in child.type:
                        add(text(child))
            elif kind == "use_declaration":      # rust
                arg = node.child_by_field_name("argument")
                add(text(arg if arg is not None else node).rstrip(";"))
            elif kind == "import_declaration":   # java / kotlin (go wraps specs)
                for child in node.children:
                    if not child.is_named:
                        continue
                    if child.type in ("scoped_identifier", "identifier", "dotted_name"):
                        add(text(child))
            elif kind == "call_expression":
                # require("x") only — an ordinary call is not an import.
                fn = node.child_by_field_name("function")
                if fn is not None and text(fn) == "require":
                    args = node.child_by_field_name("arguments")
                    for a in (args.children if args is not None else []):
                        if a.is_named and "string" in a.type:
                            add(text(a))
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return [f for f in dict.fromkeys(found) if f]


def _extract_imports(code: str, language: str) -> list[str]:
    """Import/library names in `code`, by parser where one exists.

    The regex path remains for languages tree-sitter does not cover here; it is
    a heuristic and is documented as one rather than presented as extraction.
    """
    parsed = _imports_via_parser(code, language)
    if parsed is not None:
        return parsed
    return _extract_imports_regex(code, language)


def _extract_imports_regex(code: str, language: str) -> list[str]:
    """Heuristic fallback. Matches inside strings and comments; see above."""
    found: list[str] = []
    if language == "python3":
        pat = re.findall(r"^\s*(?:import|from)\s+([a-zA-Z_][\w.]*)", code, re.MULTILINE)
    elif language in ("node", "typescript", "bun", "deno"):
        pat = re.findall(r"(?:require\(|from\s+['\"])([a-zA-Z0-9_@/.-]+)", code)
    elif language == "go":
        pat = re.findall(r'import\s+\(?[\s\n]*"([^"]+)"', code) or \
            re.findall(r'import\s+"([^"]+)"', code)
    elif language == "rust":
        pat = re.findall(r"^\s*use\s+([a-zA-Z0-9_:]+)", code, re.MULTILINE)
    elif language in ("java", "kotlin"):
        pat = re.findall(r"^\s*import\s+([a-zA-Z0-9_.]+)", code, re.MULTILINE)
    else:
        pat = []
    for name in pat:
        root = name.split(".")[0]
        found.append(root)
        found.append(name)
    return [f for f in dict.fromkeys(found) if f]


def docs(library_id: str, query: str, fast: bool = True,
         timeout: int = 25) -> dict:
    """Fetch LLM-reranked documentation context for a library."""
    if not library_id.startswith("/"):
        library_id = "/" + library_id
    params = urllib.parse.urlencode({
        "libraryId": library_id, "query": query,
        "type": "txt", "fast": "true" if fast else "false",
    })
    url = f"{API}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "codecalc/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode(errors="replace")
        if text.strip().startswith("{") and '"error"' in text[:200]:
            try:
                err = json.loads(text)
                return {"ok": False, "error": err.get("message", text[:200])}
            except Exception:
                pass
        return {"ok": True, "library_id": library_id, "query": query,
                "content": text[:MAX_CONTENT_CHARS],
                "content_chars": min(len(text), MAX_CONTENT_CHARS),
                # A caller receiving exactly MAX_CONTENT_CHARS cannot otherwise
                # tell a complete answer from a clipped one.
                "truncated": len(text) > MAX_CONTENT_CHARS}
    except Exception as exc:
        return {"ok": False, "error": f"context7 unavailable: {exc}"}


def docs_for_language(language: str, query: str | None = None,
                      timeout: int = 25) -> dict:
    """Docs for a language's standard library (best-effort)."""
    lib = _LANG_LIBRARIES.get(language)
    if lib is None:
        return {"ok": False, "error": f"no context7 library mapped for '{language}'"}
    q = query or f"core standard library usage for {language}"
    return docs(lib, q, timeout=timeout)


def docs_for_code(code: str, language: str, query: str | None = None,
                  max_libs: int = 3, timeout: int = 25) -> dict:
    """Docs for libraries imported by `code` (best-effort, capped)."""
    imports = _extract_imports(code, language)
    libs: list[str] = []
    for imp in imports:
        lib = _LIB_ALIASES.get(imp)
        if lib and lib not in libs:
            libs.append(lib)
        if len(libs) >= max_libs:
            break
    if not libs:
        return {"ok": False, "error": "no known libraries detected in code"}
    q = query or "usage examples and API"
    # Every library found, not just the first. `max_libs` bounded a list whose
    # element [0] was then the only one fetched, so the parameter had no
    # observable effect at any value: code importing numpy, pandas and scipy
    # got numpy documentation and nothing said the other two were dropped.
    # `timeout` is the budget for the WHOLE call, divided across the requests.
    # Fetching every library was the fix for max_libs doing nothing, but giving
    # each request the full timeout turned a 25s worst case into 75s — and the
    # only caller, translate_code, already spends another 25s on language docs
    # and then has to fit an LLM round-trip inside a 120s MCP deadline. A fix
    # that blows the caller's budget is not a fix.
    chosen = libs[:max_libs]
    deadline = time.monotonic() + timeout
    results = []
    for lib in chosen:
        remaining = deadline - time.monotonic()
        if remaining < _MIN_REQUEST_SECONDS:
            results.append({"ok": False,
                            "error": f"skipped {lib}: {timeout}s budget spent"})
            continue
        results.append(docs(lib, q, timeout=max(_MIN_REQUEST_SECONDS, int(remaining))))
    ok = [r for r in results if r.get("ok")]
    return {
        "ok": bool(ok),
        "libraries": chosen,
        "results": results,
        "content": "\n\n".join(r.get("content", "") for r in ok),
        "failed": [r.get("error") for r in results if not r.get("ok")],
    }


def discover(query: str, timeout: int = 15) -> dict:
    """Search context7's library catalog.

    The parameter is `query`. It used to send `q`, which the API rejects with
    HTTP 400 `{"error":"validation_error","message":"Query is required to
    search for libraries"}` — so this function has never once returned a
    result, and the failure was swallowed into a structured error nobody read.
    Verified live against /api/v1/search and /api/v2/search, both of which
    answer 200 with `query`.
    """
    params = urllib.parse.urlencode({"query": query})
    url = f"https://context7.com/api/v2/search?{params}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "codecalc/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"ok": True, "results": json.loads(resp.read())}
    except Exception as exc:
        return {"ok": False, "error": f"context7 search unavailable: {exc}"}
