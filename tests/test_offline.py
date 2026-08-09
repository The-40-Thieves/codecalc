"""The codecalc PACKAGE opens no sockets of its own. This asserts that,
structurally, and is careful about what it does not assert.

The package used to contain two outbound paths: an LLM client that shipped user
code to a configured gateway, and a context7 documentation fetch. Both are gone.

The LLM went because the caller of this server IS a language model — asking it
to configure a second, weaker one to write translations was backwards, and it
made the two most distinctive tools the only two that failed on a fresh install.
context7 went for the same reason from the other direction: the calling model
already has documentation access of its own, so the tool duplicated a capability
its only user already had, in the one module that could still leak.

What remains is a calculator that executes code, measures it, and does exact
maths, with no outbound client of its own. That is a stronger property than "one
outbound request, and it's only documentation", and this file is what keeps it
true.

WHAT THIS FILE DOES NOT PROVE, stated because the banner used to overreach.
It reads the source of one Python package. It cannot say anything about what a
CHILD PROCESS does, and three tool paths deliberately reach the network through
one:

  - install_package runs uv/npm/gem/cargo, which fetch from their registries
  - runtimes_status / update_runtimes shell out to mise/rustup/swiftly/npm
  - executed code reaches the network unless no_net is requested AND the native
    executor is present to apply the shim

So "codecalc makes no network calls", the claim this banner used to make, is
false at the level a user cares about. "The package contains no outbound
client" is true, is what is actually asserted below, and is what the banner now
says. An operator who needs the stronger property has to enforce it at the
process or container boundary, which is outside anything this file can check.

These are STRUCTURAL assertions, deliberately. A test that watched for traffic
would only prove nothing happened to fire during the test; reading the source
proves there is nothing to fire. And unlike the suite this replaced, none of it
needs the internet — so it cannot skip, and a skip can never be mistaken for a
pass.
"""

from __future__ import annotations

import pathlib
import re
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

PKG = REPO_ROOT / "codecalc"

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


MODULES = sorted(PKG.glob("*.py"))
SOURCES = {p.name: p.read_text(encoding="utf-8") for p in MODULES}
check("the package has modules to scan", len(MODULES) > 10, f"-> {len(MODULES)}")

# ═══ nothing in the package can open a socket ══════════════════════════════
#: Every stdlib route to the network. `socket` is included even though the
#: sandbox's own no-net shim talks about it — the shim is C, and no Python
#: module here should be constructing sockets either.
NETWORK_IMPORTS = (
    "urllib.request", "urllib3", "http.client", "httplib", "requests",
    "httpx", "aiohttp", "socket", "ftplib", "smtplib", "telnetlib",
    "xmlrpc.client", "websockets",
)
for name, src in SOURCES.items():
    # Import statements only — a variable called `_requests`, or the word
    # "socket" inside a comment about the no-net shim, is not a network call.
    imports = re.findall(r"^\s*(?:import|from)\s+([\w.]+)", src, re.M)
    offending = sorted({i for i in imports if any(
        i == n or i.startswith(n + ".") for n in NETWORK_IMPORTS)})
    check(f"{name} imports nothing that can reach the network",
          not offending, f"-> {offending}")

# ═══ the two removed modules stay removed ══════════════════════════════════
for gone in ("llm.py", "context7.py"):
    check(f"codecalc/{gone} does not exist", not (PKG / gone).exists())

for mod in ("llm", "context7"):
    try:
        __import__(f"codecalc.{mod}")
        check(f"importing codecalc.{mod} fails", False, "-> it imported")
    except ImportError:
        check(f"importing codecalc.{mod} fails", True)

# ═══ no configuration surface for an outbound service ══════════════════════
ALL_SRC = " ".join(SOURCES.values())
#: A READ, not a mention. complexity.py's docstring names
#: CODECALC_COMPLEXITY_LLM to say it was removed, and a check that bans the
#: string fails on the sentence explaining the fix — the third time today an
#: assertion has matched prose instead of code.
_READS = re.compile(r"""environ(?:\.get)?\(?\[?\s*['"]([A-Z_]+)['"]""")
_read_vars = set(_READS.findall(ALL_SRC))
for var in ("CODECALC_LLM_GATEWAY", "CODECALC_LLM_API_KEY", "CODECALC_LLM_MODEL",
            "CODECALC_COMPLEXITY_LLM", "LITELLM_AGENT_KEY"):
    check(f"{var} is not read anywhere", var not in _read_vars,
          f"-> env vars actually read: {sorted(_read_vars)}")

# No hardcoded endpoint can survive either — check_portability.py already bans
# private addresses; this bans public ones in the package itself.
urls = sorted({u for u in re.findall(r"https?://[^\s\"'`)]+", ALL_SRC)
               if "example.com" not in u and "localhost" not in u
               and "127.0.0.1" not in u})
check("no outbound URL is embedded in the package", not urls, f"-> {urls[:3]}")

# ═══ the tools that replaced them need no network ══════════════════════════
# The verification gates run PROGRAMS, which is the point: the caller writes the
# translation or the optimisation, and the executor decides by running it.
from codecalc import optimization, server, translation

for fn in (translation.verify_translation, optimization.verify_optimization):
    src = pathlib.Path(fn.__code__.co_filename).read_text(encoding="utf-8")
    check(f"{fn.__name__} lives in a module with no network import",
          not any(n in src for n in ("urllib.request", "import requests", "import httpx")))

tool_count = SOURCES["server.py"].count("\n@mcp.tool")
check("the server still exposes its full tool surface", tool_count >= 47,
      f"-> {tool_count}")
for required in ("verify_translation", "verify_optimization", "execute_code",
                 "compare_edge_cases"):
    check(f"{required} is still served", hasattr(server, required))

# ═══ and the executor's own no-net shim is untouched ═══════════════════════
# Removing outbound calls from the PACKAGE says nothing about code the sandbox
# runs on a caller's behalf. That still reaches the network unless --no-net is
# passed, and the shim is what enforces it.
shim = REPO_ROOT / "executor" / "blocknet.c"
check("the sandbox's no-net shim still exists", shim.is_file())
check("  ...and still blocks only the network families",
      "AF_INET" in shim.read_text(encoding="utf-8"))

print(f"\n=== {len(FAILS)} FAILURE(S) ===" if FAILS else
      "\n=== THE codecalc PACKAGE CONTAINS NO OUTBOUND CLIENT ===")
sys.exit(1 if FAILS else 0)
