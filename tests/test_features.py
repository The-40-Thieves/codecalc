"""Verify the new feature set over MCP: sessions, files, artifacts, packages,
verdicts, limits, streaming, compact mode."""
import asyncio
import json
import sys

from fastmcp import Client

CONFIG = {
    "mcpServers": {
        "codecalc": {
            "command": sys.executable,
            "args": ["-m", "codecalc.server"],
            "env": {"PYTHONPATH": "/home/ubuntu/codecalc"},
        }
    }
}

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
    async with Client(CONFIG) as client:
        tools = await client.list_tools()
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
        check("compact mode", '"stdout": "z\\n"' in ct and '"cpu_ms"' not in ct, f"-> {ct[:80]}")

        # 6. streaming
        st = await client.call_tool("execute_code_stream", {"language": "python3",
                                                            "code": "import time\nfor i in range(3):\n print('tick', i); time.sleep(0.3)"})
        stt = await _txt(st)
        check("streaming returns result", "tick" in stt and "streamed_partial" in stt)

        # 7. package install (network; small pure-python pkg)
        p = await client.call_tool("install_package", {"language": "python3",
                                                       "package": "six", "session_id": sid})
        pt = await _txt(p)
        check("install_package six", '"ok": true' in pt, f"-> {pt[:120]}")

        # 8. cleanup
        await client.call_tool("session_stop", {"session_id": sid})

    print(f"\n=== {len(FAILS)} failures ===" if FAILS else "\n=== ALL NEW-FEATURE TESTS PASS ===")
    sys.exit(1 if FAILS else 0)


asyncio.run(main())
