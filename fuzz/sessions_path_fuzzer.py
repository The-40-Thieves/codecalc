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
compare against a base. The contract it checks is not "every returned part
looks safe by the same rules the implementation applies" (round 1's own
check did exactly that, re-stating `_jail_nofollow`'s NUL/encodability
predicates — which can only catch one of them being dropped, never one
being wrong, and round 1's surrogate predicate WAS wrong on a platform
whose default filesystem-encoding error mode differs from POSIX's — see
`_jail_nofollow`'s own docstring). Round 2's oracle is the real syscall
`_unlink_pinned` makes: each returned component goes through
`os.lstat(component, dir_fd=...)`, the exact call THE-1103 exists to keep
NUL bytes and lone surrogates away from. NUL and lone-surrogate seeds live
in `fuzz_corpus.SEED_CORPUS_PATH` (shared with `_check_jail`, since both
guards must refuse the same malformed component), and `TestOneInput`'s
tail for this branch uses `ConsumeUnicode` (not `ConsumeUnicodeNoSurrogates`)
so the mutator itself can produce a lone surrogate, not just the seed.
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

#: A real, open directory fd under `_BASE`, used ONLY so `_check_jail_nofollow`
#: can hand each returned component to the ACTUAL syscall wrapper
#: `_unlink_pinned` uses (`os.lstat(..., dir_fd=...)`) — see that function's
#: docstring for why a real syscall, not a re-statement of the implementation's
#: own predicates, is the oracle THE-1103 round 2 needs. `None` on a platform
#: without `dir_fd` support (Windows): `_unlink_pinned` itself has no
#: equivalent walk there either (`delete_file`'s own `not _DIR_FD_SUPPORTED`
#: fallback), so there is nothing for this oracle to exercise.
_LSTAT_DIR_FD = (
    os.open(_BASE, os.O_RDONLY | sessions._O_DIRECTORY | sessions._O_NOFOLLOW | sessions._O_CLOEXEC)
    if sessions._DIR_FD_SUPPORTED else None
)


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
    """THE-1103 round 2 (grok verify-security): round 1's check re-applied
    the SAME predicates `_jail_nofollow`'s implementation uses (NUL
    substring, `os.fsencode` raising) — which can only ever catch one of
    those checks being DROPPED from the implementation, never one being
    WRONG (round 1's own surrogate check was wrong: right predicate, wrong
    platform-dependent oracle — see `_jail_nofollow`'s docstring). The real
    contract is "every returned component survives the ACTUAL syscall
    `_unlink_pinned` makes with it", so this hands each component to that
    same call (`os.lstat(component, dir_fd=...)`) and lets whatever happens
    happen: `OSError` (no such entry, `ELOOP`, `ENOTDIR`, ...) is the
    ordinary "not found" outcome `_unlink_pinned` itself treats as safe;
    anything else — `ValueError` for an embedded NUL, `UnicodeEncodeError`
    for a name the platform cannot encode at all — is exactly the class of
    bug THE-1103 fixed and is left to propagate uncaught as the finding.
    """
    try:
        parts = sessions._jail_nofollow(path)
    except ValueError:
        return  # the documented, safe refusal
    if not parts or any(p in ("", ".", "..") for p in parts):
        raise AssertionError(f"sessions._jail_nofollow returned invalid parts: {parts!r} for path={path!r}")
    if _LSTAT_DIR_FD is None:
        return  # no dir_fd support on this platform — see _LSTAT_DIR_FD's own comment
    for part in parts:
        try:
            os.lstat(part, dir_fd=_LSTAT_DIR_FD)
        except OSError:
            pass  # no such entry (or ELOOP/ENOTDIR) — the ordinary, safe outcome


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
        # `ConsumeUnicode`, NOT `ConsumeUnicodeNoSurrogates`: this is the
        # ONE branch whose whole point is exercising lone-surrogate
        # components (THE-1103 round 2), so the tail must be able to
        # PRODUCE one, not just rely on the seed corpus carrying one.
        corpus = fuzz_corpus.SEED_CORPUS_PATH
        idx = fdp.ConsumeIntInRange(0, len(corpus))
        seed = corpus[idx] if idx < len(corpus) else ""
        tail = fdp.ConsumeUnicode(fdp.remaining_bytes())
        _check_jail_nofollow(seed + tail)


if __name__ == "__main__":
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()
