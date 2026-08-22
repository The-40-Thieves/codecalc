#!/usr/bin/env python3
"""Mutation fuzzer over codecalc's two highest-risk caller-string surfaces.

Two targets, both places a caller string reaches something that
walks it structurally rather than just comparing bytes:

  1. `safe_expr.classify_unsafe` / `safe_expr.safe_parse` — the screen that
     stands between an `evaluate_expression`-shaped caller string and SymPy's
     `parse_expr`, which (per that module's own docstring) populates its
     default namespace from `vars(builtins)` and will run `__import__(...)`
     if asked to. The seed corpus below starts from the exact payloads
     `tests/test_security.py` already treats as confirmed exploits (CVE-2026-
     codecalc-001 and its relatives) and from `evaluate_expression`'s own
     "screened before the parser" regression block.

  2. `sessions._jail` / `sessions._session_dir` — the traversal guard between
     a caller-supplied relative path and a filesystem write under a session
     workspace. `_jail`'s own docstring records a confirmed bypass (a sibling
     directory whose name merely EXTENDED the session id satisfied a naive
     `str.startswith` check) that a component-wise `is_relative_to` fixed;
     this fuzzer is the standing check that nothing reopens that shape.

THE CONTRACT BOTH TARGETS MUST HOLD, for every generated input:
  - never raise an exception the caller does not itself define as the
    result (`classify_unsafe`/`safe_parse` return `None` or a tuple;
    `_jail`/`_session_dir` return a `Path` or raise `ValueError` — anything
    else escaping is the finding);
  - never actually CALL a builtin like `input()`/`breakpoint()` — see
    `_no_stdin()` below for how this is turned into a fast, safe assertion
    instead of a hang;
  - never run unboundedly — see `_call_timeboxed()`.

WHY A DAEMON THREAD, NOT `signal.alarm`. `signal.alarm` only exists on POSIX,
and this fuzzer is meant to run the same way on every CI runner this repo
already tests on (README: "Two things degrade rather than fail on a given
platform"). A daemon thread can time out a call on every platform, at the
cost of a real caveat: it cannot forcibly stop a CPU-bound infinite loop in
CPython (nothing outside `signal.alarm`/a subprocess can). A hang from that
class of bug is still DETECTED here — it just leaks one parked daemon thread
per hang instead of blocking the fuzzer — and daemon=True is what stops that
leak from blocking process exit, same reasoning as `sessions._readline_timeout`.

DETERMINISM. `random.Random(seed)` with a fixed default seed ('s own
requirement: no `Math.random`/unseeded time, so a crash reproduces). Pass
`--seed` to explore a different stream; the crash report always echoes the
seed that produced it.

    python scripts/fuzz.py                          # 3000 inputs/target, seed 0
    python scripts/fuzz.py --iterations 20000        # a deeper run
    python scripts/fuzz.py --target safe_expr        # one surface only
    CODECALC_FUZZ_ITERATIONS=500 python scripts/fuzz.py   # CI's smaller budget

Exit 0 with zero crashes; exit 1 and print every crash (input included, so it
reproduces) otherwise. A crash found here is a REAL finding — report it, don't
silence the input that triggered it.

TWO KNOWN FINDINGS, neither fixed by this script (; filed for
follow-up rather than papered over — see the ticket, not this comment, for
disposition). Both are DoS-shaped (cost, not an escape). #1 sits entirely
outside the space today's callers can reach (no guard bounds it at all); #2
has a modest, bounded cost inside the space a documented tool can reach today
and gets sharply worse just past it — which is exactly why a fuzzer that
explores past the guarded boundary is the thing that found both:

  1. `sessions._jail`'s traversal guard resolves the caller's path with
     `Path.resolve()` before checking it stays inside the workspace (by
     design — see `_jail`'s own docstring: resolving BEFORE the boundary
     check is what stops a symlink escape). That resolve costs WORSE than
     linear in the number of `/`-separated segments: measured, 10_000
     segments took ~4.5s and 20_000 took ~19.7s (a ~4.4x cost for a 2x
     segment count). Neither `_jail` nor its callers (`write_file`,
     `list_files`, `resource_read`) cap segment count or raw length before
     calling it, so a caller-supplied `path` built from many repeated short
     segments (no `..`, no absolute prefix — ordinary relative-path syntax)
     can occupy the server for a duration that grows faster than the bytes
     sent to trigger it.

  2. `safe_expr.reject_explosive` bounds `Pow` shapes only (power towers,
     oversized exponents/digit counts). Deeply-nested-parens combined with a
     long chain of repeated terms is NOT bounded by it, and IS reachable
     within `logic._MAX_EXPR_LEN`'s own 2000-char ceiling: a 1_994-char input
     (deep parens around a long `2x2x2x...` implicit-multiplication chain,
     found by this fuzzer at `--seed 777`) took 5.3s — bounded (it ends in a
     caught "maximum recursion depth exceeded" parse error, not a true hang),
     but multiple seconds for one call is already a real per-request cost a
     documented tool can trigger today. Past 2000 chars the same shape gets
     worse fast and looks unbounded: depth 130 with 410 repeated `1.5 + 2.3`
     terms (3_950 chars) cost 3.5s with no sign of a ceiling as either factor
     grows toward `_nest`'s own 200-deep cap. `safe_expr.safe_parse` is
     documented as "the ONLY place in this package that calls SymPy's
     parse_expr", used by four different callers, and carries no length or
     shape guard of its own beyond `reject_explosive`'s `Pow`-only checks —
     `logic.py`/`exact.py`/`linalg.py`'s 2000-char caps blunt this but do not
     close it, and a future fifth caller that reuses `safe_parse` without
     re-deriving that same external cap inherits the worse, unbounded-past-
     2000-chars version un-warned.

This fuzzer's default `--timeout` (10s) is set with headroom over the 5.3s
worst case measured AT the 2000-char default `_MAX_MUTATED_LEN`, so a default
run distinguishes "slow but bounded, like finding #2 within the cap" from a
genuine hang. Raise `_MAX_MUTATED_LEN` past 2000 (and `--timeout` to match) to
fuzz past that boundary into finding #2's unbounded-looking region directly.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import queue
import random
import sys
import tempfile
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from codecalc import safe_expr, sessions  # noqa: E402 — needs the path insert above

# ── seed corpus: safe_expr ──────────────────────────────────────────────────
# The first four are copied verbatim from tests/test_security.py's "screened
# before the parser" block (CVE-2026-codecalc-001 and its relatives) — the
# fuzzer's mutations are meant to explore AROUND confirmed exploits, not
# invent unrelated ones from scratch. `input()`/`breakpoint()`/`quit()`/
# `exit()` are the builtins explicitly names as must-never-execute;
# they carry none of classify_unsafe's denied syntax (no dot, no bracket, no
# leading underscore), so they are exactly the shape that only
# `safe_global_dict()`'s empty `__builtins__` — not the token screen — stops.
SEED_CORPUS_EXPR = [
    "__import__('os').system('id')",
    "().__class__.__base__.__subclasses__()",
    "x.__class__",
    "lambda: 1",
    "input()",
    "breakpoint()",
    "quit()",
    "exit()",
    "2+2",
    "sin(x) + cos(y)",
    "x**2 + 3*x - 5",
    "2x",
    "1.5 + 2.3",
    "x^2",
    "factorial(99999)",
    "9**9**9**9",
    "2**100000",
    "(x+1)**20000",
    "binomial(200000, 100000)",
    "a and b or not c",
    "Matrix([[1,2],[3,4]])",
    "",
    "   ",
    "()",
    "((((((",
    # Unicode shapes that crash CPython's C tokenizer from inside
    # classify_unsafe (it round-trips source through UTF-8): the lone surrogate
    # (UnicodeEncodeError, originally found by the mutator) and the
    # replacement/truncated-multibyte char (UnicodeDecodeError, found by the
    # ClusterFuzzLite harness). Seeded here so the deterministic gate covers
    # both directly, not only when the mutator happens to reconstruct them.
    "\ud800",
    "�\r�",
    # `!!` (sympy's standard_transformations double-factorial) turns the
    # exponent into a huge exact Integer — `factorial2(100000)` /
    # `factorial2(20000)` — reaching an int->float conversion and an
    # f-string interpolation in `reject_explosive` that neither bounded
    # for a value that size. Found by the 2026-08-22 ClusterFuzzLite batch;
    # `reject_explosive` raised (OverflowError / ValueError) instead of
    # returning a refusal, the exact contract this fuzzer holds it to.
    "2**100000!!ubrNembeubrNember",
    "(x+1)**20000!!ubrNembeubrNember",
    # a >308-digit base: SymPy's `Integer.__float__` returns `inf` here
    # rather than raising like Python's own `int.__float__` does, so the
    # digit estimate's `try/except OverflowError` never fired. Same batch.
    "9" * 400 + "**12",
]

# ── seed corpus: session paths ──────────────────────────────────────────────
SEED_CORPUS_PATH = [
    "output.txt",
    "sub/dir/file.txt",
    "a/b/c.py",
    "../etc/passwd",
    "../../etc/passwd",
    "..\\..\\windows\\system32\\config\\sam",
    "/etc/passwd",
    "/etc/shadow",
    "~/.ssh/id_rsa",
    "....//....//etc/passwd",
    "..%2f..%2fetc%2fpasswd",
    "a\x00.txt",
    "a\x00../../etc/passwd",
    "C:\\Windows\\System32\\drivers\\etc\\hosts",
    "\\\\server\\share\\file",
    "a" * 5000,
    "café/résumé.txt",
    "文件.txt",
    "𝔘𝔫𝔦𝔠𝔬𝔡𝔢.txt",
    ".",
    "..",
    "...",
    "./",
    "../",
    "/",
    "//",
    "\\",
    "sub/../../../etc/passwd",
    "evil_link/../../outside-target/secret.txt",
    "evil_link/secret.txt",
]

# ── seed corpus: session ids ─────────────────────────────────────────────────
SEED_CORPUS_SESSION_ID = [
    "python3-deadbeef",
    "node-abc12345",
    "../etc/passwd",
    "..",
    ".",
    "/etc/passwd",
    "a" * 100,
    "a" * 1000,
    # the confirmed bypass _jail's own docstring records: a sibling directory
    # whose name merely EXTENDS a real session id.
    "python3-deadbeefEVIL",
    "a\x00b",
    "café",
    "文件",
    "\U0001f3af",
    "",
    " ",
    "a/b",
    "a\\b",
    "a:b",
]

#: Bounds every mutated string, applied after EACH chained mutation step (not
#: just the final one) so `_repeat`/`_huge` chained twice can't multiply past
#: it. Set to exactly `logic._MAX_EXPR_LEN` / `exact._MAX_EXPR_LEN` — the
#: length every real caller already enforces before a string reaches
#: `safe_expr` at all — so the DEFAULT corpus stress-tests precisely the
#: input space production traffic can reach, and no larger. Measured across a
#: (nesting depth x repeated-term count) grid at this bound: worst observed
#: was ~0.66s, comfortably inside the default `--timeout`.
#:
#: Deliberately NOT raised to explore further: measured just past this
#: bound, cost stops looking bounded-by-length and starts tracking
#: `depth * repeated_term_count` instead — depth 130 x 410 terms (3_950
#: chars, ~2x this cap) already cost 3.5s, and the growth is consistent with
#: unbounded for depth approaching `_nest`'s own 200 cap combined with a
#: large repeat count. That is a real gap in `reject_explosive` (it bounds
#: `Pow` shapes only, not `Add`/`Mul` term-count growth under nesting) — see
#: the module docstring's KNOWN FINDING. It is not exercised by the default
#: corpus because it sits outside the space any current caller can reach;
#: raise this constant (and `--timeout` to match) to fuzz that region
#: directly.
_MAX_MUTATED_LEN = 2_000


# ── mutation operators ───────────────────────────────────────────────────────
# Each takes (rng, s) -> str and is allowed to raise (a mutator failing on a
# pathological intermediate string is not itself a finding; _mutate() below
# swallows it and keeps the pre-mutation string).

def _bit_flip(rng: random.Random, s: str) -> str:
    b = bytearray(s.encode("utf-8", errors="replace"))
    if not b:
        return s
    i = rng.randrange(len(b))
    b[i] ^= 1 << rng.randrange(8)
    return b.decode("utf-8", errors="replace")


_INSERT_TOKENS = [
    "(", ")", "_", ".", "[", "]", "'", '"', "__import__", "os", "system",
    "0x41", "\n", "\t", " ", "\\", "..", "%00", "\x00", "\ufeff",
]


def _insert_token(rng: random.Random, s: str) -> str:
    tok = rng.choice(_INSERT_TOKENS)
    pos = rng.randrange(len(s) + 1)
    return s[:pos] + tok + s[pos:]


def _delete_char(rng: random.Random, s: str) -> str:
    if not s:
        return s
    pos = rng.randrange(len(s))
    return s[:pos] + s[pos + 1:]


def _repeat(rng: random.Random, s: str) -> str:
    if not s:
        return s
    return s * rng.randrange(2, 50)


def _nest(rng: random.Random, s: str) -> str:
    depth = rng.randrange(1, 200)
    return "(" * depth + s + ")" * depth


_UNICODE_CODEPOINTS = [
    0x200B,   # zero-width space
    0x202E,   # right-to-left override
    0x1F600,  # emoji, outside the BMP
    0x0301,   # combining acute accent
    0xFEFF,   # BOM
    0x00B5,   # micro sign — looks like an identifier char in some fonts
    0x2028,   # line separator
    0xD800,   # an unpaired surrogate, if the platform's str allows it
]


def _unicode_inject(rng: random.Random, s: str) -> str:
    cp = rng.choice(_UNICODE_CODEPOINTS)
    try:
        ch = chr(cp)
    except ValueError:
        return s
    pos = rng.randrange(len(s) + 1)
    return s[:pos] + ch + s[pos:]


def _huge(rng: random.Random, s: str) -> str:
    if not s:
        s = "x"
    return s * rng.randrange(200, 4000)


def _truncate(rng: random.Random, s: str) -> str:
    if not s:
        return s
    return s[:rng.randrange(len(s))]


_MUTATORS = [_bit_flip, _insert_token, _delete_char, _repeat, _nest,
             _unicode_inject, _huge, _truncate]


def _mutate(rng: random.Random, seed: str) -> str:
    s = seed
    for _ in range(rng.randrange(1, 4)):  # 1-3 chained mutations
        op = rng.choice(_MUTATORS)
        try:
            s = op(rng, s)
        except Exception:
            pass  # the mutator itself misbehaving is not a finding
        if len(s) > _MAX_MUTATED_LEN:
            s = s[:_MAX_MUTATED_LEN]
    return s


# ── timeboxing + stdin neutralisation ────────────────────────────────────────

@contextlib.contextmanager
def _no_stdin():
    """Replace stdin with an empty, non-blocking stream for one call.

    If a regression ever lets a screened string reach a real `input()`, that
    call would otherwise block reading fd 0 — the SAME descriptor a stdio MCP
    server uses for its transport, per safe_expr.py's own docstring. An empty
    `io.StringIO` turns that into an immediate `EOFError` (a fast, catchable
    crash finding) instead of a hang the timeout has to absorb.
    """
    old = sys.stdin
    sys.stdin = io.StringIO("")
    try:
        yield
    finally:
        sys.stdin = old


def _call_timeboxed(fn, args, timeout: float):
    """Run `fn(*args)` on a daemon thread with a wall-clock budget.

    Returns `(result, exc, timed_out)`. `exc` is the raw exception object —
    `BaseException`, not `Exception`, so a stray `SystemExit` (e.g. from a
    builtin `quit()`/`exit()` actually being called) is caught here rather
    than killing the fuzzer itself.

    Same pattern as `sessions._readline_timeout`: a bounded queue and a
    daemon thread, because there is no portable way to forcibly cancel a
    running Python thread. A genuine CPU-bound hang leaks one parked thread
    per occurrence rather than blocking this process's exit.
    """
    q: queue.Queue = queue.Queue(maxsize=1)

    def _run():
        try:
            q.put(("ok", fn(*args)))
        except BaseException as exc:  # see docstring above for why this is BaseException
            q.put(("exc", exc))

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    try:
        kind, payload = q.get(timeout=timeout)
    except queue.Empty:
        return None, None, True
    if kind == "exc":
        return None, payload, False
    return payload, None, False


# ── target 1: safe_expr ─────────────────────────────────────────────────────

def fuzz_safe_expr(rng: random.Random, iterations: int, timeout: float):
    crashes = []
    for _ in range(iterations):
        mutated = _mutate(rng, rng.choice(SEED_CORPUS_EXPR))

        with _no_stdin():
            result, exc, hung = _call_timeboxed(safe_expr.classify_unsafe, (mutated,), timeout)
        if hung:
            crashes.append(("classify_unsafe", mutated, "TIMEOUT (possible hang / blocking call)"))
            continue
        if exc is not None:
            crashes.append(("classify_unsafe", mutated, f"{type(exc).__name__}: {exc}"))
            continue
        if result is not None and not (isinstance(result, tuple) and len(result) == 2):
            crashes.append(("classify_unsafe", mutated, f"unexpected return shape: {result!r}"))
            continue

        with _no_stdin():
            result, exc, hung = _call_timeboxed(safe_expr.safe_parse, (mutated,), timeout)
        if hung:
            crashes.append(("safe_parse", mutated, "TIMEOUT (possible hang / blocking call)"))
            continue
        if exc is not None:
            crashes.append(("safe_parse", mutated, f"{type(exc).__name__}: {exc}"))
            continue
        if not (isinstance(result, tuple) and len(result) == 2):
            crashes.append(("safe_parse", mutated, f"unexpected return shape: {result!r}"))
            continue
        value, refusal = result
        if value is None and refusal is None:
            crashes.append(("safe_parse", mutated, "returned (None, None): neither a value nor a refusal"))
    return crashes, iterations


# ── target 2: sessions path handling ────────────────────────────────────────

def _plant_symlink_trap(base: Path) -> None:
    """A symlink inside `base` pointing OUTSIDE it — the exact escape
    `_jail`'s resolve()-before-comparison exists to defeat. Best-effort:
    symlink creation needs a privilege absent on some Windows configurations
    (this repo's own `_win_cased`/Landlock tests treat that as routine), and
    a fuzzer whose setup step can fail should degrade, not crash.
    """
    try:
        outside = base.parent / "outside-target"
        outside.mkdir(exist_ok=True)
        (outside / "secret.txt").write_text("should never be reachable\n")
        link = base / "evil_link"
        if not link.exists():
            link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pass


def fuzz_jail(rng: random.Random, iterations: int, timeout: float, base: Path):
    crashes = []
    base_resolved = base.resolve()
    for _ in range(iterations):
        mutated = _mutate(rng, rng.choice(SEED_CORPUS_PATH))

        with _no_stdin():
            result, exc, hung = _call_timeboxed(sessions._jail, (base, mutated), timeout)
        if hung:
            crashes.append(("_jail", mutated, "TIMEOUT (possible hang / blocking call)"))
            continue
        if exc is not None:
            if isinstance(exc, ValueError):
                continue  # a refusal — the documented, safe outcome
            crashes.append(("_jail", mutated, f"{type(exc).__name__}: {exc}"))
            continue
        try:
            escaped = not result.resolve().is_relative_to(base_resolved)
        except Exception as post_exc:  # the post-check itself must not explode
            crashes.append(("_jail", mutated, f"post-check raised: {post_exc!r}"))
            continue
        if escaped:
            crashes.append(("_jail", mutated, f"ESCAPED WORKSPACE: resolved to {result}"))
    return crashes, iterations


def fuzz_session_dir(rng: random.Random, iterations: int, timeout: float, root: Path):
    orig_root = sessions.SESSION_ROOT
    sessions.SESSION_ROOT = root
    crashes = []
    root_resolved = root.resolve()
    try:
        for _ in range(iterations):
            mutated = _mutate(rng, rng.choice(SEED_CORPUS_SESSION_ID))

            with _no_stdin():
                result, exc, hung = _call_timeboxed(sessions._session_dir, (mutated,), timeout)
            if hung:
                crashes.append(("_session_dir", mutated, "TIMEOUT (possible hang / blocking call)"))
                continue
            if exc is not None:
                if isinstance(exc, ValueError):
                    continue  # a refusal — the documented, safe outcome
                crashes.append(("_session_dir", mutated, f"{type(exc).__name__}: {exc}"))
                continue
            try:
                escaped = not result.resolve().is_relative_to(root_resolved)
            except Exception as post_exc:
                crashes.append(("_session_dir", mutated, f"post-check raised: {post_exc!r}"))
                continue
            if escaped:
                crashes.append(("_session_dir", mutated, f"ESCAPED ROOT: {result}"))
    finally:
        sessions.SESSION_ROOT = orig_root
    return crashes, iterations


# ── driver ───────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--iterations", type=int,
        default=int(os.environ.get("CODECALC_FUZZ_ITERATIONS", "3000")),
        help="generated inputs PER TARGET (default: $CODECALC_FUZZ_ITERATIONS or 3000)")
    parser.add_argument(
        "--seed", type=int, default=0,
        help="random.Random seed — deterministic by default so a crash reproduces (default: 0)")
    parser.add_argument(
        "--timeout", type=float, default=10.0,
        help="per-call wall-clock budget in seconds (default: 10.0)")
    parser.add_argument(
        "--target", choices=("safe_expr", "sessions", "all"), default="all",
        help="which surface to fuzz (default: all)")
    args = parser.parse_args(argv)

    rng = random.Random(args.seed)  # noqa: S311 — corpus mutation, not cryptography; determinism is the point
    crashes: list[tuple[str, str, str]] = []
    total = 0

    if args.target in ("safe_expr", "all"):
        found, n = fuzz_safe_expr(rng, args.iterations, args.timeout)
        crashes += found
        total += n
        print(f"safe_expr (classify_unsafe + safe_parse): {n} inputs, {len(found)} crashes")

    if args.target in ("sessions", "all"):
        with tempfile.TemporaryDirectory(prefix="codecalc-fuzz-") as tmp:
            base = Path(tmp) / "session-workspace"
            base.mkdir(parents=True, exist_ok=True)
            _plant_symlink_trap(base)
            found, n = fuzz_jail(rng, args.iterations, args.timeout, base)
            crashes += found
            total += n
            print(f"sessions._jail: {n} inputs, {len(found)} crashes")

            root = Path(tmp) / "session-root"
            root.mkdir(parents=True, exist_ok=True)
            found, n = fuzz_session_dir(rng, args.iterations, args.timeout, root)
            crashes += found
            total += n
            print(f"sessions._session_dir: {n} inputs, {len(found)} crashes")

    print(f"\n=== {total} total inputs, {len(crashes)} crashes (seed={args.seed}) ===")
    if crashes:
        print(f"\nCRASHES (reproduce with --seed {args.seed} --target ...):")
        for target, inp, detail in crashes[:50]:
            print(f"  [{target}] {detail}\n    input={inp!r}")
        if len(crashes) > 50:
            print(f"  ... and {len(crashes) - 50} more")
    return 1 if crashes else 0


if __name__ == "__main__":
    sys.exit(main())
