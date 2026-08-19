"""THE-829 acceptance probe — the payload codecalc runs INSIDE the sandbox.

Emits one JSON line describing what this process's token is and what it can
actually reach, so a harness can compare a normal run against a
`CODECALC_WIN_APPCONTAINER=1` run and confirm the isolation empirically rather
than from the source contract. Everything here is measured from inside the
payload; nothing is asserted. Portable-Python only (ctypes on Windows), so the
same file is the payload in both arms.

Measured:
  * is_appcontainer / appcontainer_sid / privilege_count — the token itself
    (acceptance: "expected AppContainer SID and no ambient user token privileges")
  * can_read_secret  — open %USERPROFILE%\\codecalc-ac-secret.txt planted by the
    harness (acceptance: "payload cannot read user-profile secrets")
  * can_write_outside — write %USERPROFILE%\\codecalc-ac-escape.txt
    (acceptance: "cannot write outside the work directory")
  * can_write_workdir — write in cwd (must stay TRUE — the sandbox is usable)
  * can_network — connect out (acceptance: "--no-net egress ... blocked")
"""

from __future__ import annotations

import json
import os
import socket

result: dict[str, object] = {"platform": os.name}


# ── token introspection (Windows only) ───────────────────────────────────────
def _token_facts() -> dict[str, object]:
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)

    # Pin prototypes. Without restype=HANDLE, GetCurrentProcess()'s (HANDLE)-1
    # pseudo-handle is truncated to 32 bits and OpenProcessToken then fails with
    # ERROR_INVALID_HANDLE (6) on 64-bit Python. This is the whole reason the
    # first box run reported "token_error": "OpenProcessToken 6".
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetCurrentProcess.argtypes = []
    kernel.LocalFree.restype = ctypes.c_void_p
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_wchar_p),
    ]

    TOKEN_QUERY = 0x0008
    TokenPrivileges = 3
    TokenIsAppContainer = 29
    TokenAppContainerSid = 31

    htok = wintypes.HANDLE()
    if not advapi.OpenProcessToken(
        kernel.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(htok)
    ):
        return {"token_error": f"OpenProcessToken {ctypes.get_last_error()}"}

    def query(info_class: int, buf_type):
        size = wintypes.DWORD(0)
        advapi.GetTokenInformation(htok, info_class, None, 0, ctypes.byref(size))
        if size.value == 0:
            return None
        buf = (ctypes.c_byte * size.value)()
        if not advapi.GetTokenInformation(
            htok, info_class, buf, size.value, ctypes.byref(size)
        ):
            return None
        return ctypes.cast(buf, ctypes.POINTER(buf_type)).contents

    facts: dict[str, object] = {}

    is_ac = query(TokenIsAppContainer, wintypes.DWORD)
    facts["is_appcontainer"] = bool(is_ac.value) if is_ac is not None else None

    # TOKEN_APPCONTAINER_INFORMATION { PSID TokenAppContainer } — a single SID ptr.
    class TOKEN_APPCONTAINER_INFORMATION(ctypes.Structure):
        _fields_ = [("TokenAppContainer", ctypes.c_void_p)]

    ac_info = query(TokenAppContainerSid, TOKEN_APPCONTAINER_INFORMATION)
    sid_str = None
    if ac_info is not None and ac_info.TokenAppContainer:
        p = ctypes.c_wchar_p()
        if advapi.ConvertSidToStringSidW(
            ctypes.c_void_p(ac_info.TokenAppContainer), ctypes.byref(p)
        ):
            sid_str = p.value
            kernel.LocalFree(ctypes.cast(p, ctypes.c_void_p))
    facts["appcontainer_sid"] = sid_str

    # TOKEN_PRIVILEGES { DWORD PrivilegeCount; ... } — count is the first DWORD.
    priv = query(TokenPrivileges, wintypes.DWORD)
    facts["privilege_count"] = int(priv.value) if priv is not None else None

    return facts


if os.name == "nt":
    try:
        result.update(_token_facts())
    except Exception as exc:  # never let introspection abort the reach probes
        result["token_error"] = f"{type(exc).__name__}: {exc}"

# ── reach probes: read a planted secret, write outside, write inside, network ──
home = os.environ.get("USERPROFILE") or os.environ.get("HOME") or ""
secret_path = os.path.join(home, "codecalc-ac-secret.txt")
outside_path = os.path.join(home, "codecalc-ac-escape.txt")

try:
    with open(secret_path, encoding="utf-8") as fh:
        fh.read()
    result["can_read_secret"] = True
except Exception as exc:
    result["can_read_secret"] = False
    result["read_err"] = type(exc).__name__

try:
    with open(outside_path, "w", encoding="utf-8") as fh:
        fh.write("escaped")
    result["can_write_outside"] = True
    os.remove(outside_path)
except Exception as exc:
    result["can_write_outside"] = False
    result["write_outside_err"] = type(exc).__name__

try:
    with open("ac-probe-inside.txt", "w", encoding="utf-8") as fh:
        fh.write("ok")
    result["can_write_workdir"] = True
except Exception as exc:
    result["can_write_workdir"] = False
    result["write_workdir_err"] = type(exc).__name__

try:
    conn = socket.create_connection(("1.1.1.1", 80), timeout=5)
    conn.close()
    result["can_network"] = True
except Exception as exc:
    result["can_network"] = False
    result["net_err"] = type(exc).__name__

print(json.dumps(result))
