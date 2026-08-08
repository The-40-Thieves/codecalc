"""The runtime self-update tools over MCP. Dry-run only — nothing mutating.

This file used to print the numbers it received and exit 0. Measured: injecting
`summary.total = -999` left it exiting 0 and printing `total = -999`. It failed
only when a key went missing, so it detected a broken SHAPE and never a wrong
VALUE.

The fix is not "assert total == 33". That number depends on what is installed on
the machine, so pinning it makes the test fail on a different host for a reason
that is not a defect. The summary is instead checked against the data it
summarises — a count that disagrees with the thing it counts is wrong on any
host, and that is exactly what the injection produced.

`update_runtimes` is called in dry-run and the assertions confirm it stayed that
way: a test for an updater that actually updated the machine's toolchain would
be worse than no test.
"""

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _mcp_client import data, over_stdio

FAILS: list[str] = []

#: A version, as opposed to a status string like "apt-managed" or "up to date".
_VERSION = re.compile(r"^v?\d+(\.\d+)*")


def _is_version(value: object) -> bool:
    return isinstance(value, str) and bool(_VERSION.match(value))


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


async def main():
    async with over_stdio() as client:
        names = sorted(t.name for t in (await client.list_tools()).tools)
        print(f"tools ({len(names)}): {names}")
        for required in ("runtimes_status", "update_runtimes"):
            check(f"tool {required!r} is served", required in names)

        st = data(await client.call_tool("runtimes_status", {}))
        check("runtimes_status succeeds", st.get("ok") is not False, f"-> {str(st)[:80]}")

        languages = st.get("languages") or {}
        summary = st.get("summary") or {}
        check("runtimes_status reports per-language detail", bool(languages),
              f"-> {len(languages)} entries")

        # The check the old version could not make: the summary must agree with
        # the data it summarises. -999 fails this on every host; a correct count
        # passes on all of them.
        check("summary.total equals the number of languages reported",
              summary.get("total") == len(languages),
              f"-> total={summary.get('total')}, languages={len(languages)}")

        updatable = {k: v for k, v in languages.items() if v.get("updatable")}
        check("summary.updatable equals the number marked updatable",
              summary.get("updatable") == len(updatable),
              f"-> updatable={summary.get('updatable')}, marked={len(updatable)}")
        check("counts are non-negative",
              summary.get("total", -1) >= 0 and summary.get("updatable", -1) >= 0,
              f"-> {summary}")
        check("no more languages are updatable than exist",
              summary.get("updatable", 0) <= summary.get("total", 0), f"-> {summary}")

        # Every entry marked updatable must say what it would move BETWEEN.
        # "updatable" with no versions is a claim with nothing behind it.
        for name, info in updatable.items():
            check(f"{name}: updatable entry names current and latest",
                  bool(info.get("current")) and bool(info.get("latest")),
                  f"-> {info.get('current')!r} -> {info.get('latest')!r}")
            check(f"{name}: current and latest actually differ",
                  info.get("current") != info.get("latest"),
                  f"-> both {info.get('current')!r}")

        # Anything NOT marked updatable must not be hiding a pending upgrade —
        # but only where both fields are actually VERSIONS. They are not always:
        # an apt-managed runtime reports current="apt-managed", latest="up to
        # date", and a first draft of this check asserted equality across all of
        # them and failed 15 entries that were perfectly correct. The invariant
        # holds for version strings; for status strings there is nothing to
        # compare.
        for name, info in languages.items():
            if info.get("updatable"):
                continue
            cur, latest = info.get("current"), info.get("latest")
            if _is_version(cur) and _is_version(latest):
                check(f"{name}: not-updatable means current == latest", cur == latest,
                      f"-> {cur!r} vs {latest!r}")

        check("runtimes_status is read-only", st.get("dry_run") is True,
              f"-> dry_run={st.get('dry_run')}")

        # ── update_runtimes, dry-run ───────────────────────────────────────
        up = data(await client.call_tool("update_runtimes", {"languages": "gradle,swift"}))
        check("update_runtimes defaults to a dry run", up.get("dry_run") is True,
              f"-> {up.get('dry_run')}")
        check("  ...and says so in the message",
              "dry run" in (up.get("message") or "").lower(),
              f"-> {(up.get('message') or '')[:60]!r}")
        check("update_runtimes reports nothing as applied",
              not up.get("updated"), f"-> {up.get('updated')!r}")

        planned = up.get("languages") or {}
        check("update_runtimes plans only what was asked for",
              set(planned) <= {"gradle", "swift"}, f"-> {sorted(planned)}")
        for name, info in planned.items():
            cmd = info.get("update_command") or ""
            check(f"{name}: the plan includes a command to run", bool(cmd), f"-> {cmd!r}")
            # A dry run that has already executed is not a dry run.
            check(f"{name}: the command was not executed", info.get("applied") is not True,
                  f"-> applied={info.get('applied')!r}")


asyncio.run(main())
print(f"\n=== {len(FAILS)} FAILURE(S) ===" if FAILS else "\n=== RUNTIME TOOLS OK (dry-run only) ===")
sys.exit(1 if FAILS else 0)
