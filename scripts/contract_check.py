#!/usr/bin/env python3
"""Assert the executor binary's JSON contract. Runs on Linux, macOS and Windows.

This replaces a bash heredoc that could only ever run on a POSIX runner, which
is a poor way to test a cross-platform claim. Everything here is stdlib Python
and asserts on OBSERVED output of the real binary rather than on its source.

The four fields checked are the ones codecalc/executor.py parses. If any changes
shape, the Python side silently degrades to
`{"ok": false, "error": "executor produced invalid output"}` with no clue why.

FLOOR: the run needs a working interpreter inside the sandbox, and which ones
exist differs per runner. Rather than hardcoding python3 — absent or differently
named on some Windows images — it probes, picks the first available, and FAILS if
none is. "No runtime available" must not read the same as "all checks passed".

Usage: python scripts/contract_check.py <path-to-codecalc-exec>
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

BIN = Path(sys.argv[1] if len(sys.argv) > 1 else "bin/codecalc-exec").resolve()

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


def run(args: list[str], stdin: str = "", timeout: int = 120) -> dict:
    p = subprocess.run([str(BIN), *args], input=stdin.encode(),
                       capture_output=True, timeout=timeout, check=False)
    out = p.stdout.decode(errors="replace")
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"__unparsable__": out[:300], "__stderr__": p.stderr.decode(errors="replace")[:300]}


if not BIN.is_file():
    print(f"::error::executor binary not found at {BIN}")
    print("::error::contract_check asserts the BUILT executor's JSON contract — "
          "build it first (see CONTRIBUTING.md 'Build the Rust core'), e.g. "
          "`cargo build --release --manifest-path executor/Cargo.toml` then copy "
          "the binary + its --no-net shim into bin/. Unlike the other scripts/ "
          "gates, this one is not a bare-checkout gate.")
    sys.exit(1)
print(f"binary : {BIN}")
print(f"platform: {sys.platform}")

# ── 1. the language catalogue ───────────────────────────────────────────────
langs = json.loads(subprocess.run([str(BIN), "--languages"], capture_output=True,
                                  check=True).stdout.decode())
check("--languages returns >= 30 entries", len(langs) >= 30, f"-> {len(langs)}")

# ── 2. runtime probe, and pick something we can actually execute ────────────
probe = json.loads(subprocess.run([str(BIN), "--probe"], capture_output=True,
                                  check=True).stdout.decode())
available = sorted(k for k, v in probe.items() if v)
print(f"available runtimes ({len(available)}): {', '.join(available[:14])}"
      f"{' …' if len(available) > 14 else ''}")

#: (language, program printing exactly "hello", program that spins forever)
CANDIDATES = [
    ("python3", 'print("hello")', "while True: pass"),
    ("node", 'console.log("hello")', "while(true){}"),
    ("ruby", 'puts "hello"', "loop {}"),
    ("perl", 'print "hello\\n";', "1 while 1;"),
    ("php", '<?php echo "hello\\n";', "<?php while(true){}"),
    ("lua", 'print("hello")', "while true do end"),
    ("deno", 'console.log("hello")', "while(true){}"),
]
picked = next(((l, ok_src, spin) for l, ok_src, spin in CANDIDATES if probe.get(l)), None)

if picked is None:
    # A gate that quietly passes when it could not execute anything is the
    # failure mode this repo keeps finding. Be loud instead.
    print("::error::no interpreter from the candidate list is available on this "
          f"runner; nothing could be executed. probe reported: {available}")
    sys.exit(1)

lang, hello_src, spin_src = picked
print(f"exercising: {lang}")

# ── 3. a successful run ─────────────────────────────────────────────────────
d = run(["--lang", lang, "--timeout", "60"], hello_src)
check("success: ok/verdict/stdout",
      d.get("ok") is True and d.get("verdict") == "OK" and d.get("stdout", "").strip() == "hello",
      f"-> verdict={d.get('verdict')} stdout={d.get('stdout', '')[:40]!r} "
      f"stderr={str(d.get('stderr'))[:80]!r}")
for k in ("exit_code", "duration_ms", "cpu_ms", "peak_memory_kb", "timed_out",
          "unenforced", "platform"):
    check(f"success: field {k!r} present", k in d)

#: The full key set of a successful run, captured before `d` is reused below.
#: Every other return path is compared against THIS rather than against its own
#: list of expected fields — see the compile-failure check at the end of the
#: file for why that distinction is the whole point.
SUCCESS_KEYS = set(d)

# peak_memory_kb is normalised to KiB on every platform. getrusage reports it in
# BYTES on macOS and KiB on Linux, so an unconverted macOS value shows up here as
# an absurd figure — a hello-world in any of these runtimes is nowhere near 1 GiB.
peak = d.get("peak_memory_kb", 0)
check("peak_memory_kb is KiB, not raw bytes", 0 < peak < 1_048_576, f"-> {peak} KiB")

# ── 4. the wall-clock kill ──────────────────────────────────────────────────
d = run(["--lang", lang, "--timeout", "3"], spin_src)
check("timeout: killed and reported TLE",
      d.get("timed_out") is True and d.get("verdict") == "TLE",
      f"-> verdict={d.get('verdict')} timed_out={d.get('timed_out')}")

# ── 5. the output cap ───────────────────────────────────────────────────────
BIG = {"python3": 'print("x" * 200000)', "node": 'console.log("x".repeat(200000))',
       "ruby": 'puts "x" * 200000', "perl": 'print "x" x 200000, "\\n";',
       "php": '<?php echo str_repeat("x", 200000), "\\n";',
       "lua": 'print(string.rep("x", 200000))',
       "deno": 'console.log("x".repeat(200000))'}[lang]
d = run(["--lang", lang, "--timeout", "60"], BIG)
check("output cap: truncated and reported OLE",
      len(d.get("stdout", "")) < 70_000 and d.get("verdict") == "OLE",
      f"-> {len(d.get('stdout', ''))} bytes, verdict={d.get('verdict')}")

# ── 6. an unknown language is structured, not a crash ───────────────────────
d = run(["--lang", "notalanguage", "--timeout", "10"], "x")
check("unknown language: structured error",
      d.get("ok") is False and "unknown language" in str(d.get("error", "")),
      f"-> {str(d.get('error'))[:60]!r}")

# ── 7. executed code cannot read the executor's environment ─────────────────
# AUDIT.md CRITICAL-02. The allowlist is enforced at spawn time, so it can only
# be verified by running the binary with a secret in ITS environment.
ENVSRC = {"python3": 'import os; print(os.environ.get("FAKE_API_KEY", "CLEAN"))',
          "node": 'console.log(process.env.FAKE_API_KEY || "CLEAN")',
          "ruby": 'puts ENV.fetch("FAKE_API_KEY", "CLEAN")',
          "perl": 'print(($ENV{FAKE_API_KEY} // "CLEAN") . "\\n");',
          "php": '<?php echo getenv("FAKE_API_KEY") ?: "CLEAN", "\\n";',
          "lua": 'print(os.getenv("FAKE_API_KEY") or "CLEAN")',
          "deno": 'console.log(Deno.env.get("FAKE_API_KEY") || "CLEAN")'}[lang]
os.environ["FAKE_API_KEY"] = "codecalc-ci-canary-do-not-leak"
d = run(["--lang", lang, "--timeout", "60"], ENVSRC)
got = d.get("stdout", "").strip()
check("env allowlist: canary did not reach the child",
      "codecalc-ci-canary" not in got, f"-> {got[:50]!r}")

# ── every return path must have the SAME shape ──────────────────────────────
# The compile-FAILURE return carried 13 keys where a successful run carried 16,
# missing total_ms/platform/workdir. A caller reading result["workdir"] got a
# KeyError whose only cause was that the submitted program did not compile,
# which is an ordinary outcome and not an exceptional one.
#
# Asserted as SET EQUALITY against the successful run, deliberately, rather than
# as another list of expected fields. A per-path field list is exactly what let
# this diverge: both paths satisfied their own list, and nobody ever compared
# the two lists to each other. Set equality cannot drift that way, and it also
# catches a field added to one path and forgotten on the other.
COMPILED_BROKEN = [
    ("c", "this is not valid c"),
    ("cpp", "this is not valid c++"),
    ("rust", "this is not valid rust"),
]
broken = next(((lg, src) for lg, src in COMPILED_BROKEN if probe.get(lg)), None)
if broken is None:
    # Named, not silent: this runner cannot exercise the compile-failure path at
    # all, which is different from exercising it and finding it correct.
    print("SKIP compile-failure shape: no compiled language on this runner "
          f"(probe reported: {', '.join(available[:10])})")
else:
    blang, bsrc = broken
    fd = run(["--lang", blang, "--timeout", "60"], bsrc)
    check(f"compile failure ({blang}): reported as a failure, not a crash",
          fd.get("ok") is False and fd.get("phase") == "compile",
          f"-> ok={fd.get('ok')} phase={fd.get('phase')}")
    missing, extra = SUCCESS_KEYS - set(fd), set(fd) - SUCCESS_KEYS
    check(f"compile failure ({blang}): identical key set to a successful run",
          not missing and not extra,
          f"-> missing={sorted(missing)} unexpected={sorted(extra)}")

print(f"\n=== {len(FAILS)} FAILURE(S) ===" if FAILS else
      "\n=== EXECUTOR CONTRACT HOLDS ===")
sys.exit(1 if FAILS else 0)
