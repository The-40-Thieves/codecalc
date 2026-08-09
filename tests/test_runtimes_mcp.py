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


# ── #28: apply=True must not report success it did not earn ────────────────
# Everything above goes over MCP/stdio, which cannot monkeypatch the SERVER
# subprocess's copy of codecalc.runtimes. These go straight at the module
# instead — unknown names, missing executables, nonzero exits, timeouts and
# partial success, entirely via dry_run and monkeypatched UPDATE_COMMANDS /
# _run, never a real update. Same rule test_python_sweep.py already follows
# for executor._rust: save the attribute, monkeypatch, restore in `finally`.
from codecalc import runtimes

# An unknown language must not enter apply mode at all: status() already
# reports ok=False for it, and update() used to ignore that and return
# {"ok": True, "dry_run": False, "executed": []} regardless.
r = runtimes.update("notalanguage", apply=True)
check("apply=True on an unknown language reports ok=False",
      r.get("ok") is False, f"-> {r}")
check("  ...and still names it unknown", r.get("unknown") == ["notalanguage"],
      f"-> {r.get('unknown')}")
check("  ...and executed nothing", not r.get("executed"), f"-> {r.get('executed')}")

# A language that IS recognised but whose updater binary is missing: the
# executed entry must carry the failure, and TOP-LEVEL ok must reflect it —
# it used to stay hardcoded True with exit_code=None sitting right there.
saved_cmd = dict(runtimes.UPDATE_COMMANDS)
runtimes.UPDATE_COMMANDS["mise"] = ["/nonexistent/bin/codecalc-no-mise", "up"]
try:
    st = runtimes.status("gradle")
    if st.get("languages", {}).get("gradle", {}).get("updatable"):
        r = runtimes.update("gradle", apply=True)
        check("a missing updater executable is reflected in ok=False",
              r.get("ok") is False, f"-> {r}")
        entry = (r.get("executed") or [{}])[0]
        check("  ...with exit_code None", entry.get("exit_code") is None,
              f"-> {entry.get('exit_code')}")
        check("  ...and ok=False on the entry itself", entry.get("ok") is False,
              f"-> {entry.get('ok')}")
    else:
        print("SKIP missing-updater regression (gradle not updatable on this host)")
finally:
    runtimes.UPDATE_COMMANDS.clear()
    runtimes.UPDATE_COMMANDS.update(saved_cmd)

# A nonzero exit from the updater is a real failure, not a shrug.
saved_cmd = dict(runtimes.UPDATE_COMMANDS)
runtimes.UPDATE_COMMANDS["mise"] = ["python3", "-c", "import sys; sys.exit(7)"]
try:
    st = runtimes.status("gradle")
    if st.get("languages", {}).get("gradle", {}).get("updatable"):
        r = runtimes.update("gradle", apply=True)
        check("a nonzero exit from the updater reports ok=False",
              r.get("ok") is False, f"-> {r}")
        check("  ...with the real exit code preserved",
              (r.get("executed") or [{}])[0].get("exit_code") == 7,
              f"-> {(r.get('executed') or [{}])[0].get('exit_code')}")
    else:
        print("SKIP nonzero-exit regression (gradle not updatable on this host)")
finally:
    runtimes.UPDATE_COMMANDS.clear()
    runtimes.UPDATE_COMMANDS.update(saved_cmd)

# A timeout from the updater is also a failure, not exit_code=None-and-ok=True.
saved_cmd = dict(runtimes.UPDATE_COMMANDS)
runtimes.UPDATE_COMMANDS["mise"] = ["python3", "-c", "import time; time.sleep(5)"]
try:
    st = runtimes.status("gradle")
    if st.get("languages", {}).get("gradle", {}).get("updatable"):
        r = runtimes.update("gradle", apply=True, timeout=1)
        check("a timed-out updater reports ok=False", r.get("ok") is False, f"-> {r}")
        check("  ...and says it timed out",
              "timed out" in ((r.get("executed") or [{}])[0].get("output_tail") or ""),
              f"-> {(r.get('executed') or [{}])[0].get('output_tail')!r}")
    else:
        print("SKIP timeout regression (gradle not updatable on this host)")
finally:
    runtimes.UPDATE_COMMANDS.clear()
    runtimes.UPDATE_COMMANDS.update(saved_cmd)

# Partial success — one manager's update succeeds, another's fails — must be
# represented truthfully: ok=False overall (not every requested action
# succeeded), with partial_failure set and each manager's own outcome intact,
# rather than collapsing to a single boolean that hides which half worked.
saved_cmd = dict(runtimes.UPDATE_COMMANDS)
runtimes.UPDATE_COMMANDS["mise"] = ["python3", "-c", "print('ok')"]
runtimes.UPDATE_COMMANDS["swiftly"] = ["/nonexistent/bin/codecalc-no-swiftly", "update"]
try:
    st = runtimes.status("gradle,swift")
    updatable = {n for n, i in st.get("languages", {}).items() if i.get("updatable")}
    if {"gradle", "swift"} <= updatable:
        r = runtimes.update("gradle,swift", apply=True)
        check("partial success reports ok=False overall", r.get("ok") is False, f"-> {r}")
        check("  ...and flags partial_failure", r.get("partial_failure") is True,
              f"-> {r.get('partial_failure')}")
        by_mgr = {e["manager"]: e for e in r.get("executed", [])}
        check("  ...with mise's own success preserved",
              by_mgr.get("mise", {}).get("ok") is True, f"-> {by_mgr.get('mise')}")
        check("  ...and swiftly's own failure preserved",
              by_mgr.get("swiftly", {}).get("ok") is False, f"-> {by_mgr.get('swiftly')}")
    else:
        print(f"SKIP partial-success regression (need gradle+swift updatable, have {sorted(updatable)})")
finally:
    runtimes.UPDATE_COMMANDS.clear()
    runtimes.UPDATE_COMMANDS.update(saved_cmd)

# An absent package manager must not read as "up to date" — the status
# checkers used to collapse every subprocess failure (including a missing
# binary) to the same empty string a healthy tool returns when it genuinely
# has nothing to report.
orig_run = runtimes._run


def _fake_run(cmd, timeout=60):
    if cmd and cmd[0] == "mise":
        return runtimes._RunResult(False, "", None, "mise: not found (simulated)")
    return orig_run(cmd, timeout=timeout)


runtimes._run = _fake_run
try:
    st = runtimes.status("gradle")
    check("a language whose manager is entirely unavailable is not ok=True",
          st.get("ok") is False, f"-> {st}")
    check("  ...and is not fabricated into languages",
          "gradle" not in (st.get("languages") or {}), f"-> {sorted(st.get('languages') or {})}")
    check("  ...and is distinguished from a typo via check_failed, not unknown",
          st.get("check_failed") == ["gradle"] and not st.get("unknown"),
          f"-> check_failed={st.get('check_failed')} unknown={st.get('unknown')}")
    check("  ...naming which manager and why", "manager_errors" in st and "mise" in st["manager_errors"],
          f"-> {st.get('manager_errors')}")
finally:
    runtimes._run = orig_run

print(f"\n=== {len(FAILS)} FAILURE(S) ===" if FAILS else "\n=== RUNTIME TOOLS OK (dry-run only) ===")
sys.exit(1 if FAILS else 0)
