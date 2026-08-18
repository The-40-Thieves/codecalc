"""Confine a process to a set of paths, on macOS, via `sandbox-exec` (Seatbelt).

LAYER 2 of the #23 mitigation on macOS. Layer 1 stops the package managers from
running third-party code at all (`--ignore-scripts` and friends in
packages.py). This is for what still runs: the manager itself, and anything a
future entry cannot disable. It is the macOS counterpart to `landlock.py`,
which does the same job on Linux through a different kernel mechanism —
THE-819 closed the gap `landlock.abi_version()` returning 0 on Darwin left
open: an installer ran with the server user's full filesystem access on
macOS, with only the honest disclosure token
(`package_install_not_confined_no_landlock`) to say so.

WHY sandbox-exec AND NOT SOMETHING ELSE: it is deprecated (Apple has shipped no
replacement CLI for third-party use since removing the `Sandbox.framework`
public API) but still functional on every currently-shipping macOS release, and
it needs no entitlements, no code signing and no daemon — `-f <profile> command
args...` from an ordinary unprivileged process. The alternatives all cost more
than this bug is worth closing twice: the App Sandbox entitlement path requires
a signed, notarized app; a helper written against the Hardened Runtime is a
different program shape than "spawn a subprocess".

PROFILE SHAPE: `(deny default)`, matching `_landlock_confinement`'s own
posture (reads scoped to an explicit allowlist, not "everything"), NOT the
broader `(allow default)` + narrow `(deny file-write*)` shape some build
tools use for this same job. The stricter shape is deliberate here: the
confinement test (`tests/test_package_isolation.py`) asserts a same-UID
canary file OUTSIDE the workspace is unreadable, which only a scoped
file-read allowlist can satisfy — a `(deny file-write*)`-only profile would
pass every OTHER assertion and fail exactly that one.

The risk `(deny default)` carries — forgetting a mach-lookup/sysctl-read a
process needs just to START, which breaks the child before it prints
anything, in a way this repo cannot reproduce (development happens on Linux;
this module's confinement is only PROVEN by macOS CI, per the module's own
test) — is managed by leaving every non-filesystem operation class
(process-fork, process-exec, signal, sysctl-read, mach-lookup, iokit-open,
POSIX IPC) UNQUALIFIED rather than narrowed to specific service names this
repo cannot verify from a Linux box. Filesystem is the one class actually
scoped, because it is the one class the test can and does measure without
macOS: reads via an allowlist, writes via `read_write` alone. This mirrors
OpenAI Codex CLI's Seatbelt sandbox for the identical job (confining a
subprocess that spawns further interpreters/toolchains to a workspace) —
`(deny default)`, broad non-filesystem grants, a curated `file-read*`
allowlist — rather than Bazel's `(allow default)` variant, which does not
scope reads at all.

SCOPE, matching Landlock's on Linux: this confines filesystem writes to
`read_write`, and reads additionally to `read_write | read_only`. Process
exec/fork and network are left open — an install needs both (npm execs
node, uv may exec a python probe, and every one of them needs the network to
fetch the package) — and metadata syscalls (stat, chmod, chown, utime,
xattr-read) are left open too, matching Landlock's own documented inability
to restrict them on Linux. `unenforced_reasons()` below says exactly what is
NOT covered, on every outcome, so "confined" here never means "confined to
everything a reader might assume".
"""

from __future__ import annotations

import shutil
import sys


def available() -> bool:
    """Whether `sandbox-exec` can be used to confine a child on this host."""
    return sys.platform == "darwin" and shutil.which("sandbox-exec") is not None


