"""The `unenforced` array must tell the truth, on every platform.

The executor's cross-platform story rests on one promise: a ceiling it cannot
apply is NAMED in `unenforced` rather than silently skipped. That promise has
two halves and only one of them is easy.

    listed as unenforced   ->  fine, the caller was told
    NOT listed             ->  it had better actually bite

The second half is what this file checks, by making a program violate each
ceiling the running platform claims to enforce and confirming the sandbox stops
it. A limit that is set but not enforced looks identical to one that is enforced
until something tries to exceed it — which is exactly how
`memory_limit_not_enforced_on_macos` came to be a documented entry rather than a
surprise.

The per-platform vocabulary is also pinned against the Rust source, because the
README's platform table and the strings the executor actually emits are two
descriptions of one thing and nothing else compares them. `cpu_limit` was
reported as `unavailable_on_windows` for a while when Windows job objects have
supported a CPU ceiling since XP; the table said the same, so the two agreed
with each other and disagreed with Windows.
"""

from __future__ import annotations

import pathlib
import re
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from codecalc import executor

FAILS: list[str] = []
SKIPS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


def skip(name: str, why: str) -> None:
    print(f"SKIP {name} ({why})")
    SKIPS.append(name)


IS_LINUX = sys.platform.startswith("linux")
IS_MAC = sys.platform == "darwin"
IS_WINDOWS = sys.platform.startswith("win")

#: Every string the executor is allowed to put in `unenforced`, and where it
#: comes from. A value outside this set is either a typo or a new limitation
#: nobody wrote down — both worth failing over.
KNOWN_UNENFORCED = {
    # unix.rs — a soft limit clamped down by an existing hard limit
    "cpu_limit_clamped_to_hard_rlimit",
    "file_size_limit_clamped_to_hard_rlimit",
    "process_limit_clamped_to_hard_rlimit",
    "memory_limit_clamped_to_hard_rlimit",
    "process_limit_is_a_fixed_ceiling_not_measured",
    "memory_limit_not_enforced_on_macos",
    # RLIMIT_NPROC does not bind a process whose effective uid is 0: the
    # kernel exempts privileged processes, so as root the ceiling is set and
    # has no effect. Not an escape — running as root is a deployment error —
    # but the result used to stay silent about it, which reads as "applied".
    # Verified by running the executor under sudo, where it appears, and as an
    # ordinary uid, where it does not. The Python fallback carries the prose
    # equivalent and scripts/check_parity.py gates that both have one.
    "process_limit_not_enforced_for_uid_0",
    # windows.rs
    "cpu_limit_counts_user_time_only_on_windows",
    "open_file_limit_unavailable_on_windows",
    "file_size_limit_unavailable_on_windows",
    "no_net_unavailable_on_windows",
    # main.rs — the shim is missing, so --no-net would do nothing
    "no_net_requested_but_no_shim_available",
}

if not executor._rust:
    skip("platform contract", "no native executor built")
