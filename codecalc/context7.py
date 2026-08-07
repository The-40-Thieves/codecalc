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
import urllib.parse
import urllib.request

API = "https://context7.com/api/v2/context"

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
    "c": "/torvalds/linux",
    "cpp": "/gcc-mirror/gcc",
    "c++": "/gcc-mirror/gcc",
    "csharp": "/dotnet/runtime",
    "r": "/wch/r-source",
    "zig": "/ziglang/zig",
    "elixir": "/elixir-lang/elixir",
    "erlang": "/erlang/otp",
    "gleam": "/gleam-lang/gleam",
    "haskell": "/ghc/ghc",
    "fortran": "/flang-compiler/flang",
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


def _extract_imports(code: str, language: str) -> list[str]:
    """Best-effort import/library-name extraction per language."""
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
                "content": text[:12000]}
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
    return docs(libs[0], q, timeout=timeout)


def discover(query: str, timeout: int = 15) -> dict:
    """Search context7's library catalog."""
    params = urllib.parse.urlencode({"q": query})
    url = f"https://context7.com/api/v1/search?{params}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return {"ok": True, "results": json.loads(resp.read())}
    except Exception as exc:
        return {"ok": False, "error": f"context7 search unavailable: {exc}"}
