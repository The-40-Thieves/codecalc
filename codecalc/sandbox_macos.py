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

fix-round-2 history, because the first two rounds were both wrong in
instructive ways and the next reader should not repeat either mistake:

  round 1 fixed a real bug (unresolved `/var/folders/...` paths never
  matching Seatbelt's kernel-RESOLVED `/private/var/folders/...` form — see
  `_canon()`) but ALSO replaced round-0's unverified bare `(allow
  mach-lookup)` / `(allow sysctl-read)` / `(allow iokit-open)` with... still
  unverified guesses, just fewer of them. Round 1 shipped anyway because an
  invalid keyword and a too-narrow grant look IDENTICAL from a Linux dev box
  (both abort the child silently) and there was no source to check against
  yet.

  round 2 (this one) replaces every non-filesystem grant AND the missing
  file-map-executable rights below with lines copied or directly adapted
  from OpenAI Codex CLI's ACTUAL shipped Seatbelt profile — a program that
  runs the identical kind of job (a forked/exec'd interpreter under `(deny
  default)`) in production, right down to a comment explicitly calling out
  Python multiprocessing. Pinned source, commit
  e13c1d569d953ecac06a09cf5663fb3cd405636d:
    https://github.com/openai/codex/blob/e13c1d569d953ecac06a09cf5663fb3cd405636d/codex-rs/sandboxing/src/seatbelt_base_policy.sbpl
    https://github.com/openai/codex/blob/e13c1d569d953ecac06a09cf5663fb3cd405636d/codex-rs/sandboxing/src/restricted_read_only_platform_defaults.sbpl
    https://github.com/openai/codex/blob/e13c1d569d953ecac06a09cf5663fb3cd405636d/codex-rs/sandboxing/src/seatbelt_network_policy.sbpl
  (Codex's own base policy says it is in turn "inspired by Chrome's sandbox
  policy".) Every grant below traces to one of those three files or is
  marked as this module's own addition.

  The one likely ROOT CAUSE round 2 found that round 1 could not: dyld needs
  `file-map-executable` to mmap a shared library as executable pages, which
  is a DIFFERENT right from `file-read*` — round 1's profile granted broad
  `file-read*` over every system library path but never granted
  `file-map-executable` anywhere, so dyld could see its own dependencies but
  not load them. That fails during interpreter startup, before any of this
  module's own script code runs — a silent SIGABRT with empty stderr
  (Seatbelt denials go to the unified log, not the child's stderr) is
  exactly what CI measured.

  round 2 also fixes a SECOND, independent bug round 1's diagnostics
  surfaced: `tests/test_package_isolation.py`'s canary lived under
  `tempfile.mkdtemp()`, i.e. under `/private/var/folders/...` once
  resolved — the SAME tree round 1's `read_only` list granted blanket
  `file-read*` over (`/private/var`) so dyld/Python startup could read
  `/private/var/db`. Once the interpreter can actually start, that blanket
  grant would make the canary READABLE, failing the "canary outside is
  unreadable" assertion as a false negative on the real security property.
  Fixed two ways: `read_only` no longer includes blanket `/private/var`
  (only the specific `/private/var/db` subpath startup needs — baked into
  this module rather than caller-supplied, see `build_profile()`), and the
  test moves its canary under `$HOME`, nowhere near any grant.

SCOPE, matching Landlock's on Linux: this confines filesystem writes to
`read_write`, and reads (plus the mapping needed to load and execute code
from them) additionally to `read_write | read_only`. Metadata syscalls
(stat, chmod, chown, utime, xattr-read) are left open everywhere, matching
Landlock's own documented inability to restrict them on Linux, and network
is left fully open — an install needs it. `unenforced_reasons()` below says
exactly what is NOT covered, on every outcome, so "confined" here never
means "confined to everything a reader might assume".
"""

from __future__ import annotations

import os
import shutil
import sys

#: Process/signal/IPC/IOKit/mach-lookup/sysctl baseline. Every line here is
#: copied or directly adapted from OpenAI Codex CLI's shipped Seatbelt
#: profile (see the module docstring for the pinned source) — NOT a guess.
#: This is what lets a forked/exec'd interpreter (Codex's own comments call
#: out Python multiprocessing and PyTorch/libomp by name) actually start
#: under `(deny default)`, independent of anything workspace-specific.
_STARTUP_BASELINE = """\
; ---- process / signal / IPC baseline ----
; seatbelt_base_policy.sbpl: "child processes inherit the policy of their
; parent" — needed so the installer can exec its own subprocesses (npm execs
; node; uv may exec a probe) under the SAME confinement, not a wider one.
(allow process-exec)
(allow process-fork)
(allow signal (target same-sandbox))
(allow process-info* (target same-sandbox))

; seatbelt_base_policy.sbpl: "Needed for python multiprocessing on MacOS for
; the SemLock" — python3 is one of the interpreters this module confines.
(allow ipc-posix-sem)

; seatbelt_base_policy.sbpl + restricted_read_only_platform_defaults.sbpl
(allow iokit-open (iokit-registry-entry-class "RootDomainUserClient"))

; mach-lookup: union of seatbelt_base_policy.sbpl,
; restricted_read_only_platform_defaults.sbpl and seatbelt_network_policy.sbpl
; (the last group only matters once a process actually dials out, which an
; install always does). Directory/preference/logging/trust services an
; interpreter's own startup and TLS stack reach for.
(allow mach-lookup
    (global-name "com.apple.system.opendirectoryd.libinfo")
    (global-name "com.apple.system.opendirectoryd.membership")
    (global-name "com.apple.system.DirectoryService.libinfo_v1")
    (global-name "com.apple.cfprefsd.agent")
    (global-name "com.apple.cfprefsd.daemon")
    (global-name "com.apple.logd")
    (global-name "com.apple.logd.events")
    (global-name "com.apple.system.notification_center")
    (global-name "com.apple.trustd")
    (global-name "com.apple.trustd.agent")
    (global-name "com.apple.PowerManagement.control")
    (global-name "com.apple.bsd.dirhelper")
    (global-name "com.apple.SecurityServer")
    (global-name "com.apple.networkd")
    (global-name "com.apple.ocspd")
    (global-name "com.apple.SystemConfiguration.DNSConfiguration")
    (global-name "com.apple.SystemConfiguration.configd")
    (local-name "com.apple.cfprefsd.agent"))

; seatbelt_base_policy.sbpl's full sysctl-read allowlist, verbatim — CPU
; topology/feature detection every interpreter's runtime probes at startup.
(allow sysctl-read
    (sysctl-name "hw.activecpu")
    (sysctl-name "hw.busfrequency_compat")
    (sysctl-name "hw.byteorder")
    (sysctl-name "hw.cacheconfig")
    (sysctl-name "hw.cachelinesize_compat")
    (sysctl-name "hw.cpufamily")
    (sysctl-name "hw.cpufrequency_compat")
    (sysctl-name "hw.cputype")
    (sysctl-name "hw.l1dcachesize_compat")
    (sysctl-name "hw.l1icachesize_compat")
    (sysctl-name "hw.l2cachesize_compat")
    (sysctl-name "hw.l3cachesize_compat")
    (sysctl-name "hw.logicalcpu_max")
    (sysctl-name "hw.machine")
    (sysctl-name "hw.model")
    (sysctl-name "hw.memsize")
    (sysctl-name "hw.ncpu")
    (sysctl-name "hw.nperflevels")
    (sysctl-name-prefix "hw.optional.arm.")
    (sysctl-name-prefix "hw.optional.armv8_")
    (sysctl-name "hw.packages")
    (sysctl-name "hw.pagesize_compat")
    (sysctl-name "hw.pagesize")
    (sysctl-name "hw.physicalcpu")
    (sysctl-name "hw.physicalcpu_max")
    (sysctl-name "hw.logicalcpu")
    (sysctl-name "hw.cpufrequency")
    (sysctl-name "hw.tbfrequency_compat")
    (sysctl-name "hw.vectorunit")
    (sysctl-name "machdep.cpu.brand_string")
    (sysctl-name "kern.argmax")
    (sysctl-name "kern.hostname")
    (sysctl-name "kern.maxfilesperproc")
    (sysctl-name "kern.maxproc")
    (sysctl-name "kern.osproductversion")
    (sysctl-name "kern.osrelease")
    (sysctl-name "kern.ostype")
    (sysctl-name "kern.osvariant_status")
    (sysctl-name "kern.osversion")
    (sysctl-name "kern.secure_kernel")
    (sysctl-name "kern.usrstack64")
    (sysctl-name "kern.version")
    (sysctl-name "sysctl.proc_cputype")
    (sysctl-name "vm.loadavg")
    (sysctl-name-prefix "hw.perflevel")
    (sysctl-name-prefix "kern.proc.pgrp.")
    (sysctl-name-prefix "kern.proc.pid.")
    (sysctl-name-prefix "net.routetable."))
(allow sysctl-read (sysctl-name-regex #"^net.routetable"))
; "misclassified as a write because userspace passes a memory buffer to the
; sysctl, but conceptually it is a read" — seatbelt_base_policy.sbpl's own
; comment, verbatim rule.
(allow sysctl-write (sysctl-name "kern.grade_cputype"))

; ---- read-only platform defaults (restricted_read_only_platform_defaults.sbpl) ----
; file-map-executable is a SEPARATE right from file-read* — dyld needs it to
; mmap a shared library as EXECUTABLE pages. Missing here in fix-round-1 is
; the most likely reason the confined interpreter SIGABRT'd during its own
; startup, before any script code ran: file-read* let dyld see its
; dependencies but not load them.
(allow file-map-executable
    (subpath "/usr/lib")
    (subpath "/System/Library/Frameworks")
    (subpath "/System/Library/PrivateFrameworks")
    (subpath "/System/Library/SubFrameworks")
    (subpath "/System/Library/Extensions"))
(allow file-read* file-test-existence
    (subpath "/usr/lib")
    (subpath "/usr/share")
    (subpath "/Library/Preferences")
    (subpath "/private/var/db")
    (subpath "/usr/local"))
(allow file-read-data (subpath "/bin"))
(allow file-read-metadata (subpath "/bin"))
(allow file-read-data (subpath "/sbin"))
(allow file-read-metadata (subpath "/sbin"))
(allow file-read-data (subpath "/usr/bin"))
(allow file-read-metadata (subpath "/usr/bin"))
(allow file-read-data (subpath "/usr/sbin"))
(allow file-read-metadata (subpath "/usr/sbin"))
(allow file-read-data (subpath "/usr/libexec"))
(allow file-read-metadata (subpath "/usr/libexec"))
(allow file-read* (subpath "/etc"))
(allow file-read* (subpath "/private/etc"))
(allow file-read* file-test-existence (literal "/"))

; Metadata (stat/access/xattr-read) unrestricted everywhere — matching
; landlock.py's identical, documented gap on Linux, not a wider hole: this
; covers existence/permission checks, never file CONTENT. Deliberately
; broader than restricted_read_only_platform_defaults.sbpl's own
; metadata-only `/var`/`/private/var` rules (this repo already made that
; call before fix-round-2 and it is unrelated to what fix-round-2 changed).
(allow file-read-metadata (subpath "/"))
"""


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


def _canon(path: str) -> str:
    """Resolve symlinks BEFORE a path reaches a `(subpath ...)` rule.

    THE-819 fix-round-1: Seatbelt matches a process's paths in their
    kernel-RESOLVED (symlink-free) form, regardless of what string the
    process itself used to open() them. macOS's own temp directory is the
    textbook case that broke this: `tempfile.mkdtemp()` returns something
    under `/var/folders/...`, but `/var` is itself a symlink to
    `/private/var` — so a rule written as `(subpath "/var/folders/...")`
    never matches the `/private/var/folders/...` form the kernel actually
    presents to Seatbelt. Measured on macOS CI: every workspace rule silently
    missed, the confined child couldn't even write its OWN workspace, and the
    probe produced no output at all — not "confinement too strong", the
    profile simply never matched anything real.

    `/tmp` -> `/private/tmp` and `/etc` -> `/private/etc` are the same trap.
    `os.path.realpath()` handles all three (and every path that is already
    canonical is returned unchanged, so this is safe to apply universally
    rather than special-casing which inputs need it). It resolves symlinks in
    whatever PREFIX of the path currently exists and leaves the rest
    literal — exactly what a not-yet-created cache subdirectory needs.
    """
    return os.path.realpath(path)


def build_profile(read_write: list[str], read_only: list[str]) -> str:
    """A `(deny default)` Seatbelt profile: writes confined to `read_write`,
    reads (and the execute-mapping needed to actually load code) confined to
    `read_write | read_only` plus the fixed system baseline in
    `_STARTUP_BASELINE`, everything else on the filesystem untouched by any
    of it.

    Nonexistent paths are included anyway — `subpath` matching a path that is
    not there yet is inert, not an error, so a cache directory created lazily
    by the installer is still covered. Every path is passed through
    `_canon()` first — see its docstring for why an unresolved path silently
    matches nothing.

    `read_only` gets BOTH `file-read*` and `file-map-executable`: a caller's
    toolchain prefix (e.g. a venv's own `lib/` holding `libpython*.dylib`)
    needs its shared libraries mapped executable exactly the same way the
    system frameworks in `_STARTUP_BASELINE` do — see the module docstring
    for why `file-read*` alone was fix-round-1's bug.
    """
    rw = " ".join(f'(subpath "{_quote(_canon(p))}")' for p in read_write)
    ro = " ".join(f'(subpath "{_quote(_canon(p))}")' for p in read_only)
    lines = [
        "(version 1)",
        "(deny default)",
        "",
        _STARTUP_BASELINE,
        "; Read-only: the installer's own runtime (interpreter/toolchain prefix,",
        "; the dynamic linker's shared libraries it needs beyond the fixed",
        "; system baseline above, DNS/TLS trust config).",
    ]
    if ro:
        lines.append(f"(allow file-read* file-map-executable {ro})")
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
