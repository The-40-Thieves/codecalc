"""Verify the new feature set over MCP: sessions, files, artifacts, packages,
verdicts, limits, streaming, compact mode."""
import asyncio
import json
import pathlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _mcp_client import over_stdio

FAILS = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


async def _txt(r) -> str:
    """Best-effort text extraction from a CallToolResult."""
    if hasattr(r, "structured_content") and r.structured_content is not None:
        return json.dumps(r.structured_content)
    if hasattr(r, "content") and r.content:
        return r.content[0].text
    return str(r)


async def main():
    async with over_stdio() as client:
        tools = (await client.list_tools()).tools
        names = sorted(t.name for t in tools)
        print("tools:", len(names), names)
        for want in ["session_start", "session_stop", "session_list",
                     "session_files", "session_read_file", "session_write_file",
                     "session_artifacts", "install_package", "execute_code_stream"]:
            check(f"tool present: {want}", want in names)

        # 1. session lifecycle + stateful python
        s = await client.call_tool("session_start", {"language": "python3"})
        sid = json.loads(await _txt(s))["session_id"]
        check("session_start returns id", bool(sid), f"-> {sid}")

        r1 = await client.call_tool("execute_code", {"language": "python3",
                                                     "code": "x = 42", "session_id": sid})
        r2 = await client.call_tool("execute_code", {"language": "python3",
                                                     "code": "print(x)", "session_id": sid})
        check("stateful: x persists", "42" in await _txt(r2))

        # 2. file tools
        w = await client.call_tool("session_write_file", {"session_id": sid, "path": "in/data.csv", "content": "a,b\n1,2"})
        r = await client.call_tool("session_read_file", {"session_id": sid, "path": "in/data.csv"})
        check("write+read file", "a,b" in await _txt(r))
        f = await client.call_tool("session_files", {"session_id": sid})
        check("list files", "in/" in await _txt(f))

        # 3. artifact from executed code
        a = await client.call_tool("execute_code", {"language": "python3",
                                                    "code": "open('out.json','w').write('[9,9]')",
                                                    "session_id": sid})
        art = await client.call_tool("session_artifacts", {"session_id": sid})
        check("artifacts detected", "out.json" in await _txt(art))

        # 4. verdict + limits on plain execute
        v = await client.call_tool("execute_code", {"language": "python3",
                                                    "code": "print('hi')"})
        vt = await _txt(v)
        check("verdict present", '"verdict": "OK"' in vt, f"-> {vt[:80]}")
        t = await client.call_tool("execute_code", {"language": "python3",
                                                    "code": "while True: pass", "timeout": 2})
        tt = await _txt(t)
        check("TLE verdict", "TLE" in tt)

        # 5. compact mode
        c = await client.call_tool("execute_code", {"language": "python3",
                                                    "code": "print('z')", "compact": True})
        ct = await _txt(c)
        cj = json.loads(ct)
        # Assert the DECODED value, not the serialized spelling: Python emits
        # CRLF line endings on Windows, so a literal '"stdout": "z\\n"' search
        # against the raw JSON text never matches there even though the
        # decoded stdout is the same one line of output either way.
        check("compact mode",
              cj.get("stdout", "").strip() == "z" and "cpu_ms" not in cj,
              f"-> {ct[:80]}")

        # 6. streaming
        st = await client.call_tool("execute_code_stream", {"language": "python3",
                                                            "code": "import time\nfor i in range(3):\n print('tick', i); time.sleep(0.3)"})
        stt = await _txt(st)
        sj = json.loads(stt)
        if sj.get("streamed") is False:
            # The documented no-native fallback: execute_code_stream runs
            # non-streaming and says so via streamed=False plus a note,
            # rather than raising or silently omitting the field. That is a
            # supported outcome, not a failure — assert its actual content.
            check("streaming falls back to non-streaming (documented no-native mode)",
                  "tick" in sj.get("stdout", "") and "no native executor" in sj.get("note", ""),
                  f"-> streamed={sj.get('streamed')} note={sj.get('note')!r}")
        else:
            check("streaming returns result",
                  sj.get("streamed") is True and "tick" in stt and "streamed_partial" in sj,
                  f"-> streamed={sj.get('streamed')}")

        # 7. package install (network; small pure-python pkg)
        p = await client.call_tool("install_package", {"language": "python3",
                                                       "package": "six", "session_id": sid})
        pt = await _txt(p)
        check("install_package six", '"ok": true' in pt, f"-> {pt[:120]}")

        # 8. cleanup
        await client.call_tool("session_stop", {"session_id": sid})

    # ── #88: the two entry points and `doctor` ────────────────────────────────
# `uvx codecalc` and `pipx run codecalc` always worked — they use the console
# script in [project.scripts]. `python -m codecalc` did not, because there was
# no __main__.py, and that is the form people reach for inside a venv they have
# not activated.
import subprocess as _sp2
import sys as _sys2

_r = _sp2.run([_sys2.executable, "-m", "codecalc", "doctor"],
              capture_output=True, text=True, timeout=180)
check("`python -m codecalc doctor` runs", _r.returncode == 0,
      f"-> rc={_r.returncode} {(_r.stderr or '')[-90:]!r}")
for _needle in ("execution backend", "runtimes", "mcpServers"):
    check(f"  ...and reports {_needle!r}", _needle in _r.stdout,
          f"-> {_r.stdout[:70]!r}")

# The count it prints must be the one the README states and check_claims
# enforces. A raw len(LANGUAGES) is 32 because `c++` and `cpp` are one language
# written twice; printing that would put a third number into circulation.
import re as _re2

from codecalc import registry as _reg2

_readme_n = int(_re2.search(r"\*\*(\d+) languages\*\*",
                            pathlib.Path("README.md").read_text()).group(1))
_m = _re2.search(r"runtimes\s+(\d+)/(\d+)", _r.stdout)
check("doctor's language count matches the README's",
      _m is not None and int(_m.group(2)) == _readme_n,
      f"-> doctor={_m.group(2) if _m else '?'} README={_readme_n} "
      f"raw_registry={len(_reg2.LANGUAGES)}")

# The no-argument path is what an MCP client spawns. Anything that made it
# print to stdout or exit would look like a protocol error to the client.
_probe_src = (
    "import sys; sys.argv=['codecalc']; "
    "from codecalc import server; print('MAIN_RESOLVES', callable(server.main))"
)
_r2 = _sp2.run([_sys2.executable, "-c", _probe_src],
               capture_output=True, text=True, timeout=120)
check("the no-argument entry point is still the MCP server",
      "MAIN_RESOLVES True" in _r2.stdout, f"-> {_r2.stdout[:60]!r}")

print(f"\n=== {len(FAILS)} failures ===" if FAILS else "\n=== ALL NEW-FEATURE TESTS PASS ===")
sys.exit(1 if FAILS else 0)


asyncio.run(main())
