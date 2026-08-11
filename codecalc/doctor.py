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
import sys
import tempfile
from pathlib import Path

from . import __version__, contract, executor, landlock, optional, registry

#: The four states, ordered weakest to strongest claim. Exported so the schema
#: and the tests use the same list rather than three transcriptions of it.
RUNTIME_STATES = ("supported", "installed", "unhealthy", "available")

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


def primary_command(entry: dict) -> str:
    """The command whose presence decides whether a language resolves.

    Mirrors the fallback in `executor.probe()` deliberately, and
    `tests/test_doctor.py` asserts the two agree on every language. Two copies
    of this rule that drift would make `doctor` and the executor disagree about
    which runtimes exist, which is worse than either being wrong on its own.
    """
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
        cmd = primary_command(registry.LANGUAGES[name])
        status, path = _runtime_status(cmd)
        row = {"name": name, "command": cmd, "status": status, "path": path}
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
        "install_sandbox": {
            "landlock_abi": abi,
            "confined": bool(abi),
            "detail": None if abi else "installs are not confined on this host",
        },
        "extras": extras,
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
