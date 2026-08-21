"""THE-829: the Windows AppContainer strict backend, honestly bounded.

The isolation this backend is meant to deliver — a payload that cannot read the
user profile, cannot write outside its workdir, and gets no network — is only
observable on a real Windows 11 desktop. This suite therefore asserts EXACTLY
what is checkable from where it runs, and NOTHING it cannot see:

  * everywhere (source-scan): the AppContainer path is present, fails closed,
    cleans up, composes with the job topology, and discloses itself as
    UNVERIFIED via `appcontainer_isolation_unverified_on_windows`.
  * on Windows with the native executor (runtime): setting
    CODECALC_WIN_APPCONTAINER=1 makes the executor still produce valid JSON and
    either (a) emit the unverified-disclosure token, or (b) FAIL CLOSED with an
    error — never fall through to an unconfined run. That is the whole of what a
    Server-SKU CI runner can confirm.

What is DELIBERATELY NOT asserted here, because it is a Win11-box acceptance
item and claiming it from CI would be a fabrication: that the payload actually
cannot read user-profile secrets, and that `--no-net` egress is actually blocked.
Those stay unchecked until someone runs this on real Windows 11.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

FAILS: list[str] = []
SKIPS: list[str] = []

IS_WINDOWS = sys.platform.startswith("win")
EXE = REPO_ROOT / "bin" / ("codecalc-exec.exe" if os.name == "nt" else "codecalc-exec")
WINDOWS_RS = (REPO_ROOT / "executor" / "src" / "platform" / "windows.rs").read_text(
    encoding="utf-8"
)
DISCLOSURE = "appcontainer_isolation_unverified_on_windows"


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


def skip(name: str, why: str) -> None:
    print(f"SKIP {name} ({why})")
    SKIPS.append(name)


# ── source contract: runs on EVERY platform (the code is the same everywhere) ──
appc_path = WINDOWS_RS.split("fn prepare_appcontainer", 1)
check(
    "the AppContainer strict backend exists in the source",
    len(appc_path) == 2 and "fn create_appcontainer" in WINDOWS_RS,
    "-> code is present",
)
check(
    "it is gated OFF by default behind CODECALC_WIN_APPCONTAINER",
    'std::env::var("CODECALC_WIN_APPCONTAINER")' in WINDOWS_RS
    and ".unwrap_or(false)" in WINDOWS_RS,
    "-> the flag defaults to false",
)
check(
    "it uses the documented AppContainer profile/SID API",
    "CreateAppContainerProfile" in WINDOWS_RS
    and "DeriveAppContainerSidFromAppContainerName" in WINDOWS_RS
    and "DeleteAppContainerProfile" in WINDOWS_RS,
    "-> create/derive/delete are all wired",
)
check(
    "profile + SID cleanup runs even on the error path (RAII Drop)",
    "impl Drop for AppContainer" in WINDOWS_RS
    and "DeleteAppContainerProfile(self.name_w.as_ptr())" in WINDOWS_RS
    and "FreeSid(self.sid)" in WINDOWS_RS,
    "-> deterministic teardown",
)
check(
    "the runtime is launched with SECURITY_CAPABILITIES in the attribute list",
    "PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES" in WINDOWS_RS
    and "SECURITY_CAPABILITIES" in WINDOWS_RS
    and "EXTENDED_STARTUPINFO_PRESENT" in WINDOWS_RS,
    "-> STARTUPINFOEX + UpdateProcThreadAttribute path",
)
check(
    "the workdir is granted to the AC SID by an explicit ACL",
    "SetNamedSecurityInfoW" in WINDOWS_RS
    and "SetEntriesInAclW" in WINDOWS_RS
    and "grant_sid_path_access" in WINDOWS_RS,
    "-> only the workdir (+ read-only runtime dir) is granted",
)
check(
    "network is denied by default: no capability SIDs",
    "CapabilityCount: 0" in WINDOWS_RS and "Capabilities: std::ptr::null_mut()" in WINDOWS_RS,
    "-> the AppContainer gets no network capability",
)
check(
    "it FAILS CLOSED — profile/ACL failure aborts, never an unconfined launch",
    "let (ac, caps) = prepare_appcontainer(&cmd)?;" in WINDOWS_RS,
    "-> the `?` propagates the error instead of downgrading",
)
check(
    "it preserves the creation-time job assignment",
    "PROC_THREAD_ATTRIBUTE_JOB_LIST" in WINDOWS_RS
    and "spawn_with_job_at_creation" in WINDOWS_RS,
    "-> both attributes ride one attribute list",
)
check(
    "it preserves the stdio-only inherited-handle narrowing",
    "PROC_THREAD_ATTRIBUTE_HANDLE_LIST" in WINDOWS_RS,
    "-> handle inheritance is not regressed",
)
check(
    "the AppContainer path discloses itself as UNVERIFIED on Windows",
    f'unenforced.push("{DISCLOSURE}")' in WINDOWS_RS,
    "-> honesty token emitted when the path is taken",
)
# The disclosure must NOT be phrased as verified/working anywhere in the source.
lowered = WINDOWS_RS.lower()
check(
    "the source never claims the isolation is verified on Windows",
    "appcontainer_isolation_verified" not in lowered
    and "isolation confirmed on windows" not in lowered,
    "-> UNVERIFIED is the only claim made",
)

# ── runtime contract: Windows only, and tolerant of a fail-closed outcome ──────
if not IS_WINDOWS:
    skip(
        "AppContainer runtime smoke",
        "non-Windows: the AppContainer path is Windows-only; isolation is a "
        "Win11-box acceptance item and is left unchecked here",
    )
elif not EXE.exists():
    skip("AppContainer runtime smoke", "bin/codecalc-exec not built")
else:
    env = dict(os.environ)
    env["CODECALC_WIN_APPCONTAINER"] = "1"
    argv = [str(EXE), "--lang", "python3", "--timeout", "30"]
    proc = subprocess.run(
        argv,
        input="print('hello')",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env=env,
    )
    parsed: dict | None
    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError:
        parsed = None

    check(
        "with the flag on, the executor still produces valid JSON (no crash)",
        parsed is not None,
        f"-> stdout={proc.stdout[:160]!r} stderr={proc.stderr[:160]!r}",
    )

    if parsed is not None:
        unenforced = parsed.get("unenforced") or []
        emitted = DISCLOSURE in unenforced
        # A launch that could not build the AppContainer must fail closed, not
        # silently run unconfined. Both outcomes are honest; exactly one occurs.
        failed_closed = (
            parsed.get("ok") is False
            and "appcontainer" in json.dumps(parsed).lower()
        )
        check(
            "the AppContainer path was TAKEN: it either discloses unverified "
            "isolation or fails closed",
            emitted or failed_closed,
            f"-> ok={parsed.get('ok')} unenforced={unenforced}",
        )
        # We can confirm the DISCLOSURE, never the ISOLATION. State that boundary
        # in the log so nobody mistakes a green CI run for Win11 verification.
        skip(
            "payload cannot read user-profile secrets / --no-net egress blocked",
            "Win11-box acceptance criteria; a Server-SKU CI runner cannot exhibit "
            "the Win11 AppContainer behaviour",
        )


print(
    f"\n=== {len(FAILS)} FAILURE(S), {len(SKIPS)} skipped ==="
    if FAILS
    else f"\n=== APPCONTAINER CONTRACT HOLDS ({len(SKIPS)} skipped) ==="
)
sys.exit(1 if FAILS else 0)