else:
    # ── the vocabulary in the source matches the vocabulary here ───────────
    rust_sources = [
        REPO_ROOT / "executor" / "src" / "main.rs",
        REPO_ROOT / "executor" / "src" / "platform" / "unix.rs",
        REPO_ROOT / "executor" / "src" / "platform" / "windows.rs",
    ]
    emitted: set[str] = set()
    for src in rust_sources:
        text = src.read_text(encoding="utf-8")
        # Two precise forms, not one loose one. A first draft used
        # `unenforced[^;]*?"..."` with DOTALL and swallowed whole statements
        # between an `unenforced` mention and the next string literal anywhere
        # in the file — it "found" 12 entries, two of which were paragraphs of
        # unrelated Rust.
        for m in re.finditer(r'unenforced\.push\(\s*"([^"]+)"\s*\)', text):
            emitted.add(m.group(1))
        for block in re.finditer(r'unenforced\s*=\s*vec!\[(.*?)\]', text, re.S):
            emitted.update(re.findall(r'"([^"]+)"', block.group(1)))
    check("the source emits at least one unenforced string", bool(emitted),
          f"-> the extractor found {len(emitted)}")
    unknown = emitted - KNOWN_UNENFORCED
    check("every unenforced string the source emits is documented here",
          not unknown, f"-> undocumented: {sorted(unknown)}")

    # ── a clean run on a capable platform claims nothing ───────────────────
    r = executor.execute("python3", "print(1)", timeout=15)
    reported = r.get("unenforced")
    check("a clean run returns an unenforced array", isinstance(reported, list),
          f"-> {reported!r}")
    check("everything reported is from the known vocabulary",
          set(reported or []) <= KNOWN_UNENFORCED, f"-> {reported}")
    if IS_LINUX:
        check("Linux enforces everything on a default run", reported == [],
              f"-> {reported}")

    # ── anything NOT reported unenforced must actually bite ────────────────
    # Each case makes a program exceed one ceiling. The assertion is that the
    # sandbox stopped it — not merely that the limit was set.
    def enforced(flag: str) -> bool:
        """Does this platform claim to enforce `flag` on this run?"""
        return not any(flag in u for u in (reported or []))

    r = executor.execute("python3", "import time; time.sleep(30)", timeout=3)
    check("the wall-clock timeout bites", r.get("timed_out") is True and r.get("verdict") == "TLE",
          f"-> verdict={r.get('verdict')} timed_out={r.get('timed_out')}")

    r = executor.execute("python3", 'print("x" * 300000)', max_output_kb=8, timeout=20)
    check("the output cap bites and is reported as OLE",
          r.get("verdict") == "OLE" and len(r.get("stdout") or "") < 20_000,
          f"-> verdict={r.get('verdict')} len={len(r.get('stdout') or '')}")

    cpu = executor.execute("python3", "x=0\nwhile True: x+=1", max_cpu=1, timeout=30)
    if enforced("cpu_limit"):
        # Killed on its CPU budget, not on the wall clock: timeout is 30s and it
        # must die around 1s of CPU.
        check("the CPU ceiling bites",
              cpu.get("ok") is False and cpu.get("timed_out") is not True,
              f"-> verdict={cpu.get('verdict')} cpu_ms={cpu.get('cpu_ms')} "
              f"timed_out={cpu.get('timed_out')}")
        check("  ...on CPU time, well inside the wall clock",
              (cpu.get("duration_ms") or 0) < 15_000,
              f"-> ran {cpu.get('duration_ms')}ms of a 30000ms timeout")
    else:
        skip("CPU ceiling", f"reported unenforced: {reported}")

    mem = executor.execute("python3", "b = bytearray(400_000_000); print(len(b))",
                           max_memory_mb=64, timeout=30)
    if enforced("memory_limit"):
        check("the memory ceiling bites",
              mem.get("ok") is False and "400000000" not in (mem.get("stdout") or ""),
              f"-> verdict={mem.get('verdict')} stdout={(mem.get('stdout') or '')[:40]!r}")
    else:
        # macOS accepts setrlimit(RLIMIT_AS) and ignores it, which is why this
        # is a documented entry rather than an assertion.
        skip("memory ceiling", f"reported unenforced: {reported}")

    # ── --no-net is honest in both directions ──────────────────────────────
    net = executor.execute(
        "python3",
        "import socket\n"
        "try:\n"
        "    s = socket.socket(); s.settimeout(4); s.connect(('1.1.1.1', 80))\n"
        "    print('EGRESS REACHED')\n"
        "except OSError as e:\n"
        "    print('blocked', e.errno)\n",
        no_net=True, timeout=25)
    net_unenforced = any("no_net" in u for u in (net.get("unenforced") or []))
    if net_unenforced:
        check("--no-net says so when it cannot be applied", True,
              f"-> {net.get('unenforced')}")
    else:
        check("--no-net blocks egress when it does NOT report itself unenforced",
              "EGRESS REACHED" not in (net.get("stdout") or ""),
              f"-> {(net.get('stdout') or '').strip()[:60]!r}")

    # ── the env allowlist must not make one platform second-class ──────────
    # It held 11 POSIX-oriented names and no Windows plumbing, so a process
    # started there had no SystemRoot — which winsock and crypto initialisation
    # need. `node` probed as available and returned empty output with ok=false
    # through the sandbox. Dropping a variable is a security decision; dropping
    # the ones that make the OS work is just a broken platform.
    allow = executor._ENV_ALLOWLIST
    for var in ("SystemRoot", "COMSPEC", "PATHEXT", "USERPROFILE"):
        check(f"the env allowlist carries {var} for Windows", var in allow)
    # And the things it exists to keep OUT are still out.
    for secret in ("GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "PYTHONPATH", "GEM_HOME"):
        check(f"the env allowlist still excludes {secret}", secret not in allow)

    # ── the README's platform table describes the same executor ────────────
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    check("the README documents a per-platform support table",
          "| Guarantee | Linux | macOS | Windows |" in readme)
    # The claim that broke: Windows reported the CPU ceiling as unavailable when
    # job objects have supported it since XP. Whatever the table says, it must
    # not contradict what windows.rs emits.
    win = (REPO_ROOT / "executor" / "src" / "platform" / "windows.rs").read_text(encoding="utf-8")
    check("windows.rs applies a CPU ceiling", "JOB_OBJECT_LIMIT_PROCESS_TIME" in win)
    check("  ...and the README no longer calls it unavailable",
          "| CPU-time ceiling | `RLIMIT_CPU` | `RLIMIT_CPU` | reported unenforced |" not in readme)

print(f"\n=== {len(FAILS)} FAILURE(S), {len(SKIPS)} skipped ===" if FAILS else
      f"\n=== PLATFORM CONTRACT HOLDS ({len(SKIPS)} skipped) ===")
sys.exit(1 if FAILS else 0)