def unenforced_reasons(applied: bool, scope: str = "install") -> list[str]:
    """What this confinement does NOT cover, in the executor's own vocabulary.

    Returned whether or not the profile actually applied, same contract as
    `landlock.unenforced_reasons()`: "we could not confine the installer" and
    "we confined it but the network is still open" are different facts, and a
    caller acting on either needs to know which one it has.

    `applied` is False when `sandbox-exec` is missing, or when the language
    being installed has not been measured under this profile yet (see
    `packages._CONFINABLE_DARWIN`) — the same "declared but not yet proven"
    distinction `packages._CONFINABLE` makes for Landlock.
    """
    if not applied:
        return [f"{scope}_not_confined_no_sandbox_exec"] if scope != "install" else [
            "package_install_not_confined_no_sandbox_exec"]
    # The SAME three tokens `landlock.unenforced_reasons()` emits when applied
    # — reused rather than invented, so a caller checking for
    # "install_metadata_syscalls_unrestricted" gets a true answer on either
    # platform instead of needing a second vocabulary. All three are equally
    # true here: metadata syscalls are unrestricted by design (see module
    # docstring), and network — TCP and UDP both — is fully open, a strictly
    # LARGER gap than Linux's own (which restricts TCP once the kernel ABI
    # allows it).
    return [
        f"{scope}_metadata_syscalls_unrestricted",
        f"{scope}_tcp_egress_unrestricted",
        f"{scope}_udp_egress_unrestricted",
    ]


def _quote(path: str) -> str:
    """Escape a path for embedding in a double-quoted Seatbelt profile string."""
    return path.replace("\\", "\\\\").replace('"', '\\"')


def build_profile(read_write: list[str], read_only: list[str]) -> str:
    """A `(deny default)` Seatbelt profile: writes confined to `read_write`,
    reads confined to `read_write | read_only`, everything else on the
    filesystem untouched by either allowance. See the module docstring for
    why process/mach/sysctl stay unqualified while filesystem is the one
    class actually scoped.

    Nonexistent paths are included anyway — `subpath` matching a path that is
    not there yet is inert, not an error, so a cache directory created lazily
    by the installer is still covered.
    """
    rw = " ".join(f'(subpath "{_quote(p)}")' for p in read_write)
    ro = " ".join(f'(subpath "{_quote(p)}")' for p in read_only)
    lines = [
        "(version 1)",
        "(deny default)",
        "",
        "; Non-filesystem operation classes: left open. Layer 1 (packages.py)",
        "; stops hostile install-time code from running at all; this profile",
        "; bounds WHERE the installer — and anything it spawns — may write, not",
        "; what it may run, look up or introspect. See the module docstring for",
        "; why these stay unqualified rather than a service-name allowlist this",
        "; repo cannot verify without a macOS host.",
        "(allow process-fork)",
        "(allow process-exec)",
        "(allow signal (target self))",
        "(allow sysctl-read)",
        "(allow mach-lookup)",
        "(allow mach-priv-host-port)",
        "(allow iokit-open)",
        "(allow ipc-posix-shm)",
        "",
        "; Metadata (stat/access/xattr-read) unrestricted everywhere — matching",
        "; landlock.py's identical, documented gap on Linux, not a wider hole:",
        "; this covers existence/permission checks, never file CONTENT.",
        '(allow file-read-metadata (subpath "/"))',
        "",
        "; Read-only: the installer's own runtime (interpreter/toolchain prefix,",
        "; the dynamic linker's shared libraries, DNS/TLS trust config).",
    ]
    if ro:
        lines.append(f"(allow file-read* {ro})")
    lines += [
        "",
        "; Read-write: ONLY the workspace, its redirected caches and its",
        "; private tmp dir — see packages._macos_confinement's comment for why",
        "; nothing under $HOME is on this list (mirrors _landlock_confinement's",
        "; identical rule on Linux).",
    ]
    if rw:
        lines.append(f"(allow file-read* file-write* {rw})")
    lines += [
        "",
        "; Device nodes a redirect target (`> /dev/null`) legitimately needs.",
        "(allow file-read* file-write-data",
        '    (literal "/dev/null") (literal "/dev/zero")',
        '    (literal "/dev/urandom") (literal "/dev/random"))',
        "",
        "; An install needs the network; see this module's docstring for why",
        "; that is disclosed via unenforced_reasons() rather than restricted.",
        "(allow network*)",
    ]
    return "\n".join(lines) + "\n"


def command_prefix(profile_path: str) -> list[str]:
    """The argv to prepend so the child runs under `profile_path`.

    Unlike Landlock's `preexec_fn`, Seatbelt confinement is applied by a
    wrapper PROCESS (`sandbox-exec` itself), not something run after fork —
    so it has to become part of argv rather than a `preexec_fn` callback.
    """
    return ["sandbox-exec", "-f", profile_path]
