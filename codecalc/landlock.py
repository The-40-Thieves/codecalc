"""Confine a process to a set of paths, using the Landlock LSM.

LAYER 2 of the #23 mitigation. Layer 1 stops the package managers from running
third-party code at all (`--ignore-scripts` and friends in packages.py). This
is for what still runs: the manager itself, and anything a future entry cannot
disable.

Landlock is an unprivileged, stackable Linux LSM. A process declares which
paths it may touch, calls `landlock_restrict_self`, and the kernel enforces
that on it and every process it goes on to create. No root, no daemon, no
container runtime, and — the part that matters here — no delegated cgroup
write access, which is what ruled out `cgroup v2 pids.max` for a stdio MCP
server launched by an arbitrary client.

WHY ctypes AND NOT A LIBRARY:
`py-landlock` is at 0.1.1 with two releases, and `landlock` is at 1.0.0.dev5.
Both may well be fine; neither is something to put a security boundary on in a
project that gates this carefully, when the interface being wrapped is three
syscalls. Nothing here is imported at module scope that a fork would inherit
badly, and there is no new dependency to pin, audit or keep current.

WHAT LANDLOCK DOES NOT DO, from the kernel documentation, because a boundary
whose gaps are not written down gets mistaken for a complete one:

  - It cannot restrict `chdir`, `stat`, `flock`, `chmod`, `chown`, `setxattr`,
    `utime`, `fcntl` or `access`. Metadata is readable and changeable on paths
    the process can reach.
  - `chroot(2)` remains allowed.
  - A file descriptor opened BEFORE the ruleset is applied stays usable. This
    is why the ruleset is applied in `preexec_fn`, after fork and before exec,
    with `close_fds=True` doing the rest.
  - UDP needs ABI 10. On anything lower — including the ABI 7 this was
    developed against — a confined process can still reach the network over
    UDP, DNS included. TCP needs ABI 4.
  - Multithreaded `restrict_self` needs ABI 8 (TSYNC). Irrelevant here and
    deliberately so: this runs in a freshly forked child, which has exactly one
    thread.

Everything in that list is reported by `unenforced_reasons()` rather than
being left for a reader to discover in a kernel manual.
"""

from __future__ import annotations

import ctypes
import os
import pathlib

#: Syscall numbers. Identical on x86_64 and aarch64 — Landlock arrived after
#: both moved to the generic table — and verified on aarch64 before use.
_NR_CREATE_RULESET = 444
_NR_ADD_RULE = 445
_NR_RESTRICT_SELF = 446

_LANDLOCK_CREATE_RULESET_VERSION = 1 << 0
_LANDLOCK_RULE_PATH_BENEATH = 1

_PR_SET_NO_NEW_PRIVS = 38

# Filesystem access rights, with the ABI that introduced each. The set is
# masked against the running kernel's ABI: asking for a right it does not know
# makes the whole ruleset fail, which would turn a partial boundary into none.
_FS = {
    "execute": (1 << 0, 1), "write_file": (1 << 1, 1), "read_file": (1 << 2, 1),
    "read_dir": (1 << 3, 1), "remove_dir": (1 << 4, 1), "remove_file": (1 << 5, 1),
    "make_char": (1 << 6, 1), "make_dir": (1 << 7, 1), "make_reg": (1 << 8, 1),
    "make_sock": (1 << 9, 1), "make_fifo": (1 << 10, 1), "make_block": (1 << 11, 1),
    "make_sym": (1 << 12, 1),
    "refer": (1 << 13, 2), "truncate": (1 << 14, 3), "ioctl_dev": (1 << 15, 5),
}

_READ_RIGHTS = ("execute", "read_file", "read_dir")
_WRITE_RIGHTS = ("write_file", "remove_dir", "remove_file", "make_char",
                 "make_dir", "make_reg", "make_sock", "make_fifo",
                 "make_block", "make_sym", "refer", "truncate", "ioctl_dev")

#: Rights that mean anything for a NON-directory. The kernel rejects a rule
#: carrying directory-only rights on a file with EINVAL, so a mask has to be
#: narrowed by what the path actually is. Found by adding /dev/null to a
#: ruleset and watching the whole confinement fail to apply — which is the
#: right direction to fail, but only if the caller notices.
_FILE_RIGHTS = ("execute", "read_file", "write_file", "truncate", "ioctl_dev")


