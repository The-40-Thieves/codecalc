"""Package installation inside sandbox workspaces.

Installs packages with each language's native package manager into the
session workspace (or a shared per-language cache for ad-hoc runs), so
executed code can import them.

Security: package names/versions are passed as argv (never shell), and only
the language's own package manager is invoked — no arbitrary command
execution from package strings.

THE INSTALL MUST NOT ESCAPE THE WORKSPACE, and this is the part that was
wrong. python3 used `uv pip install --system`, which ignores cwd entirely and
targets the interpreter — on a mise-managed host, the toolchain-managed
interpreter under the mise installs root, which is the very interpreter the
sandbox runs untrusted code on. One tool call therefore installed third-party
code into every future sandboxed run, for every session, permanently, and
survived session_stop. The docstring claimed workspace scoping the whole time.

`--target <dir>` fixes it and needs no environment plumbing: CPython puts the
script's own directory on sys.path[0], and executed code runs from the session
workdir, so a package installed there is importable. Verified both halves — a
package installed with --target is importable from that directory and is NOT
visible to the host interpreter.

`ruby` and `r` had the same shape (`gem install` -> the interpreter's global
gem directory, `install.packages` -> the global R site-library). Neither can be
made
importable from a workspace without adding GEM_HOME/R_LIBS to the executor's
environment allowlist, and that allowlist is the fix for AUDIT.md CRITICAL-02 —
not something to widen for a convenience feature. They are declined instead,
with the reason, rather than silently mutating the host.

Network: an install needs it. There is no --no-net interaction to enforce
because installs do not run inside the sandbox at all; they are a direct
subprocess from the server. The previous docstring claimed --no-net sessions
could not install, which was never checked anywhere.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import errors, executor, registry

#: language -> (manager binary, argv template with {pkg} placeholder, env hint)
#: {target} is substituted with the workspace directory. Every entry must be
#: workspace-scoped: an installer that writes outside it does not belong here.
#: LAYER 1 of the #23 mitigation: do not run the hostile code at all.
#:
#: Installing a package normally EXECUTES third-party code before anything is
#: imported — npm lifecycle scripts, Python build backends, Composer plugins,
#: Cargo build scripts. Confining that code is layer 2 below; not running it is
#: cheaper, works on every platform, and needs no kernel feature.
#:
#: npm shipped exactly this as its own default in v12 (July 2026), after a year
#: of supply-chain attacks that "shared the same core mechanism: a postinstall
#: hook that fired the moment a developer ran npm install". codecalc cannot
#: assume v12 — the npm on this host is 11.17.0 — so the flag is passed
#: explicitly rather than relied upon.
#:
#: THE COST IS REAL AND IS NOT HIDDEN. `--ignore-scripts` breaks packages that
#: genuinely need a build step (native addons), and `--only-binary=:all:` fails
#: for a package with no wheel for this platform. Both failures are reported to
#: the caller with the flag named, so "it did not install" is never mistaken
#: for "the package does not exist".
#: Characters a package specifier may contain. Deliberately broad — it exists
#: to keep argv from being reinterpreted, not to validate any one registry's
#: naming rules. `;`, `&`, `|`, backticks and quotes are absent, but note that
#: nothing here is passed through a shell: the argv array is exec'd directly,
#: so those were never the risk. The leading-hyphen check above is.
_PACKAGE_NAME_RE = re.compile(r"[A-Za-z0-9._@/\[\]+~=<>!,\- ]+")

#: THE-791 residual: an operator opt-in, deny-by-default allowlist.
#:
#: UNSET (default) is today's behaviour — every syntactically valid name
#: passes, same as before this existed. SET makes install() deny-by-default:
#: a name not on the list is refused before any subprocess or network work,
#: which is the property the ticket asks for ("a package allowlist that
#: ignores --index-url is a fence with a gate" — so the gate has to be the
#: FIRST thing standing between a request and a spawn, not a filter on the
#: result).
#:
#: Comma-separated entries. Each is either `<language>:<name>` (scoped to one
#: ecosystem — the "per ecosystem where distinguishable" the ticket asks for,
#: `<language>` is whatever `registry.canonical()` returns, e.g. `python3`,
#: `node`) or a bare `<name>` (allowed in every ecosystem this module
#: installs for). Matching is case-insensitive and against the BARE name —
#: `requests[security]==2.31.0` matches an entry of `requests`; see
#: `_bare_name`.
#:
#: There is no separate "registries" knob. install_package accepts no
#: index-url/registry-override parameter today — the ticket's own charset
#: already refuses a colon, so a registry URL cannot reach argv at all — so
#: there is nothing to allowlist there yet. What this module DOES bind is
#: the other user-controlled field that lands in the same argv slot: see the
#: embedded-flag-token check in `install()`, which refuses a package/version
#: string shaped like a flag regardless of whether the allowlist is even
#: configured.
ALLOWLIST_ENV = "CODECALC_PACKAGE_ALLOWLIST"

#: Characters that start a qualifier a package manager parses separately from
#: the bare installable name: `[` (PyPI extras), version-specifier operators,
#: `,` (specifier lists) and space. `@` and `/` are NOT stop characters —
#: npm scopes (`@scope/name`) start with `@`, and stripping at the first `/`
#: would break every scoped package's second half.
_QUALIFIER_RE = re.compile(r"[\[=<>!~, ]")


def _bare_name(package: str) -> str:
    """The installable name, stripped of extras/version-pin syntax, lowered.

    Used only for allowlist comparison — installers still receive the full
    `package` string unmodified.
    """
    stop = _QUALIFIER_RE.search(package)
    return (package[:stop.start()] if stop else package).strip().lower()


def _allowlist() -> set[str] | None:
    """Configured allowlist entries, lowercased, or None when unset.

    Read per call rather than cached at import — an operator's decision
    should take effect without a server restart, same reasoning as
    runtimes.elevated_apply_allowed().
    """
    raw = os.environ.get(ALLOWLIST_ENV, "")
    if not raw.strip():
        return None
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def _allowlist_denial(language: str, package: str) -> str | None:
    """None if `package` is allowed under `language`; else the denial reason.

    Checked BEFORE the installer table lookup that follows it, so an unset
    allowlist costs one dict-less function call and a configured one refuses
    before `shutil.which` or `subprocess.run` ever run.
    """
    entries = _allowlist()
    if entries is None:
        return None
    bare = _bare_name(package)
    if bare in entries or f"{language}:{bare}" in entries:
        return None
    return (f"package {bare!r} is not on the {ALLOWLIST_ENV} allowlist for "
            f"ecosystem '{language}'; deny-by-default is active because "
            f"{ALLOWLIST_ENV} is set")


#: A package/version string containing a SPACE followed by a token starting
#: with `-` is refused outright, allowlist or not. subprocess's argv-list
#: semantics already make this inert — `{pkg}` lands as ONE argv element, so
#: no shell ever re-splits "pkg --index-url=..." into two flags — but the
#: ticket calls out this exact shape ("a package allowlist that ignores
#: --index-url is a fence with a gate"), and refusing the shape does not rely
#: on that mechanics detail continuing to hold if this code is ever moved
#: behind something that DOES reinterpret argv.
_EMBEDDED_FLAG_RE = re.compile(r"\s-")


def _has_embedded_flag(spec: str) -> bool:
    return bool(_EMBEDDED_FLAG_RE.search(spec))


_INSTALLERS: dict[str, tuple[str, list[str], dict[str, str]]] = {
    # --only-binary=:all: — wheels only. A source distribution runs its build
    # backend, which is arbitrary code execution at install time.
    "python3": ("uv", ["uv", "pip", "install", "--only-binary=:all:", "--target", "{target}", "{pkg}"], {"UV_CACHE_DIR": "{target}/.cache/uv"}),
    # --prefix is a CONTAINMENT fix, not a convenience. Without it npm walks UP
    # from cwd looking for a package root, finds none in the workspace, and
    # settles on the first ancestor it likes — observed asking to
    # `mkdir $HOME/node_modules/<pkg>` while cwd was the workspace. The workspace scoping this table's own comment
    # promises was not happening for node, and the Landlock ruleset below is
    # what surfaced it: the install failed with EACCES on a path outside the
    # workspace rather than quietly succeeding somewhere else.
    "node": ("npm", ["npm", "install", "{pkg}", "--prefix", "{target}", "--ignore-scripts", "--no-save", "--no-audit", "--no-fund"], {"npm_config_cache": "{target}/.cache/npm"}),
    "bun": ("bun", ["bun", "add", "--ignore-scripts", "{pkg}"], {"BUN_INSTALL_CACHE_DIR": "{target}/.cache/bun"}),
    "deno": ("deno", ["deno", "install", "{pkg}"], {}),
    # Composer plugins are code; --no-scripts also declines the package's own.
    "php": ("composer", ["composer", "require", "--no-scripts", "{pkg}"], {"COMPOSER_HOME": "{target}/.cache/composer", "COMPOSER_CACHE_DIR": "{target}/.cache/composer"}),
    # `go get` does not execute package code; the build does, later, under the
    # executor's own sandbox rather than here.
    "go": ("go", ["go", "get", "{pkg}"], {"GOMODCACHE": "{target}/.cache/go/mod", "GOPATH": "{target}/.cache/go"}),
    # `cargo add` only edits Cargo.toml — build.rs runs at BUILD time, which is
    # the executor's sandbox, not this path.
    "rust": ("cargo", ["cargo", "add", "{pkg}"], {"CARGO_HOME": "{target}/.cache/cargo"}),
}

#: The flag each installer relies on to keep third-party code from running, so
#: a failure can name it. Absent means the manager does not execute package
#: code at install time (see the per-entry comments above).
_NO_EXEC_FLAG = {
    "python3": "--only-binary=:all:",
    "node": "--ignore-scripts",
    "bun": "--ignore-scripts",
    "php": "--no-scripts",
}

#: languages whose installer is not yet supported (declared so we can tell the
#: caller why, instead of failing cryptically)
_UNSUPPORTED = {"java", "csharp", "kotlin", "swift", "zig", "elixir", "erlang",
                "gleam", "haskell", "fortran", "c", "cpp", "c++", "perl", "lua",
                "tcl", "bash", "zsh", "awk", "jq", "sqlite", "mojo", "typescript",
                # Declined rather than host-mutating — see the module docstring.
                "ruby", "r"}

#: Why a language is declined, when the reason is not simply "no installer".
_DECLINED_REASON = {
    "ruby": "gem installs into the interpreter's global gem directory; scoping it "
            "to a workspace needs GEM_HOME in the executor env allowlist, which is "
            "the CRITICAL-02 fix and is not widened for this",
    "r": "install.packages writes to the global R site-library; scoping it needs "
         "R_LIBS in the executor env allowlist, same reason",
}

#: shared cache dir for ad-hoc (non-session) installs; sessions get their own
CACHE_ROOT = Path("~/.codecalc/pkgs").expanduser()


#: Managers verified to work under the Landlock ruleset below. python3 was
#: absent for a while: `uv` failed with "Could not detect either glibc version
#: nor musl libc version", which read like a libc problem and was not. Two of
#: MY bugs, found by tracing to the end of the run instead of the first
#: denials (#90):
#:
#:   1. /dev/null was not allowed. uv's libc detection spawns a probe with its
#:      output redirected there; the open failed and detection reported the
#:      only thing it could — that it had not detected a libc.
#:   2. landlock.restrict_self applied directory-only rights to every path, so
#:      adding a device node made landlock_add_rule return EINVAL and took the
#:      WHOLE ruleset down with it.
#:
#: Both fixed, so every installer here is confined and this set is now "all of
#: them". It stays as a set rather than being deleted: a manager added later
#: has to be measured under the ruleset before it is claimed to be confined.
_CONFINABLE = {"python3", "node", "bun", "php", "go", "rust", "deno"}

#: Device nodes an installer legitimately needs. Granted individually rather
#: than by allowing /dev, which would hand over every device on the host.
#: Read-WRITE because writing to /dev/null is what a redirect does.
_DEVICE_NODES = ("/dev/null", "/dev/zero", "/dev/urandom", "/dev/random")


def _confinement(bin_: str, workspace: str, env: dict, language: str) -> tuple:
    """(preexec_fn, unenforced) confining the installer to its workspace.

    LAYER 2 of the #23 mitigation. Layer 1 stops the managers running
    third-party code; this bounds what the manager itself — and anything a
    future entry cannot disable — is able to reach.

    Applied in preexec_fn, which is the only correct place for it: after fork
    and before exec, in a child with one thread (so the ABI 8 TSYNC gap does
    not apply) and no inherited descriptors (subprocess closes them), which is
    what the kernel's "an fd opened before the ruleset stays usable" caveat
    would otherwise leave open.

    The read-only list is DERIVED, never hardcoded. An installer needs its own
    runtime — node for npm, the toolchain prefix for uv — and those live
    wherever the host put them. Resolving the binary and allowing its prefix
    keeps this working on a mise host, a system-package host and a CI runner
    alike, and keeps check_portability.py from finding a machine-specific path
    baked into the source.
    """
    from . import landlock

    if language not in _CONFINABLE:
        return None, [f"install_not_confined_{language}"] + landlock.unenforced_reasons()
    if not landlock.available():
        return None, landlock.unenforced_reasons()

    # NOTHING under $HOME. An earlier version allowed ~/.npm, ~/.cargo and
    # friends, because an installer denied its own cache fails outright rather
    # than degrading — npm dies with EACCES on ~/.npm/_logs. That made
    # "confined to its workspace" untrue in the one place a reader would care.
    #
    # Every manager's cache is redirected into the workspace by env instead
    # (see _INSTALLERS), so the allowance is unnecessary and the claim is
    # honest. The cost is real: caches are no longer shared between installs,
    # so a repeat install re-downloads and the workspace holds more.
    writable = [workspace]
    for key in ("UV_CACHE_DIR", "npm_config_cache", "BUN_INSTALL_CACHE_DIR",
                "COMPOSER_HOME", "COMPOSER_CACHE_DIR", "CARGO_HOME",
                "GOMODCACHE", "GOPATH"):
        value = env.get(key)
        if value:
            writable.append(str(pathlib.Path(value).expanduser()))
    # A private temp dir, not the shared one: managers unpack there, and
    # /tmp is where a confined process would otherwise drop things for
    # something outside the sandbox to pick up.
    private_tmp = pathlib.Path(workspace) / ".tmp"
    private_tmp.mkdir(parents=True, exist_ok=True)
    writable.append(str(private_tmp))
    env.setdefault("TMPDIR", str(private_tmp))
    writable.extend(d for d in _DEVICE_NODES if pathlib.Path(d).exists())

    # /run is not decoration: /etc/resolv.conf is a symlink to
    # ../run/systemd/resolve/stub-resolv.conf on systemd hosts, so without it
    # every install dies with EAI_AGAIN and looks like a network outage rather
    # than a policy decision. Measured the same way.
    readable = ["/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc", "/proc",
                "/opt", "/run", "/var/lib"]
    # uv's managed-interpreter store, READ-ONLY. It has to find an interpreter
    # to install FOR, and denying the directory is a hard error rather than a
    # graceful skip: "failed to read directory .../uv/python: Permission
    # denied". An earlier probe hid this by pointing HOME at a temp dir, where
    # the store did not exist and uv moved on.
    #
    # This is the one path outside the workspace the installer may read, and it
    # is worth being exact about what that costs: reading an interpreter store
    # is not a way out. Nothing outside the workspace is WRITABLE, which is the
    # property the test asserts.
    for store in (env.get("UV_PYTHON_INSTALL_DIR"),
                  env.get("XDG_DATA_HOME") and env["XDG_DATA_HOME"] + "/uv",
                  "~/.local/share/uv"):
        if store:
            readable.append(str(pathlib.Path(store).expanduser()))
    resolved = shutil.which(bin_, path=executor.registry.runtime_path()) or shutil.which(bin_)
    if resolved:
        # Two levels up from <prefix>/bin/<tool> is the toolchain root, which is
        # what a runtime actually needs — node reaches its own lib/, uv reaches
        # the interpreter it targets.
        real = pathlib.Path(resolved).resolve()
        readable.extend([str(real.parent), str(real.parent.parent)])
    readable.append(sys.prefix)
    readable.append(sys.base_prefix)

    def _apply() -> None:
        # Raises on failure, which subprocess turns into a failed spawn. That
        # is deliberate: an installer that could not be confined must not run
        # unconfined while the result claims otherwise.
        landlock.restrict_self(read_write=writable, read_only=readable)

    return _apply, landlock.unenforced_reasons()


def install(language: str, package: str, session_id: str | None = None,
            version: str | None = None) -> dict:
    """Install a package for a language. Returns where it was installed."""
    name = registry.canonical(language) or language
    if name in _UNSUPPORTED:
        reason = _DECLINED_REASON.get(name)
        msg = f"package install not supported for '{name}'"
        return {"ok": False, "error": f"{msg}: {reason}" if reason else msg}

    installer = _INSTALLERS.get(name)
    if installer is None:
        return {"ok": False, "error": f"no installer defined for '{name}'"}
    bin_, tmpl, env_hint = installer
    if shutil.which(bin_, path=executor.registry.runtime_path()) is None and \
            shutil.which(bin_) is None:
        return {"ok": False, "error": f"package manager '{bin_}' not found"}

    # ARGUMENT INJECTION. `package` lands in an argv array next to the
    # manager's own flags, so a name beginning with '-' is not a name — it is
    # an option. `install_package("python3", "--target=/etc")` was a flag
    # uv would honour, and every manager here has an equivalent.
    #
    # A permissive charset rather than a per-ecosystem grammar: npm scopes
    # (@scope/name), PyPI extras (pkg[extra]) and version specifiers are all
    # legitimate, and a regex tight enough to encode one manager's rules would
    # reject another's valid input. What matters is that the string cannot be
    # read as an option, which is decided by the first character.
    if not package:
        return {"ok": False, "error": "no package name given"}
    if package[0] == "-":
        return {"ok": False,
                "error": f"invalid package name {package!r}: a name starting "
                         "with '-' would be read as a command-line flag by the "
                         "package manager"}
    if not _PACKAGE_NAME_RE.fullmatch(package):
        return {"ok": False,
                "error": f"invalid package name {package!r}: expected letters, "
                         "digits and . _ - @ / [ ] + ~ = < > ! , or spaces"}
    # `version` lands in the SAME argv slot as `package` (see `spec` below), so
    # it gets the same injection checks — a caller-controlled string that
    # skipped them here was the other half of "install_package accepts", and
    # a leading-hyphen/charset check on `package` alone left it unvalidated.
    if version is not None:
        if not version:
            return {"ok": False, "error": "empty version given"}
        if version[0] == "-":
            return {"ok": False,
                    "error": f"invalid version {version!r}: a version starting "
                             "with '-' would be read as a command-line flag by "
                             "the package manager"}
        if not _PACKAGE_NAME_RE.fullmatch(version):
            return {"ok": False,
                    "error": f"invalid version {version!r}: expected letters, "
                             "digits and . _ - @ / [ ] + ~ = < > ! , or spaces"}
    # A space followed by a '-' is refused outright, allowlist configured or
    # not — see `_has_embedded_flag`'s comment for why this is defensive
    # rather than closing a currently-reachable hole.
    if _has_embedded_flag(package):
        return {"ok": False,
                "error": f"invalid package name {package!r}: a space followed "
                         "by '-' looks like an embedded command-line flag"}
    if version is not None and _has_embedded_flag(version):
        return {"ok": False,
                "error": f"invalid version {version!r}: a space followed by "
                         "'-' looks like an embedded command-line flag"}

    # THE-791: deny-by-default operator allowlist. Checked after the format
    # validation above (so a malformed name is reported as malformed, not as
    # "not allowed") and before every remaining side-effecting step: no
    # session-dir lookup, no cache-dir mkdir, no subprocess.run. (The
    # installer/binary resolution above this point is PATH lookups only —
    # shutil.which never spawns a process or touches the network.)
    denial = _allowlist_denial(name, package)
    if denial:
        return errors.error_result(errors.PERMISSION_DENIED, denial)

    spec = f"{package}=={version}" if version else package

    # session installs go into the session workspace; ad-hoc into the cache
    if session_id:
        from . import sessions
        d = sessions._session_dir(session_id)
        if not d.is_dir():
            return {"ok": False, "error": f"unknown session '{session_id}'"}
        cwd = d
    else:
        cwd = CACHE_ROOT
        cwd.mkdir(parents=True, exist_ok=True)

    # {target} AFTER cwd is known. Substituted separately from {pkg} so a package
    # name can never masquerade as the target path.
    cmd = [p.replace("{target}", str(cwd)).replace("{pkg}", spec) for p in tmpl]

    env = dict(executor._env())
    # {target} in an env hint, not just in argv. Every manager cache is
    # redirected INTO the workspace so nothing under $HOME needs to be
    # writable — see _confinement below for why that matters.
    env.update({k: v.replace("{target}", str(cwd)) for k, v in env_hint.items()})
    confine, unenforced = _confinement(bin_, str(cwd), env, name)

    def _out(result: dict) -> dict:
        """Attach what the confinement could NOT enforce, to every outcome.

        A helper rather than five literals: the field matters most on the
        paths people read least — a timeout or a non-zero exit is exactly when
        someone asks "was this thing contained?" — and a return added later
        that forgot it would answer by omission.
        """
        return {**result, "unenforced": unenforced} if unenforced else result

    try:
        p = subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True,
                           text=True, timeout=600, preexec_fn=confine)
    except subprocess.TimeoutExpired:
        return _out({"ok": False, "error": "package install timed out (600s)"})
    except Exception as exc:
        return _out({"ok": False, "error": f"install failed: {exc}"})

    tail = (p.stdout + p.stderr)[-1200:]
    if p.returncode != 0:
        # A failure caused by layer 1 is named, so "it did not install" is
        # never mistaken for "the package does not exist". The flag is the
        # actionable part: a native addon needs a build step this deliberately
        # refuses to run.
        # Gated on EVIDENCE, not on the flag being present. The first version
        # fired whenever the flag was in argv, so it explained a sandbox failure
        # ("Failed to discover managed Python installations") as a missing
        # build step — a confident, wrong diagnosis attached to an unrelated
        # error, which is worse than no hint at all.
        hint = _NO_EXEC_FLAG.get(name)
        err = f"install failed (rc={p.returncode})"
        _looks_like_build = any(m in tail.lower() for m in
                                ("no matching distribution", "sdist", "source distribution",
                                 "build", "wheel", "prepare_metadata", "gyp"))
        if hint and _looks_like_build:
            err += (f"; note {hint} is passed, so a package needing a build "
                    "step at install time will fail here by design")
        return _out({"ok": False, "error": err, "output_tail": tail})
    # Executed code finds a package only when it lands in the directory the
    # program runs from — sys.path[0] for python, node_modules lookup for node.
    # An ad-hoc install goes to the shared cache, which is NOT that directory, so
    # say so instead of reporting a success the caller cannot use.
    importable = session_id is not None
    return _out({"ok": True, "language": name, "package": spec,
            "target": str(cwd), "importable": importable,
            "note": None if importable else
                    "installed into the shared cache, which executed code does NOT "
                    "have on its import path — pass session_id to install somewhere "
                    "your code can import from",
            "output_tail": tail[-600:]})
