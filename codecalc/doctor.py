"""What this install actually resolved, as data (THE-780).

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

THE-780 asks doctor to distinguish `supported`, `installed`, `available` and
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
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
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
#: and re-exported here: `list_languages` reports the same vocabulary now
#: (THE-817), and registry is the module all three readers already import.
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
    `bash` (THE-835): `bash` resolving says nothing about whether gleam is
    installed, and probing it advertised wrapper languages on machines that
    had never seen them. `name` is optional only for callers that predate the
    wrapper distinction; passing it is what makes the answer honest.
    """
    if name is not None and name in registry.SHELL_WRAPPED:
        return registry.WRAPPED_TOOL[name]
    cmd = entry["run"][0] if entry["run"] else ""
    if cmd.startswith("{"):
        cmd = (entry["compile"] or ["bash"])[0] if entry["compile"] else "bash"
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
    """Job Object RESOURCE limits vs AppContainer SECURITY isolation (THE-829).

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


def _grammar_cache() -> dict:
    """Is the tree-sitter grammar cache warm? (THE-821)

    `analyze_complexity` parses with tree-sitter, and the grammars are NOT in
    the wheel — `tree-sitter-language-pack` ships a ~5 MB extension and fetches
    each grammar on first use, in-process, into a local cache (28 grammars,
    89 MB, ~15s cold).

    That is a socket opened from inside the server, which is a surprise on a
    host chosen for being offline. Reported here so it is discoverable BEFORE a
    tool call degrades to `regex-fallback`, rather than after — an operator
    planning an air-gapped install otherwise has no way to find out.

    `cached: false` is not an error. It means the first analysis of each
    language will reach the network; `scripts/prefetch_grammars.py` warms it.
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
            "language will DOWNLOAD one. Run scripts/prefetch_grammars.py to "
            "warm it, which is what an offline install needs"),
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
        # plan is structurally unable to run (THE-835): a stronger claim than
        # was measured, which is the defect this file's docstring is about.
        if not registry.plan_supported(name, windows=os.name == "nt"):
            runtimes.append({
                "name": name, "command": cmd, "status": "supported",
                "path": None, "version": None,
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
        # `version` is present on every row so a caller never has to branch on
        # the key's existence, and is None unless it was actually read. Under
        # --deep only, for the same reason `available` is: asking 31 runtimes
        # their version is a build, not a diagnostic.
        row = {"name": name, "command": cmd, "status": status, "path": path,
               "version": _runtime_version(cmd, path) if deep else None}
        # Only languages with a hello program are promoted. Running the others
        # with an EMPTY source would execute a no-op, find no "codecalc" in its
        # stdout, and demote a perfectly good runtime to `unhealthy` — a --deep
        # run reporting failures it manufactured itself. They stay `installed`,
        # which is what was actually measured about them.
        if deep and status == "installed" and name in _HELLO:
            probe = executor.execute(name, _HELLO[name], timeout=20)
            ran = probe.get("ok") is True and "codecalc" in (probe.get("stdout") or "")
            row["status"] = "available" if ran else "unhealthy"
            if not ran:
                row["detail"] = (probe.get("error")
                                 or (probe.get("stderr") or "")[:200]
                                 or f"verdict={probe.get('verdict')}")
        runtimes.append(row)

    summary = {state: sum(1 for r in runtimes if r["status"] == state)
               for state in RUNTIME_STATES}
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
    # exiting non-zero for them would make `doctor` useless as an install check
    # — the thing THE-780 wants it to be. What makes an install unhealthy is
    # that it cannot execute anything: no workspace to run in, or no backend.
    healthy = bool(workspace["writable"]) and backend in ("rust", "python")

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
        # The gVisor strict boundary's MEASURED prerequisites (THE-828). Doctor
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
        "extras": extras,
        # Where analyze_complexity's grammars come from, and whether they are
        # here yet. Not an extra and not a runtime: it is a network dependency
        # of a tool, which neither of those blocks describes.
        "grammar_cache": _grammar_cache(),
        # Which measurement produced the statuses above. Without this a reader
        # cannot tell `installed` ("found on PATH") from a --deep run that
        # simply found nothing runnable.
        "status_basis": "executed" if deep else "resolved",
        "runtimes": runtimes,
        "runtime_summary": summary,
        "workspace": workspace,
        "skill_file": _skill_path(),
        "remedies": _remedies(backend, extras, runtimes, workspace),
    }


#: A one-line program per language would be 31 more things to keep correct, and
#: `--deep` only needs to prove the runtime answers at all. Languages absent
#: here are still resolved and reported; they are simply not promoted.
_HELLO = {
    "python3": 'print("codecalc")',
    "node": 'console.log("codecalc")',
    "ruby": 'puts "codecalc"',
    "perl": 'print "codecalc\\n";',
    "php": '<?php echo "codecalc\\n";',
    "lua": 'print("codecalc")',
    "bash": 'echo codecalc',
}


#: How to ask a runtime its version. Keyed by COMMAND rather than by language,
#: because `primary_command` collapses several languages onto one binary and
#: asking `bash` its version four times would be three wasted spawns.
#:
#: `--version` is the default and the map holds only the exceptions, so a
#: runtime added later works without an entry. An exception is not a special
#: case for its own sake: `java -version` predates the GNU convention and
#: prints to STDERR, which is why both streams are read below.
_VERSION_FLAG: dict[str, str] = {
    "java": "-version",
    "kotlinc": "-version",
}

#: Commands with no version flag worth calling. `escript` and `tclsh` have no
#: non-interactive `--version`, and invoking them without one starts a REPL that
#: would hang until the timeout. Listed rather than discovered, because "it hung
#: for 10 seconds" is a bad way to learn this.
_NO_VERSION = frozenset({"escript", "tclsh"})


def _runtime_version(command: str, path: str | None) -> str | None:
    """The runtime's own version string, or None when it could not be read.

    NONE MEANS NOT MEASURED, NEVER "no version". THE-780 asks doctor to report
    versions; it does not ask it to invent them. A runtime that has no version
    flag, times out, crashes, or answers with something unparsable all produce
    `None` here and leave `status` untouched — a version that could not be read
    says nothing about whether the runtime works, and demoting it would report a
    failure this function manufactured.

    Runs under the executor's 23-entry env allowlist rather than the doctor
    process's own environment. This is a diagnostic, but it is still spawning
    host binaries, and there is no reason for `gcc --version` to see an API key.
    """
    if not path or command in _NO_VERSION:
        return None
    flag = _VERSION_FLAG.get(command, "--version")
    try:
        proc = subprocess.run(
            [path, flag],
            capture_output=True, timeout=10, env=executor._env(), check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # stdout OR stderr: `java -version` uses stderr, and a runtime that answers
    # on the stream we did not read is indistinguishable from one that did not
    # answer at all.
    for stream in (proc.stdout, proc.stderr):
        text = (stream or b"").decode("utf-8", "replace").strip()
        for line in text.splitlines():
            line = line.strip()
            if line:
                # First non-empty line, capped. Some runtimes print a paragraph
                # (gcc prints its licence), and the report is a diagnostic, not
                # a transcript.
                return line[:120]
    return None


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
    unhealthy = [r["name"] for r in runtimes if r["status"] == "unhealthy"]
    if unhealthy:
        out.append(f"resolved but not runnable: {', '.join(unhealthy)} — "
                   f"check the file mode and the runtime PATH")
    return out