class _RulesetAttr(ctypes.Structure):
    """struct landlock_ruleset_attr.

    Only `handled_access_fs` is declared. The syscall takes an explicit size,
    so passing the ABI-1 size asks the kernel for exactly the ABI-1 semantics —
    which is what this module uses. Network handling would add
    `handled_access_net` (ABI 4) and a larger size; it is deliberately absent,
    because a TCP-only network boundary on a kernel where UDP cannot be
    restricted is worth less than saying so plainly.
    """

    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    """struct landlock_path_beneath_attr — PACKED.

    12 bytes, not 16. The kernel struct carries __attribute__((packed)), and a
    default-aligned ctypes Structure would pad to 16 and be rejected with
    EINVAL. This is the single easiest thing to get wrong here.
    """

    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64),
                ("parent_fd", ctypes.c_int32)]


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL("libc.so.6", use_errno=True)


def abi_version() -> int:
    """The kernel's Landlock ABI, or 0 if Landlock is unavailable/disabled."""
    if not hasattr(os, "uname") or os.uname().sysname != "Linux":
        return 0
    try:
        version = _libc().syscall(_NR_CREATE_RULESET, None, 0,
                                  _LANDLOCK_CREATE_RULESET_VERSION)
    except OSError:
        return 0
    return version if version > 0 else 0


def available() -> bool:
    return abi_version() > 0


def unenforced_reasons(abi: int | None = None) -> list[str]:
    """What this confinement does NOT cover, in the executor's own vocabulary.

    Returned whether or not Landlock applied, because "we could not confine
    the installer" and "we confined it but UDP is still open" are different
    facts and a caller acting on either needs to know which it has.
    """
    abi = abi_version() if abi is None else abi
    if abi == 0:
        return ["package_install_not_confined_no_landlock"]
    out = [
        # True at every ABI: these are documented gaps, not version gaps.
        "install_metadata_syscalls_unrestricted",  # chmod/chown/stat/utime/...
    ]
    if abi < 10:
        out.append("install_udp_egress_unrestricted")
    if abi < 4:
        out.append("install_tcp_egress_unrestricted")
    return out


def _mask_for(abi: int, names: tuple[str, ...]) -> int:
    mask = 0
    for name in names:
        bit, since = _FS[name]
        if abi >= since:
            mask |= bit
    return mask


def restrict_self(read_write: list[str], read_only: list[str]) -> None:
    """Confine THIS process (and its children) to the given paths.

    Irreversible, by design. Raises OSError if the kernel refuses, and the
    caller must treat that as a failure to confine rather than proceeding —
    silently continuing unconfined is the failure mode this whole module
    exists to remove.

    Intended for use in `preexec_fn`: after fork, before exec, in a child with
    one thread and no inherited descriptors beyond the ones subprocess keeps.
    """
    abi = abi_version()
    if abi == 0:
        raise OSError("Landlock unavailable on this kernel")

    libc = _libc()
    handled = _mask_for(abi, tuple(_FS))
    attr = _RulesetAttr(handled_access_fs=handled)
    ruleset_fd = libc.syscall(_NR_CREATE_RULESET, ctypes.byref(attr),
                              ctypes.sizeof(attr), 0)
    if ruleset_fd < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset failed")

    try:
        rw_names = _READ_RIGHTS + _WRITE_RIGHTS
        ro_names = _READ_RIGHTS
        for paths, names in ((read_write, rw_names), (read_only, ro_names)):
            for path in paths:
                # A file gets the file-applicable subset; a directory gets the
                # lot. Passing the lot for a file makes landlock_add_rule
                # return EINVAL and takes the ENTIRE ruleset down with it.
                is_dir = pathlib.Path(path).is_dir()
                mask = _mask_for(abi, tuple(n for n in names
                                            if is_dir or n in _FILE_RIGHTS))
                if not mask:
                    continue
                try:
                    path_fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
                except OSError:
                    # A path that does not exist is skipped rather than fatal:
                    # the allow-list is a superset covering several platforms
                    # and toolchain layouts, and refusing to confine at all
                    # because /opt is absent would be the worst outcome.
                    continue
                try:
                    rule = _PathBeneathAttr(allowed_access=mask, parent_fd=path_fd)
                    if libc.syscall(_NR_ADD_RULE, ruleset_fd,
                                    _LANDLOCK_RULE_PATH_BENEATH,
                                    ctypes.byref(rule), 0) < 0:
                        raise OSError(ctypes.get_errno(),
                                      f"landlock_add_rule failed for {path}")
                finally:
                    os.close(path_fd)

        # REQUIRED before restrict_self, and it is its own hardening: without
        # it a confined process could still gain privileges through a setuid
        # binary it is allowed to execute.
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "prctl(PR_SET_NO_NEW_PRIVS) failed")
        if libc.syscall(_NR_RESTRICT_SELF, ruleset_fd, 0) < 0:
            raise OSError(ctypes.get_errno(), "landlock_restrict_self failed")
    finally:
        os.close(ruleset_fd)
