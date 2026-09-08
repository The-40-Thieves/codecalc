"""What this install actually resolved, as data.

`codecalc doctor` existed and printed prose. Prose is right for the operator
reading it and useless to the installer script, the CI job or the agent that
wants to branch on whether the native executor is present — all of which had to
either parse English or make a tool call and read a field out of the result.

So the report is built here as a dict and rendered by the caller. Two
consequences worth having: the text and the JSON cannot disagree, because there
is one source; and the report is testable without spawning a CLI, which is what
made the degraded-environment cases (no binary, no runtimes, unwritable
workspace) reachable at all.

THE FOUR-STATE RUNTIME VOCABULARY, AND WHY IT IS NOT FOUR STATES BY DEFAULT

Doctor is asked to distinguish `supported`, `installed`, `available` and
`unhealthy`. The honest version of that distinction costs something:

    supported   codecalc knows this language; nothing for it resolves here
    installed   its command resolves on the sandbox PATH and is executable
    unhealthy   its command resolves and is NOT executable, or was run and failed
    available   it was actually RUN here and answered

`available` is the only one that requires executing the runtime, and there are
31 of them. Doing that on every `doctor` invocation would turn a diagnostic
into a minute-long build step, so the default reports resolution only —
`installed`, never `available` — and `--deep` is what promotes a resolved
runtime to `available` or demotes it to `unhealthy` by running it.

That split is the point rather than a shortcut. Reporting `available` for a
binary this process only found on PATH would be the exact defect this project
keeps correcting: a field that claims a stronger measurement than was taken.
`status_basis` says which of the two ran, per report, so a reader never has to
infer it.

`unhealthy` HAS TWO CAUSES, DELIBERATELY MERGED

"resolves and is NOT executable" (a file present with the wrong mode) and "was
run and failed" (a --deep hello-world execution, or a version probe that
NEVER EVEN SPAWNED) are the same CLAIM from a caller's point of view — this
row resolved and cannot be trusted to run — even though they are measured
differently.

A version probe that merely TIMED OUT is deliberately NOT folded into that
same claim, and this is true of EVERY command, not a rustc-specific carve-
out: `report()`'s trust rule stopped treating a timeout as evidence for any
`_VERSION_FLAG` entry, and `_probe_version`'s retry runs for every command it
probes. Four separate hosted-runner failures on 2026-09-08 (cold
`windows-latest` images, main among them) prove exactly that breadth: three
were `rustc --version` exceeding the probe's own deadline because the FIRST
invocation on such a host goes through the rustup proxy — an arg-forwarding
shim that has to locate and re-exec the real `rustc.exe` before it can answer
anything, a cost a fresh VM has not amortized — and a fourth, on a LATER
commit than those three, was plain `go version timed out after 10s` with no
proxy involved and no `_PROBE_TIMEOUT_S` entry for `go` at all. Both flipped
a perfectly working `tested`-tier toolchain to `unhealthy`/`healthy: false`
for the identical reason: "the runner did not answer IN TIME" was being read
as "the runner answered and is broken" — a strictly weaker claim than a spawn
failure, and this file no longer treats it as interchangeable with one.
`_probe_version` reports a timeout with `hard_failure=False`, `report()`
never promotes it to `unhealthy` on its own regardless of whether `command`
has a confirmed `_VERSION_FLAG` entry (`_PROBE_TIMEOUT_S` only raises the
per-attempt deadline for a few commands audited as slow-start proxies; it
does not gate whether this rule or the retry apply — see both docstrings),
and the row stays at `installed`/`available` with `probe_error` recording
what happened. The one thing that still overrides this is `_HELLO`: a
language with a hello-world program is arbitrated by whether THAT run
succeeds, completely independent of how the version probe went.

A version probe that merely EXITED NON-ZERO is deliberately NOT treated the
same as one of those, unless this code has been explicitly told the flag it
used is the runtime's real one. `--version` is a GNU convention, not a
universal one: `go --version` exits 2 ("flag provided but not defined" — `go
version` is the real form), `lua --version` exits 1 (`-v` is correct), `zig
--version` exits 1 (`zig version` is correct) — a shipped version of this
file trusted their nonzero exits as brokenness and reported all three
`unhealthy` (go being `tested` tier, this also flipped `healthy` false) on
any host with a perfectly working toolchain. `_VERSION_FLAG` doubles as the
"this flag is confirmed correct for this command" set: a nonzero exit from a
command NOT in it is reported as merely unmeasured (`version` stays None,
`probe_error` is still recorded for a human to read), never `unhealthy`. A
command WITH an entry — java's Apple stub is the canonical case: `-version`
IS its documented flag, and it still exits non-zero printing "Unable to
locate a Java Runtime." on stderr — trusts the nonzero exit, because this
code was TOLD that flag is right and the runtime still failed it. A language
with a hello-world program (`_HELLO`) is stronger evidence still and
overrides either reading: lua is in `_HELLO`, so its wrong-flagged version
probe never gets the final word regardless.

`probe_error` never lands in `version`, which holds a version string or
nothing, never a failure message. `runtime_summary.unhealthy` counts every
TRUSTED cause together; `probe_error`/`detail` says which one applies to a
given row, and a row can carry `probe_error` while still reading `installed`
— that combination means "the version guess didn't work; nothing here says
the runtime itself is broken". That combination is surfaced in the text
renderer under its own "installed, version probe failed" heading and named
one line per runtime in `_remedies()`, phrased as "may still work" — not
just left in the JSON row for a caller to notice on their own.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import (
    __version__,
    contract,
    executor,
    landlock,
    optional,
    providers,
    registry,
    strict_runtime,
)

#: The four states, ordered weakest to strongest claim. Defined in registry.py
#: and re-exported here: `list_languages` reports the same vocabulary,
#: and registry is the module all three readers already import.
#: contract.py's schema still imports it from this name.
RUNTIME_STATES = registry.RUNTIME_STATES

#: Extras, and the modules whose presence defines them. Mirrors what the text
#: renderer used to hardcode.
EXTRAS: dict[str, tuple[str, ...]] = {
    "symbolic": ("sympy", "z3"),
    "parsing": ("tree_sitter_language_pack",),
}

#: Registry entries that are a second spelling of a language already counted.
#: Mirrors `_ALIAS_ENTRIES` in server.py and `ALIAS_ENTRIES` in
#: scripts/check_claims.py — the count this reports has to agree with the
#: README's and the gate's, or it is a fourth opinion on a number this repo
#: already has a gate for.
ALIAS_ENTRIES = frozenset({"c++"})


def primary_command(entry: dict, name: str | None = None) -> str:
    """The command whose presence decides whether a language resolves.

    Mirrors the fallback in `executor.probe()` deliberately, and
    `tests/test_doctor.py` asserts the two agree on every language. Two copies
    of this rule that drift would make `doctor` and the executor disagree about
    which runtimes exist, which is worse than either being wrong on its own.

    For a shell-wrapped language the deciding command is the REAL tool, not
    `bash`: `bash` resolving says nothing about whether gleam is
    installed, and probing it advertised wrapper languages on machines that
    had never seen them. `name` is optional only for callers that predate the
    wrapper distinction; passing it is what makes the answer honest.

    A compiled language's deciding command is ALWAYS its compile tool, never
    `run` — including when `run` is not the `{exe}` template but a literal
    command of its own (kotlin's `run` launches `java -jar ...`). Kotlin used
    to fall through to `run`'s own first token here, so `command` read
    `"java"`: a language reported `installed` from a JRE alone, with no
    Kotlin toolchain anywhere on the host. `registry.secondary_command`
    is what catches `run` needing a SECOND, different tool on top of this one.
    """
    if name is not None and name in registry.SHELL_WRAPPED:
        return registry.WRAPPED_TOOL[name]
    if entry["compile"]:
        return entry["compile"][0]
    cmd = entry["run"][0] if entry["run"] else ""
    if cmd.startswith(("bash", "sh")):
        cmd = "bash"
    return cmd


def _runtime_status(cmd: str) -> tuple[str, str | None]:
    """`(status, resolved_path)` from resolution alone — no execution."""
    if not cmd:
        return "supported", None
    runtime_path = registry.runtime_path()
    path = shutil.which(cmd, path=runtime_path)
    if path is not None:
        return "installed", path

    # `shutil.which` already filters by the execute bit, so a present-but-not-
    # executable command comes back as None and would report `supported` —
    # "codecalc knows this language, nothing for it here" — when the file is
    # sitting right there unable to run. That is the most confusing answer
    # available: the operator can see the binary and doctor says it is absent.
    #
    # The first version of this function had exactly that bug. It called
    # os.access(X_OK) on which()'s result, which can only ever be True, so
    # `unhealthy` was unreachable without --deep and the four-state vocabulary
    # was three states wearing four names. Caught by the test written for it.
    #
    # So the fallback scan is a SECOND pass, deliberately after which(): which()
    # handles PATHEXT on Windows and this does not, and getting that wrong would
    # trade a rare state for a common platform.
    for directory in runtime_path.split(os.pathsep):
        if not directory:
            continue
        candidate = Path(directory) / cmd
        if candidate.is_file():
            return "unhealthy", str(candidate)
    return "supported", None


def _windows_isolation() -> dict:
    """Job Object RESOURCE limits vs AppContainer SECURITY isolation.

    An operator reading doctor on Windows must not conflate the two guarantees.
    The Job Object caps memory, process count and user-mode CPU, and every run
    already names in `unenforced` which of those did NOT bind. AppContainer is a
    different guarantee entirely — a security boundary that denies the payload
    the user profile, the disk outside its workdir, and the network. It is:

      * OFF by default (opt-in via CODECALC_WIN_APPCONTAINER=1), and
      * IMPLEMENTED-BUT-UNVERIFIED on real Windows 11 — the isolation properties
        are only observable on a Win11 desktop, so codecalc discloses
        `appcontainer_isolation_unverified_on_windows` on every run that takes
        the path rather than claiming the boundary was confirmed.

    Reported on every platform (inert off Windows) so the distinction is
    discoverable from the machine-readable report, not just the prose docs.
    """
    on_windows = sys.platform.startswith("win")
    requested = os.environ.get("CODECALC_WIN_APPCONTAINER", "") in {"1", "true", "True"}
    return {
        "applies": on_windows,
        "job_object_resource_limits": (
            "memory, process count and user-mode CPU; per-run honesty in `unenforced`"
        ),
        "appcontainer_security_isolation": {
            "flag": "CODECALC_WIN_APPCONTAINER",
            "default": "off",
            "requested": requested,
            "status": "implemented_unverified_on_windows_11",
            "disclosure_token": "appcontainer_isolation_unverified_on_windows",
            "detail": (
                "AppContainer security isolation is IMPLEMENTED but UNVERIFIED on "
                "real Windows 11; it is distinct from the Job Object's resource "
                "limits and is OFF unless CODECALC_WIN_APPCONTAINER=1."
            ),
        },
    }


def _extensions() -> dict:
    """Capability discovery for the extension SDK.

    Lists every LOADED extension across kinds — id, kind, name, version, origin,
    health, supported operations — plus the operator policy in effect. Built-in
    extensions are always present; whether third-party ones may load is the
    operator's `CODECALC_DISABLE_THIRD_PARTY_EXTENSIONS` decision, surfaced here
    so the trust posture is discoverable from the machine-readable report.
    """
    from codecalc import extensions as ext
    from codecalc.language_packs import configured_language_pack_registry
    from codecalc.renderers import configured_renderer_registry
    from codecalc.verifiers import configured_verifier_registry

    policy = ext.ExtensionPolicy.from_env()
    registries = [
        configured_language_pack_registry(),
        configured_renderer_registry(),
        configured_verifier_registry(),
    ]
    return {
        "framework_version": ext.EXTENSION_FRAMEWORK_VERSION,
        "policy": {
            "allow_third_party": policy.allow_third_party,
            "disable_switch": "CODECALC_DISABLE_THIRD_PARTY_EXTENSIONS",
            "allowed_permissions": sorted(policy.allowed_permissions),
        },
        "loaded": ext.describe_extensions(*registries),
    }


def _grammar_cache() -> dict:
    """Is the tree-sitter grammar cache warm?

    `analyze_complexity` parses with tree-sitter, and the grammars are NOT in
    the wheel — `tree-sitter-language-pack` ships a ~5 MB extension and fetches
    each grammar on first use, in-process, into a local cache (28 grammars,
    89 MB, ~15s cold).

    That is a socket opened from inside the server, which is a surprise on a
    host chosen for being offline. Reported here so it is discoverable BEFORE a
    tool call degrades to `regex-fallback`, rather than after — an operator
    planning an air-gapped install otherwise has no way to find out.

    `cached: false` is not an error. It means the first analysis of each
    language will reach the network; `codecalc-prefetch-grammars` (or, from a
    source checkout, `scripts/prefetch_grammars.py`) warms it.
    """
    if not optional.have("tree_sitter_language_pack"):
        return {"extra_installed": False, "cached": False, "path": None,
                "grammars": 0,
                "detail": "the 'parsing' extra is not installed; "
                          "analyze_complexity uses the regex fallback"}
    try:
        from tree_sitter_language_pack import cache_dir as _cache_dir

        path = str(_cache_dir())
    except Exception as exc:
        return {"extra_installed": True, "cached": False, "path": None,
                "grammars": 0,
                "detail": f"cannot resolve the grammar cache: "
                          f"{type(exc).__name__}: {exc}"}

    # COUNTED, not just existence-checked: an empty cache directory is created
    # by the pack before anything is downloaded, so `path.is_dir()` is true on a
    # completely cold host and would report a warm cache that is not there.
    count = 0
    p = Path(path)
    if p.is_dir():
        count = sum(1 for f in p.iterdir()
                    if f.is_file() and f.suffix in (".so", ".dylib", ".dll", ".pyd"))
    return {
        "extra_installed": True,
        "cached": count > 0,
        "path": path,
        "grammars": count,
        "detail": None if count else (
            "no grammars cached — the first analyze_complexity call per "
            "language will DOWNLOAD one. Run `codecalc-prefetch-grammars` "
            "(installed) or `python scripts/prefetch_grammars.py` (from "
            "source) to warm it, which is what an offline install needs"),
    }


def _workspace_check() -> dict:
    """Can we actually create a workdir? Written, not assumed.

    `tempfile.gettempdir()` returning a path says nothing about whether this
    process may write there — a read-only TMPDIR, a full disk and a correct
    setup all return the same string. Every execution needs a writable workdir,
    so this is the one check whose failure means nothing will work.
    """
    root = tempfile.gettempdir()
    try:
        with tempfile.TemporaryDirectory(prefix="codecalc-doctor-") as d:
            probe = Path(d) / "w"
            probe.write_text("ok", encoding="utf-8")
            probe.read_text(encoding="utf-8")
        return {"path": root, "writable": True, "error": None}
    except OSError as exc:
        return {"path": root, "writable": False,
                "error": f"{type(exc).__name__}: {exc}"}


def _disk_quota() -> dict:
    """Configured session-disk quotas + what is actually used right now.
    Discoverable BEFORE a session hits the ceiling, the same
    shape `_grammar_cache` already gives the grammar cache: an operator
    wiring up a shared host should see these numbers here, not learn them
    from the first `resource_exhausted` a caller reports back.

    Per-session usage is measured for every session `list_sessions()` can
    see — the same on-disk-is-ground-truth reasoning `sessions._global_disk_
    usage` already uses, so a workspace-only session (no worker, so absent
    from `sessions._workers`) still shows up here.
    """
    from . import sessions

    per_session = [
        {"session_id": s["session_id"],
         "usage_bytes": sessions._session_dir_size(s["session_id"])}
        for s in sessions.list_sessions().get("sessions", [])
    ]
    try:
        host_free_bytes = (shutil.disk_usage(sessions.SESSION_ROOT).free
                           if sessions.SESSION_ROOT.exists() else None)
    except OSError:
        host_free_bytes = None
    return {
        "limits": {
            "session_disk_quota_mb": sessions._env_positive_float(
                sessions.SESSION_DISK_QUOTA_MB_ENV,
                sessions._DEFAULT_SESSION_DISK_QUOTA_MB),
            "total_disk_quota_mb": sessions._env_positive_float(
                sessions.TOTAL_DISK_QUOTA_MB_ENV,
                sessions._DEFAULT_TOTAL_DISK_QUOTA_MB),
            "max_artifact_bytes": sessions._max_artifact_bytes(),
            "max_artifact_count": sessions._max_artifact_count(),
            "min_host_free_mb": sessions._env_positive_float(
                sessions.MIN_HOST_FREE_MB_ENV,
                sessions._DEFAULT_MIN_HOST_FREE_MB),
        },
        "global_usage_bytes": sessions._global_disk_usage(),
        "host_free_bytes": host_free_bytes,
        "sessions": per_session,
    }


def report(deep: bool = False) -> dict:
    """Everything `doctor` knows, as data.

    `deep=True` executes each resolved runtime to promote it to `available` or
    demote it to `unhealthy`. Off by default: 31 runtimes is not a diagnostic,
    it is a build.
    """
    backend = executor.backend()
    abi = landlock.abi_version()

    extras = []
    for name, modules in EXTRAS.items():
        missing = [m for m in modules if not optional.have(m)]
        extras.append({
            "name": name,
            "installed": not missing,
            "missing": missing,
            # The exact command, because "install the extra" is not actionable
            # and this is the one place that knows the distribution name.
            "remedy": None if not missing else f"pip install 'codecalc[{name}]'",
        })

    langs = [name for name in sorted(registry.LANGUAGES) if name not in ALIAS_ENTRIES]
    runtimes = []
    for name in langs:
        cmd = primary_command(registry.LANGUAGES[name], name)
        # A plan the platform cannot run reports `supported` — "codecalc knows
        # this language, nothing FOR IT here" — with the reason in `detail`,
        # BEFORE any resolution happens. Resolving the tool first and then
        # deciding would report gleam `installed` on a Windows box where the
        # plan is structurally unable to run: a stronger claim than
        # was measured, which is the defect this file's docstring is about.
        tier = registry.LANGUAGES[name]["tier"]
        if not registry.plan_supported(name, windows=os.name == "nt"):
            runtimes.append({
                "name": name, "command": cmd, "status": "supported",
                "path": None, "version": None, "tier": tier,
                "detail": "the canonical plan needs a POSIX shell to scaffold "
                          "a project; unsupported on Windows",
            })
            continue
        status, path = _runtime_status(cmd)
        # A wrapped plan needs its shell AS WELL as its tool. The tool
        # resolving while bash is absent (minimal containers) is still a plan
        # that cannot run, and `installed` would overstate it.
        if (name in registry.SHELL_WRAPPED and status == "installed"
                and shutil.which("bash", path=registry.runtime_path()) is None):
            status, path = "supported", None
        # A plan whose `run` step needs a SECOND, different tool is not truly
        # installed until THAT resolves too — kotlin's `run` launches
        # `java -jar ...`, and `kotlinc` resolving says nothing about whether
        # a JRE is present. Reporting `installed` from the compile tool alone
        # is exactly the bug: `command` read `"java"` before this file's fix,
        # so kotlin advertised installed from a JRE with no Kotlin toolchain
        # anywhere on the host. See registry.secondary_command's docstring.
        missing_secondary = None
        secondary = registry.secondary_command(registry.LANGUAGES[name])
        if (status == "installed" and secondary is not None
                and shutil.which(secondary, path=registry.runtime_path()) is None):
            missing_secondary = secondary
            status, path = "supported", None
        # `version` is present on every row so a caller never has to branch on
        # the key's existence, and is None unless it was actually read. Under
        # --deep only, for the same reason `available` is: asking 31 runtimes
        # their version is a build, not a diagnostic.
        #
        # `tier` is the RELIABILITY axis, orthogonal to `status`
        # here: `status` is what THIS run resolved/executed just now, `tier`
        # is what codecalc's own CI has verified project-wide. A row can read
        # `status: available` (this host ran it fine under --deep) while
        # `tier: best_effort` (no CI job checks it) — that combination is
        # exactly "works here, but nothing stops it silently breaking".
        version, probe_error, probe_hard_failure, probe_timed_out, probe_ms = (
            _probe_version(cmd, path) if deep and status == "installed"
            else (None, None, False, False, None))
        row = {"name": name, "command": cmd, "status": status, "path": path,
               "tier": tier, "version": version}
        if missing_secondary:
            row["detail"] = (f"{cmd!r} resolves, but {name}'s run step also "
                             f"needs {missing_secondary!r}, which was not "
                             f"found on PATH — install it to run {name}")
        if deep and status == "installed":
            # How long the version probe took, total across every attempt
            # (including a timed-out one that got retried) — a runner-speed
            # flake is otherwise invisible: two reports with identical
            # `status`/`probe_error` could be "answered in 40ms" and
            # "answered after 24s and a retry", and only one of those is a
            # CI image worth investigating.
            if probe_ms is not None:
                row["probe_ms"] = probe_ms
            if probe_error:
                row["probe_error"] = probe_error
            # Only languages with a hello program are promoted, and — CRITICAL
            # — checked FIRST, ahead of `probe_error` below: a nonzero exit
            # from the VERSION probe is not evidence the runtime cannot run
            # code, and a real hello-world execution is strictly stronger
            # evidence than a --version flag guess. Reversing this order
            # (checking `probe_error` first) was a shipped regression: `go
            # --version` exits 2 ("flag provided but not defined"; the real
            # form is `go version`), `lua --version` exits 1 (`lua -v` is
            # correct), `zig --version` exits 1 (`zig version`, no dashes) —
            # none of them are GNU-style, and go is `tested` tier, so
            # `doctor --deep` reported a perfectly working install as
            # `unhealthy`/`healthy: false` on any host with those toolchains.
            # lua IS in `_HELLO`; skipping its hello run because a WRONG
            # version flag "failed" first (the ordering bug) hid the one
            # measurement that would have proven it fine. The same ordering
            # is what makes a TIMED-OUT version probe safe below: a hello
            # language's status never even reads `probe_timed_out` — its own
            # run is the only thing that can demote it.
            if name in _HELLO:
                probe = executor.execute(name, _HELLO[name], timeout=20)
                ran = probe.get("ok") is True and "codecalc" in (probe.get("stdout") or "")
                row["status"] = "available" if ran else "unhealthy"
                if not ran:
                    row["detail"] = (probe.get("error")
                                     or (probe.get("stderr") or "")[:200]
                                     or f"verdict={probe.get('verdict')}")
            elif probe_error:
                # No hello program to arbitrate, so this is the only signal
                # there is — and it is trusted ONLY when it is real evidence
                # of brokenness, not a guessed flag this runtime never spoke,
                # and not a runner that was merely slow to answer:
                #
                #   HARD failure (never even SPAWNED — a resolved path that
                #   refuses to execute at all) — always trusted. A command
                #   `_runtime_status` just found on PATH that will not run AT
                #   ALL is broken regardless of which flag was used.
                #
                #   TIMEOUT (spawned, never answered in time) — NEVER trusted
                #   alone, confirmed flag or not, and this is a rule about
                #   the CLAIM a timeout makes, not about which command made
                #   it. Four 2026-09-08 hosted-runner failures were exactly
                #   this: three were a cold `windows-latest` image's first
                #   `rustc --version` going through the rustup proxy and
                #   exceeding the probe deadline; a fourth, on a later
                #   commit, was plain `go version` doing the same with no
                #   proxy and no per-command timeout override at all — both
                #   flipped a perfectly working `tested`-tier toolchain to
                #   `unhealthy`/`healthy: false`. "did not answer in time" is
                #   not "answered and is broken" — see `_probe_version`'s
                #   docstring for the retry and the
                #   per-command timeout table this also gained.
                #
                #   SOFT failure (ran to completion, exited non-zero) —
                #   trusted ONLY when `cmd` has an explicit `_VERSION_FLAG`
                #   entry, i.e. this code has been TOLD that flag is the
                #   correct one for this runtime and it still failed. Apple's
                #   java stub is exactly this case: `-version` IS java's
                #   documented flag (the `_VERSION_FLAG` override below), it
                #   exits non-zero printing "Unable to locate a Java
                #   Runtime.", and java is not in `_HELLO`, so this is the
                #   only measurement of it `doctor` ever takes. A command
                #   still on the untested DEFAULT `--version` guess getting a
                #   nonzero exit proves nothing except that the guess was
                #   probably wrong — go, lua and zig before their
                #   `_VERSION_FLAG` entries existed were exactly this, and
                #   demoting them was reporting a failure this code's own
                #   wrong guess manufactured.
                if probe_hard_failure or (not probe_timed_out and cmd in _VERSION_FLAG):
                    row["status"] = "unhealthy"
                    row["detail"] = f"version probe failed: {probe_error}"
                # else: leave `status` at `installed`. Two shapes land here
                # now: a nonzero exit on an UNCONFIRMED flag (unchanged,
                # #282), and ANY timeout (new — confirmed flag or not, a
                # timeout is never trusted on its own). `version` already
                # stayed None (see _probe_version) — "not measured", not "no
                # version" — and `probe_error` is still recorded above for a
                # human to see WHY, without it driving `status` on a guess.
        runtimes.append(row)

    summary = {state: sum(1 for r in runtimes if r["status"] == state)
               for state in RUNTIME_STATES}
    # the reliability-tier mirror of `summary` above. Same shape,
    # different axis — this counts by how much CI has verified each language,
    # not by what this host just resolved.
    tier_summary = {tier: sum(1 for r in runtimes if r["tier"] == tier)
                    for tier in registry.RELIABILITY_TIERS}
    workspace = _workspace_check()
    provider_registry = providers.configured_registry()
    execution_providers = []
    for descriptor in provider_registry.descriptors():
        row = dict(descriptor)
        if descriptor["host_class"] in {"local", providers.strict_host_platform()}:
            health = provider_registry.select(descriptor["provider_id"]).health()
            row["ready"] = health["ready"]
            row["technology"] = health.get("technology")
            row["detail"] = health.get("error")
        else:
            # Doctor must not turn into an unsolicited remote network probe.
            row["ready"] = None
            row["technology"] = None
            row["detail"] = "remote readiness not probed by doctor"
        execution_providers.append(row)

    # `healthy` is deliberately NARROW. A missing optional extra and an
    # uninstalled Haskell are ordinary facts about a host, not faults, and
    # exiting non-zero for them would make `doctor` useless as the install
    # check it is meant to be. What makes an install unhealthy is
    # that it cannot execute anything: no workspace to run in, or no backend.
    #
    # One more thing is narrow enough to belong here: a `tested`-tier
    # runtime that is `unhealthy` — resolved but genuinely broken, whether
    # that came from a non-executable file or a failed --deep probe. `tested`
    # is the tier a CI job actually executes and asserts on every PR (see
    # RELIABILITY_TIERS); an UNINSTALLED language of any tier stays
    # `supported`, an ordinary fact about the host, and does not flip this —
    # only ONE that resolved and then proved broken does, because that is
    # codecalc's own advertised guarantee failing, not a host missing an
    # optional toolchain. `best_effort`/`plan_only` runtimes (java, kotlin,
    # ...) never reach here regardless of how broken their probe is — the
    # whole reason those tiers exist is that nothing promised they work.
    tested_broken = sorted({r["name"] for r in runtimes
                            if r["tier"] == "tested" and r["status"] == "unhealthy"})
    healthy = (bool(workspace["writable"]) and backend in ("rust", "python")
               and not tested_broken)

    return {
        # Under the result contract's version and policy rather than a third
        # number of its own. This is machine-readable output a client programs
        # against, which is what that policy is for; a separate doctor version
        # would mean two documents to keep in step and two policies to read.
        "contract_version": contract.CONTRACT_VERSION,
        "codecalc_version": __version__,
        "healthy": healthy,
        "python": {"version": sys.version.split()[0], "platform": sys.platform},
        "backend": {
            "kind": backend,
            "binary": executor._rust,
            # Named rather than left as an absence: "no binary" and "a binary
            # that would not run" are different problems with different fixes.
            "detail": None if backend == "rust" else (
                "native executor not found — running the pure-Python fallback, "
                "which cannot enforce no_net. Set CODECALC_REQUIRE_NATIVE=1 to "
                "make this a startup failure instead of a weaker sandbox."),
        },
        "execution_providers": execution_providers,
        # The gVisor strict boundary's MEASURED prerequisites. Doctor
        # calls the same host probe the runtime uses, so a `runsc` that doctor
        # says is registered is the one the boundary attests. Cheap by default —
        # `docker info` and an image lookup, resolution not execution, the same
        # split `status_basis` names for runtimes; `--deep` promotes it to a real
        # container startup canary that confirms, out of band, that the image
        # genuinely ran under runsc. Fail-closed: an unmet prerequisite makes
        # `available` false and names the reason, which is what a host WITHOUT
        # runsc reports — a fact about the host, so it never fails the install.
        "strict_runtime": strict_runtime.check_prerequisites(deep=deep),
        "install_sandbox": {
            "landlock_abi": abi,
            "confined": bool(abi),
            "detail": None if abi else "installs are not confined on this host",
        },
        # Two DIFFERENT Windows guarantees, kept apart on purpose. The Job Object
        # gives RESOURCE limits (memory, process count, user-mode CPU) and its
        # honesty lives in each run's `unenforced`. AppContainer gives SECURITY
        # isolation (payload denied the user profile, the disk outside its
        # workdir, and the network) — a separate thing, opt-in and OFF by
        # default, and IMPLEMENTED-BUT-UNVERIFIED on real Windows 11.
        "windows_isolation": _windows_isolation(),
        # Extension SDK capability discovery: what's loaded, of what
        # origin, and the operator's third-party policy.
        "extensions": _extensions(),
        "extras": extras,
        # Where analyze_complexity's grammars come from, and whether they are
        # here yet. Not an extra and not a runtime: it is a network dependency
        # of a tool, which neither of those blocks describes.
        "grammar_cache": _grammar_cache(),
        # Session disk quotas: configured ceilings plus current
        # usage, so an operator can see how close to them a host already is.
        "disk_quota": _disk_quota(),
        # Which measurement produced the statuses above. Without this a reader
        # cannot tell `installed` ("found on PATH") from a --deep run that
        # simply found nothing runnable.
        "status_basis": "executed" if deep else "resolved",
        "runtimes": runtimes,
        "runtime_summary": summary,
        "tier_summary": tier_summary,
        "workspace": workspace,
        "skill_file": _skill_path(),
        "remedies": _remedies(backend, extras, runtimes, workspace),
    }


#: A one-line program per language would be 31 more things to keep correct, and
#: `--deep` only needs to prove the runtime answers at all. Languages absent
#: here are still resolved and reported; they are simply not promoted.
#:
#: `awk` is here for the exact reason lua/go/zig needed `_VERSION_FLAG`
#: overrides: `--version` is not a universal convention, and unlike those
#: three, it is not even a universal convention FOR A SINGLE COMMAND NAME —
#: `awk` resolves to genuinely different, non-interoperable programs across
#: hosts (GNU awk accepts `--version`; BSD/macOS one-true-awk and busybox awk
#: reject GNU long options outright and exit non-zero). This box's own `awk`
#: is GNU awk, so `_VERSION_FLAG["awk"] = "--version"` is correct HERE and
#: kept — but trusting that confirmed-here flag on every host would repeat
#: #282's own regression for a command #282 never audited: a working BSD awk
#: demoted to `unhealthy` because its GNU-flavored flag failed. A
#: `BEGIN{print ...}` hello-world is portable POSIX awk syntax across every
#: implementation and arbitrates BEFORE the version probe gets a say
#: regardless — see the ordering note in `report()` below — so a real BSD
#: awk now reads `available` (hello ran) with `probe_error` still recorded
#: and `status` never demoted, rather than depending on which awk flavor
#: happened to be on the audit box.
#:
#: `sqlite3`/`jq`/`zsh` are added alongside it even though none of the three
#: has a known divergent fork (each is a single upstream project with a
#: documented `--version`, unlike awk) — a trivial constant-printing program
#: was cheap for all three, and a hello-world is strictly stronger evidence
#: than a flag guess regardless of how confident that guess is. The other
#: 20+ audited-default entries in `_VERSION_FLAG` are NOT given one here:
#: each is a single-vendor, cross-platform-consistent CLI with `--version`
#: in its own published reference (dotnet, node, rustc, python3's siblings,
#: ...) — see the PR body's per-command portability note — and adding a
#: hello-world to every best_effort language "just in case" would be a
#: second thing to keep correct for no evidence of the same risk.
_HELLO = {
    "python3": 'print("codecalc")',
    "node": 'console.log("codecalc")',
    "ruby": 'puts "codecalc"',
    "perl": 'print "codecalc\\n";',
    "php": '<?php echo "codecalc\\n";',
    "lua": 'print("codecalc")',
    "bash": 'echo codecalc',
    "awk": 'BEGIN{print "codecalc"}',
    "sqlite": "SELECT 'codecalc';",
    "jq": '"codecalc"',
    "zsh": 'echo codecalc',
}


#: How to ask a runtime its version. Keyed by COMMAND rather than by language,
#: because `primary_command` collapses several languages onto one binary and
#: asking `bash` its version four times would be three wasted spawns.
#:
#: Membership — NOT the flag value — is what matters to `report()`: this map
#: doubles as "this code has been TOLD the correct flag for this command",
#: and a NONZERO exit is trusted as real evidence of brokenness only for a
#: command listed here (see `report()`'s `probe_hard_failure or cmd in
#: _VERSION_FLAG` check). A command with no entry that exits nonzero is
#: reported as merely unmeasured, not broken — `status` stays `installed`,
#: `probe_error` is still recorded, and both the text renderer's "installed,
#: version probe failed" block and `_remedies()` name it, phrased as "may
#: still work" rather than overclaimed as broken.
#:
#: Originally held only the three GNU exceptions (`go`/`lua`/`zig`) plus
#: java/kotlinc's non-GNU `-version`, on the theory that "the map holds only
#: the exceptions, so a runtime added later works without an entry" — but
#: that meant every language still answering the DEFAULT `--version`
#: correctly (the common case) was "unmeasured" the moment its real-world
#: exit code ever went nonzero on some other host, for no better reason than
#: nobody had confirmed the default there either. So it now lists EVERY
#: registered runtime's command (`escript`/`tclsh` excluded — see
#: `_NO_VERSION`, they are never probed at all), confirmed flag or default
#: alike, and "unmeasured" is the rare exception (a newly added, not-yet-
#: audited runtime) rather than the common case.
#:
#: AUDITED LIVE on this box (mise-managed toolchains, ARM64 Ubuntu 24.04) —
#: every command below was actually run with the flag shown and confirmed to
#: exit 0 (or, for the five overrides, confirmed that the override is the
#: one that does): see the PR body for the full per-command
#: command/flag/exit-code/first-line table. `go --version` exits 2 ("flag
#: provided but not defined: -version"; go takes `version` as a SUBCOMMAND,
#: no dashes). `lua --version` exits 1 ("unrecognized option"; `-v` is
#: correct). `zig --version` exits 1 (prints a usage banner; `zig version`,
#: no dashes, is correct — the same subcommand shape as go). `java`/`kotlinc`
#: predate the GNU convention and use `-version` (also why both stdout and
#: stderr are read below — java's stub prints to stderr). Every other
#: audited command answered plain `--version` with exit 0 and is listed with
#: that value explicitly, rather than left to the implicit default, so its
#: membership here — not just its flag — is on record.
#:
#: NOT the same guarantee for every entry, though: `awk` (and, belt-and-
#: suspenders, `sqlite3`/`jq`/`zsh`) are ALSO in `_HELLO`, which arbitrates
#: status ahead of whatever this map says — `awk`'s `--version` is confirmed
#: correct only for the GNU awk THIS box happens to run, and a BSD/macOS/
#: busybox awk that rejects it is proven fine by the hello-world instead of
#: being demoted on a flag guess this map cannot vouch for everywhere. Every
#: other entry here has no hello-world backstop because it is a single-
#: vendor, cross-platform-consistent CLI with `--version` in its own
#: published reference (see the PR body's per-command portability note) —
#: `awk` is the one command name on this list that resolves to genuinely
#: different, non-interoperable programs depending on the host.
_VERSION_FLAG: dict[str, str] = {
    "java": "-version",
    "kotlinc": "-version",
    "go": "version",
    "lua": "-v",
    "zig": "version",
    "awk": "--version",
    "bash": "--version",
    "bun": "--version",
    "gcc": "--version",
    "g++": "--version",
    "gfortran": "--version",
    "dotnet": "--version",
    "deno": "--version",
    "elixir": "--version",
    "gleam": "--version",
    "nix-shell": "--version",
    "jq": "--version",
    "mojo": "--version",
    "node": "--version",
    "perl": "--version",
    "php": "--version",
    "python3": "--version",
    "Rscript": "--version",
    "ruby": "--version",
    "rustc": "--version",
    "sqlite3": "--version",
    "swift": "--version",
    "zsh": "--version",
}

#: Commands with no version flag worth calling. `escript` and `tclsh` have no
#: non-interactive `--version`, and invoking them without one starts a REPL that
#: would hang until the timeout. Listed rather than discovered, because "it hung
#: for 10 seconds" is a bad way to learn this.
_NO_VERSION = frozenset({"escript", "tclsh"})

#: The ordinary per-attempt deadline for a version probe. Generous already —
#: `report()`'s own docstring calls --deep "a build, not a diagnostic" for
#: asking 31 runtimes anything at all — but not generous enough for the
#: commands in `_PROBE_TIMEOUT_S` below on a cold host.
_DEFAULT_PROBE_TIMEOUT_S = 10.0

#: Per-command override of `_DEFAULT_PROBE_TIMEOUT_S`, for commands whose
#: FIRST invocation on a cold host routes through a slow-start proxy rather
#: than the real binary — the 10s default is a real deadline for a warm
#: process and a coin flip for one of these:
#:
#:   rustc    rustup's installed `rustc` is a tiny arg-forwarding shim that
#:            has to locate and re-exec the real toolchain component on its
#:            first call after a runner cold-boots; nothing amortizes that
#:            across a fresh VM. This is the exact command that produced
#:            three separate hosted-runner failures on 2026-09-08 (cold
#:            `windows-latest` images), all `rustc --version` exceeding the
#:            old 10s default.
#:   dotnet   the muxer resolves and JITs the SDK CLI on its first call.
#:   java, kotlinc  a cold JVM pays class loading and JIT warmup that a
#:            container image's page cache has not amortized yet.
#:   swift    the toolchain driver does its own first-call discovery, the
#:            same shape as `dotnet`.
#:
#: NOT the safety net — the retry in `_probe_version` and `report()`'s
#: refusal to trust a bare timeout apply to EVERY command whether or not it
#: is listed here. This table only buys a probe more time before it counts
#: as one; a command absent from it (`go`, `node`, `python3`, ...) still
#: gets the retry and the same non-demotion on a timeout, just at the plain
#: 10s default — which is exactly how a LATER 2026-09-08 hosted-runner
#: failure reproduced with plain `go version`, no proxy and no entry here at
#: all, and was fixed by the same two changes rather than a fifth entry.
#:
#: Measured on THIS host (mise-managed rustc 1.97.1, ARM64 Ubuntu 24.04 —
#: already warm, no rustup proxy in the path): `rustc --version` answers in
#: ~60-70ms whether it is the first call in the process or the fifth, so
#: this host cannot reproduce the slow-start cost itself — see the PR body
#: for the reproduction against a fake proxy that actually blocks, which is
#: what proves the retry and the raised ceiling below actually help.
_PROBE_TIMEOUT_S: dict[str, float] = {
    "rustc": 25.0,
    "dotnet": 25.0,
    "java": 25.0,
    "kotlinc": 25.0,
    "swift": 25.0,
}


def _probe_version(command: str, path: str | None) -> (
        tuple[str | None, str | None, bool, bool, float | None]):
    """`(version, probe_error, hard_failure, timed_out, probe_ms)`.

    `version`/`probe_error` are never both non-None. NONE MEANS NOT MEASURED,
    NEVER "no version". Doctor is asked to report versions; it is not asked
    to invent them. A runtime that has no version flag, or that answers on
    exit 0 with nothing parseable, is simply not measured — `version` is
    None and `probe_error` stays None too, because NEITHER case is evidence
    the runtime is broken.

    `hard_failure` is the caller's (`report()`'s) signal for how much to
    TRUST a non-None `probe_error` as evidence of brokenness, and is False
    whenever `probe_error` is None:

      True   the probe never even SPAWNED — a resolved path that refuses to
             execute at all (permission revoked, file removed between
             `_runtime_status` and here). That is real evidence regardless
             of which flag was used: a command `_runtime_status` just found
             on PATH that will not run AT ALL is broken.
      False  either the probe RAN to completion and exited non-zero, or it
             TIMED OUT without ever answering. `timed_out` (below) is what
             tells those two apart — they used to share `hard_failure=True`,
             and that was the bug: a nonzero exit and "never got an answer
             in time" are not the same claim about the runtime.

    A nonzero exit is trusted as real evidence ONLY when the flag used is one
    this code has been explicitly TOLD is correct for `command`
    (`_VERSION_FLAG` has an entry) — this is the exact case Apple's java stub
    reproduces: `-version` IS java's documented flag, and it still exits
    non-zero printing "Unable to locate a Java Runtime." A command still on
    the untested `--version` DEFAULT that exits non-zero proves nothing
    except that the guess was probably wrong: `go --version` exits 2 (`go
    version` is correct), `lua --version` exits 1 (`-v` is correct), `zig
    --version` exits 1 (`zig version` is correct) — none of them speak GNU
    `--version`, and reporting any of them `unhealthy` from this alone is a
    failure this function's own wrong guess manufactured, not one the
    runtime produced. The caller decides what "trust" means for a soft
    failure (`cmd in _VERSION_FLAG`); this function only reports what kind of
    failure it was.

    `timed_out` is True only when NEITHER attempt (see the retry below)
    answered before its deadline. A caller (`report()`) must NEVER trust a
    timeout as evidence of brokenness on its own, confirmed flag or not, FOR
    ANY COMMAND — not only the ones in `_PROBE_TIMEOUT_S`: "did not answer in
    time" measures the runner, not the runtime, and treating it like a hard
    failure is exactly what turned cold `windows-latest` probes of both
    `rustc` (three hits, via the rustup proxy) and plain `go` (a fourth, no
    proxy, no per-command entry) into a false `unhealthy`/`healthy: false`
    on a perfectly working toolchain. See this module's docstring for the
    incident and `_PROBE_TIMEOUT_S` for the mitigation at the source — that
    table only raises the per-attempt deadline for a few audited proxies; it
    is not what makes a timeout stop being trusted, `report()`'s rule is.

    Retried ONCE before a timeout is reported: a slow-start proxy (see
    `_PROBE_TIMEOUT_S`) has usually already resolved the real binary by its
    second invocation, so the retry is warm and often answers well inside
    the deadline — this is the cheaper half of the fix, catching the case
    the raised per-command ceiling does not. `probe_ms` is the TOTAL wall
    time across every attempt (so "answered promptly" and "answered, but
    only after a retry" are distinguishable from the report alone, without a
    human re-running `--deep` by hand); it is `None` only when no attempt was
    made at all (no path, or `command` has no version flag worth calling).

    Runs under the executor's 23-entry env allowlist rather than the doctor
    process's own environment. This is a diagnostic, but it is still spawning
    host binaries, and there is no reason for `gcc --version` to see an API key.
    """
    if not path or command in _NO_VERSION:
        return None, None, False, False, None
    flag = _VERSION_FLAG.get(command, "--version")
    timeout_s = _PROBE_TIMEOUT_S.get(command, _DEFAULT_PROBE_TIMEOUT_S)
    start = time.monotonic()
    proc = None
    for attempt in range(2):
        try:
            proc = subprocess.run(
                [path, flag],
                capture_output=True, timeout=timeout_s, env=executor._env(), check=False,
            )
            break
        except subprocess.TimeoutExpired:
            if attempt == 0:
                # One retry only: a slow-start proxy is warm by its second
                # call, and a third attempt would only double the cost paid
                # by a command that is genuinely hung.
                continue
            probe_ms = (time.monotonic() - start) * 1000
            return (None,
                    f"{command} {flag} timed out after {timeout_s:g}s (2 attempts)",
                    False, True, probe_ms)
        except (OSError, subprocess.SubprocessError) as exc:
            # A resolved path that fails to even SPAWN (permission revoked,
            # file removed, between `_runtime_status` and here) is the same
            # kind of broken-not-missing signal as a nonzero exit — not "not
            # measured", and not a timeout either, so it is never retried.
            probe_ms = (time.monotonic() - start) * 1000
            return None, f"{command} {flag}: {exc}", True, False, probe_ms
    probe_ms = (time.monotonic() - start) * 1000
    # stdout OR stderr: `java -version` uses stderr, and a runtime that answers
    # on the stream we did not read is indistinguishable from one that did not
    # answer at all.
    text = ""
    for stream in (proc.stdout, proc.stderr):
        decoded = (stream or b"").decode("utf-8", "replace").strip()
        for line in decoded.splitlines():
            line = line.strip()
            if line:
                text = line[:200]
                break
        if text:
            break
    if proc.returncode != 0:
        # Whatever it printed — the Apple-stub message, a usage banner, or
        # nothing at all — is a FAILURE report, not a version. `version` in
        # the returned pair stays None on this branch unconditionally. This
        # is a SOFT failure (it ran); `hard_failure=False` regardless of
        # `command` — the caller is the one that knows whether this
        # `command`'s flag is trusted, not this function.
        return None, text or f"{command} {flag} exited {proc.returncode}", False, False, probe_ms
    # First non-empty line, capped. Some runtimes print a paragraph (gcc
    # prints its licence), and the report is a diagnostic, not a transcript.
    return (text[:120] or None), None, False, False, probe_ms


def _runtime_version(command: str, path: str | None) -> str | None:
    """The runtime's own version string, or None when it could not be read —
    the half of `_probe_version` most callers (and tests) only need."""
    return _probe_version(command, path)[0]


def _skill_path() -> str | None:
    p = Path(__file__).resolve().parent / "SKILL.md"
    return str(p) if p.is_file() else None


def _remedies(backend: str, extras: list, runtimes: list, workspace: dict) -> list[str]:
    """What to do about it, in the order it matters."""
    out = []
    if not workspace["writable"]:
        out.append(f"workspace {workspace['path']} is not writable "
                   f"({workspace['error']}) — nothing can execute until it is; "
                   f"set TMPDIR to a writable directory")
    if backend != "rust":
        out.append("build the native executor (`cargo build --release` in "
                   "executor/, then copy it to bin/) or install a platform "
                   "wheel — the fallback cannot enforce no_net")
    for e in extras:
        if e["remedy"]:
            out.append(e["remedy"])
    # `probe_error` rows failed a REAL measurement (nonzero exit / timeout) —
    # named separately from a plain permission-mode `unhealthy` because the
    # fix is different: install a working runtime, not `chmod +x`.
    probed_broken = [r for r in runtimes
                     if r["status"] == "unhealthy" and r.get("probe_error")]
    probed_broken_names = {r["name"] for r in probed_broken}
    for r in probed_broken:
        out.append(f"{r['name']}: resolved on PATH ({r['command']}) but its "
                   f"own probe failed ({r['probe_error'][:100]}) — install a "
                   f"working {r['name']} runtime")
    # A row that stayed `installed` despite a `probe_error` was DELIBERATELY
    # not trusted as broken — no confirmed `_VERSION_FLAG` entry (or
    # `_HELLO` run) backs the nonzero exit, so it may just be a wrong flag
    # guess rather than a broken runtime (see report()'s conservative rule).
    # That combination used to be disclosed in the JSON row only — nothing
    # here or in the text renderer named it — so an operator reading the
    # prose had no way to learn the probe even ran. Named separately from
    # `probed_broken` above and phrased so it is never read as "broken".
    for r in runtimes:
        if r["status"] == "installed" and r.get("probe_error"):
            out.append(f"{r['name']}: version probe failed; the runtime may "
                       f"still work ({r['probe_error'][:100]})")
    unhealthy = [r["name"] for r in runtimes
                 if r["status"] == "unhealthy" and r["name"] not in probed_broken_names]
    if unhealthy:
        out.append(f"resolved but not runnable: {', '.join(unhealthy)} — "
                   f"check the file mode and the runtime PATH")
    # A language whose compile tool resolved but whose run step needs a
    # SECOND tool that did not (kotlin: kotlinc without java) reports
    # `supported`, not `unhealthy` — nothing about it is broken, it is simply
    # half-installed. Named here rather than folded into the generic
    # `supported` case because `detail` already has the exact missing binary.
    half_installed = [r for r in runtimes
                      if r["status"] == "supported" and "run step also needs" in (r.get("detail") or "")]
    for r in half_installed:
        out.append(f"{r['name']}: {r['detail']}")
    return out
