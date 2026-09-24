#!/usr/bin/env python3
"""ClusterFuzzLite/atheris coverage-guided harness for
`codecalc.sessions._jail` / `_jail_nofollow` / `_session_dir` — the traversal
guards between a caller-supplied relative path (or session id) and a
filesystem write under a session workspace (see codecalc/sessions.py's own
docstrings, in particular `_jail`'s record of the confirmed `str.startswith`
bypass fixed).

COMPLEMENTS scripts/fuzz.py, does not replace it — see fuzz/README.md and
fuzz/safe_expr_fuzzer.py's module docstring for the deterministic-gate vs.
coverage-guided-discovery split; the same reasoning applies here.

SAME SURFACE, SAME SEED CORPUS, SAME CONTRACT as scripts/fuzz.py's
`fuzz_jail`/`fuzz_session_dir` — imported from there, not copied. Per that
contract, a malformed path/session-id is expected to raise `ValueError` (the
documented refusal both functions raise deliberately) — that is caught
below and treated as the safe outcome, exactly like scripts/fuzz.py's own
loops. Anything else escaping — any OTHER exception, or a successful return
whose resolved path lands outside the workspace/root it was jailed to — is
the finding, and is what actually matters here: an escaped path is the
security bug this guard exists to prevent, not merely an unexpected
exception, so success paths are checked explicitly rather than only
watching for a crash.

`_jail_nofollow` (THE-1103) gets its own check, `_check_jail_nofollow`,
rather than being folded into `_check_jail`: it returns workspace-relative
COMPONENTS, never a resolved `Path` (see its own docstring for why it must
never call `resolve()`), so there is no filesystem path to re-resolve and
compare against a base — the contract this checks instead is "every
returned component is a real, single, `..`-free, NUL-free, filesystem-
encodable name" — the exact set of properties `delete_file`'s `_unlink_pinned`
walk trusts each component to already have. NUL and lone-surrogate seeds
live in `fuzz_corpus.SEED_CORPUS_PATH` (shared with `_check_jail`, since a
NUL byte or an unencodable component is refused the same way by both `_jail`
and `_jail_nofollow`) rather than a separate corpus here.
"""

import os
import sys
import tempfile
from pathlib import Path

# scripts/fuzz.py is put on PYTHONPATH by .clusterfuzzlite/Dockerfile — see
# fuzz/safe_expr_fuzzer.py's module docstring for why this is both the
# build-time AND run-time resolution path, and why this insert is only a
# fallback for running the file directly.
_REPO = Path(__file__).resolve().parents[1]
for _p in (str(_REPO), str(_REPO / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import atheris  # noqa: E402 — needs the path insert above

with atheris.instrument_imports():
    import fuzz as fuzz_corpus  # scripts/fuzz.py: SEED_CORPUS_PATH/_SESSION_ID + contract
    from codecalc import sessions

# Workspace + root set up ONCE for the life of this fuzzer process, not per
# input — scripts/fuzz.py's own `fuzz_jail`/`fuzz_session_dir` do the same
# (one temp dir per whole run, not per iteration) because `_jail`'s resolve()
# is the thing under test, not filesystem setup cost.
_TMP = tempfile.TemporaryDirectory(prefix="codecalc-cflite-fuzz-")
_BASE = Path(_TMP.name) / "session-workspace"
_BASE.mkdir(parents=True, exist_ok=True)
fuzz_corpus._plant_symlink_trap(_BASE)  # reused from scripts/fuzz.py, not re-implemented
_BASE_RESOLVED = _BASE.resolve()

_ROOT = Path(_TMP.name) / "session-root"
_ROOT.mkdir(parents=True, exist_ok=True)
_ROOT_RESOLVED = _ROOT.resolve()
sessions.SESSION_ROOT = _ROOT  # _session_dir reads this module global


def _check_jail(path: str) -> None:
    try:
        result = sessions._jail(_BASE, path)
    except ValueError:
        return  # the documented, safe refusal
    if not result.resolve().is_relative_to(_BASE_RESOLVED):
        raise AssertionError(f"sessions._jail escaped workspace: {result!r} for path={path!r}")


def _check_session_dir(session_id: str) -> None:
    try:
        result = sessions._session_dir(session_id)
    except ValueError:
        return  # the documented, safe refusal
    if not result.resolve().is_relative_to(_ROOT_RESOLVED):
        raise AssertionError(f"sessions._session_dir escaped root: {result!r} for id={session_id!r}")


def _check_jail_nofollow(path: str) -> None:
    try:
        parts = sessions._jail_nofollow(path)
    except ValueError:
        return  # the documented, safe refusal
    if not parts or any(p in ("", ".", "..") for p in parts):
        raise AssertionError(f"sessions._jail_nofollow returned invalid parts: {parts!r} for path={path!r}")
    for part in parts:
        # THE-1103: a NUL byte or an unencodable (lone-surrogate) component
        # must never survive to here — `_unlink_pinned` trusts every
        # component this returns to reach a real `openat`/`lstat`/`unlink`
        # call unrefused, and either one raises there instead
        # (`ValueError`/`UnicodeEncodeError`) rather than the documented
        # `{"ok": False, ...}` refusal.
        if "\x00" in part:
            raise AssertionError(f"sessions._jail_nofollow returned a NUL-containing part: {parts!r} for path={path!r}")
        try:
            os.fsencode(part)
        except UnicodeEncodeError as exc:
            raise AssertionError(
                f"sessions._jail_nofollow returned an unencodable part: {parts!r} for path={path!r}") from exc


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    # A 3-way pick chooses which of the three guards this input exercises;
    # libFuzzer's coverage feedback explores every side of that branch on
    # its own, same as it does for every other branch in the target code.
    choice = fdp.ConsumeIntInRange(0, 2)
    if choice == 0:
        corpus = fuzz_corpus.SEED_CORPUS_PATH
        idx = fdp.ConsumeIntInRange(0, len(corpus))
        seed = corpus[idx] if idx < len(corpus) else ""
        tail = fdp.ConsumeUnicodeNoSurrogates(fdp.remaining_bytes())
        _check_jail(seed + tail)
    elif choice == 1:
        corpus = fuzz_corpus.SEED_CORPUS_SESSION_ID
        idx = fdp.ConsumeIntInRange(0, len(corpus))
        seed = corpus[idx] if idx < len(corpus) else ""
        tail = fdp.ConsumeUnicodeNoSurrogates(fdp.remaining_bytes())
        _check_session_dir(seed + tail)
    else:
        # same corpus as `_check_jail` (choice 0) — see module docstring
        # for why NUL/surrogate seeds are shared rather than duplicated.
        corpus = fuzz_corpus.SEED_CORPUS_PATH
        idx = fdp.ConsumeIntInRange(0, len(corpus))
        seed = corpus[idx] if idx < len(corpus) else ""
        tail = fdp.ConsumeUnicodeNoSurrogates(fdp.remaining_bytes())
        _check_jail_nofollow(seed + tail)


if __name__ == "__main__":
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()
