"""Verify items 1-4: MCP resources, inline images, session_run, units."""
import asyncio
import json
import pathlib
import sys

from fastmcp import Client

CONFIG = {
    "mcpServers": {
        "codecalc": {
            "command": sys.executable,
            "args": ["-m", "codecalc.server"],
            "env": {"PYTHONPATH": str(pathlib.Path(__file__).resolve().parents[1])},
        }
    }
}

FAILS = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


async def _txt(r) -> str:
    if hasattr(r, "structured_content") and r.structured_content is not None:
        return json.dumps(r.structured_content)
    if hasattr(r, "content") and r.content:
        return r.content[0].text
    return str(r)


async def main():
    async with Client(CONFIG) as client:
        tools = await client.list_tools()
        names = sorted(t.name for t in tools)
        print("tools:", len(names))
        for w in ["session_run", "convert_units", "physical_constants", "list_units"]:
            check(f"tool present: {w}", w in names)

        # 1. session + multi-file program (session_run)
        s = await client.call_tool("session_start", {"language": "python3"})
        sid = json.loads(await _txt(s))["session_id"]
        await client.call_tool("session_write_file", {
            "session_id": sid, "path": "helper.py",
            "content": "def double(x):\n    return x * 2\n"})
        await client.call_tool("session_write_file", {
            "session_id": sid, "path": "main.py",
            "content": "import sys\nfrom helper import double\nn = int(sys.stdin.readline())\nprint(double(n))\n"})
        r = await client.call_tool("session_run", {"session_id": sid, "entry_file": "main.py", "stdin": "5\n"})
        t = await _txt(r)
        d = json.loads(t)
        check("session_run multi-file", d.get("ok") and "10" in d.get("stdout", ""),
              f"-> {d.get('stdout','').strip()}")

        # 2. artifact + inline image (stdlib-generated PNG — no matplotlib dep
        #    in the sandbox python; a real 1x1 red pixel)
        img = await client.call_tool("execute_code", {
            "language": "python3", "session_id": sid,
            "code": "import struct, zlib\n"
                    "def png():\n"
                    "    sig = b'\\x89PNG\\r\\n\\x1a\\n'\n"
                    "    w = h = 1\n"
                    "    ihdr = struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0)\n"
                    "    raw = b'\\x00' + b'\\xff\\x00\\x00'\n"
                    "    idat = zlib.compress(raw)\n"
                    "    def chunk(typ, data):\n"
                    "        c = struct.pack('>I', len(data)) + typ + data\n"
                    "        return c + struct.pack('>I', zlib.crc32(typ + data) & 0xffffffff)\n"
                    "    return sig + chunk(b'IHDR', ihdr) + chunk(b'IDAT', idat) + chunk(b'IEND', b'')\n"
                    "open('plot.png', 'wb').write(png())\n"})
        art = await client.call_tool("session_artifacts", {"session_id": sid})
        at = await _txt(art)
        check("PNG artifact produced", "plot.png" in at, f"-> {[a['path'] for a in json.loads(at)['artifacts']]}")
        img_r = await client.call_tool("session_read_file", {"session_id": sid, "path": "plot.png"})
        is_img = any(getattr(c, "type", "") == "image" for c in (img_r.content or []))
        check("inline image content type", is_img)

        # 3. MCP resource read
        res = await client.read_resource(f"codecalc://session/{sid}/files/helper.py")
        check("MCP resource read", "double" in (res[0].text if res else ""), f"-> {str(res)[:60]}")

        # 4. units
        u = await client.call_tool("convert_units", {"value": 60, "from_unit": "mph", "to_unit": "km/h"})
        ut = json.loads(await _txt(u))
        check("convert mph->km/h", ut.get("ok") and abs(ut["value"] - 96.56064) < 0.01,
              f"-> {ut.get('value')}")
        c = await client.call_tool("physical_constants", {"name": "speed_of_light"})
        ct = json.loads(await _txt(c))
        check("constant c", ct.get("value") == 299792458.0, f"-> {ct.get('value')}")
        lu = await client.call_tool("list_units", {})
        lut = json.loads(await _txt(lu))
        check("list_units", lut.get("count", 0) > 50, f"-> {lut.get('count')} aliases")

        # cleanup
        await client.call_tool("session_stop", {"session_id": sid})

    print(f"\n=== {len(FAILS)} failures ===" if FAILS else "\n=== ITEMS 1-4 ALL PASS ===")
    sys.exit(1 if FAILS else 0)


asyncio.run(main())
