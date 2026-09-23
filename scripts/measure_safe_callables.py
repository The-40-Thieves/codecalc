#!/usr/bin/env python3
"""THE-1095 round 14 (coordinator review of 7630e87): MEASURES, rather
than hand-picks, which of `safe_global_dict()`'s public callables are
cheap enough — regardless of the argument shape a caller might supply —
to exempt from `_deferred_global_dict()`'s own generic stand-in (see
`codecalc/safe_expr.py`'s `_MEASURED_SAFE_CALLABLES`, which this script
regenerates the data behind).

The coordinator's own review of the round-13 commit found the hand-
picked allowlist under-covered ordinary calculator input (`gcd(12, 18)`,
`lcm(4, 6)`, `limit(sin(x)/x, x, 0)`, `nsimplify(0.5)` all refused where
`origin/main` evaluates them) — this script is the fix: a name earns a
place on the allowlist by MEASUREMENT, not by which names happened to
occur to a human reviewer.

## Method

Every public callable `codecalc.safe_expr.safe_global_dict()` exports is
probed with a FIXED grid of argument shapes (see `PROBE_SHAPES`,
below): one/two/three small integers, a small `Rational`, a small
`Float`, a bare `Symbol`, `Symbol, integer`, `Symbol, Symbol` — plus a
"_heavy" variant of every integer-carrying shape, at `codecalc.
safe_expr.MAX_HEAVY_ARG` (1463) rather than a small value, since
`_measured_safe_argument_cap_violation`'s own generic cap applies PER
ARGUMENT, independently: a small-value-only probe would answer "is
this fast at a TRIVIAL input", never "is this still fast at the
LARGEST input the enforced cap will actually let through" — the
question that determines whether the cap keeps a caller safe at all
(see `PROBE_SHAPES`'s own comment for the live hang this caught: a
small-shapes-only grid allowlisted `ones`/`zeros`/`randMatrix`, and
`ones(1463, 1463)` — legal under the per-argument cap — hung for
15s+). Each shape is run in its own ISOLATED subprocess (a fresh
Python process per (name, shape) pair), with an address-space `ulimit
-v` cap and a `PROBE_CALL_TIMEOUT_S` in-child cap on the call itself (interpreter start-up excluded) and a `PROBE_TIMEOUT_S` process wall clock, so a single hanging or
memory-hungry shape can neither stall nor crash the measurement of any
OTHER name or shape.

A shape a name's own signature genuinely does not accept (a `TypeError`
from argument binding, e.g. `gcd()` called with three positional args
when its own signature takes two) is not a failure — it is excluded from
that name's own verdict, the same way `origin/main` itself would refuse
that call. A name is ALLOWLISTED only when EVERY shape it DOES accept
completes in comfortably under 100ms (this script's own `FAST_MS`
threshold, deliberately well under the coordinator's own 100ms bar, to
leave margin against machine-to-machine variance) with a result whose
own digit count (or `count_ops`, for a non-numeric `Basic`) stays under
this module's own `MAX_NUMERIC_DIGITS` ceiling. Anything else — a
timeout, an over-limit result, or a crash — leaves the name OFF the
allowlist; `_deferred_global_dict()`'s own default-deny posture is what
protects it instead (a generic stand-in, refused on any numeric
argument, per `_unbounded_generic_call_violation`'s own docstring).

## Regenerating

    python scripts/measure_safe_callables.py --write

writes `codecalc/_measured_safe_callables.json` — the TRIMMED,
committed, version-controlled measurement record
`_MEASURED_SAFE_CALLABLES` actually loads from at runtime (names, the
measurement date, the two thresholds every name was measured against —
no per-shape timing data, so it ships small in a built package) — with
a fresh measurement date, and `scripts/_measured_safe_callables_raw.json`
— the FULL per-candidate, per-shape probe log, committed too (so a
reviewer can see why any name was or wasn't allowlisted without
re-running this script) but kept OUTSIDE `codecalc/` so it never ships
in a package. Without `--write`, prints the trimmed JSON to stdout
instead (a dry run; the raw log is not written at all in this mode).

    python scripts/measure_safe_callables.py --sample 30

re-probes a random SAMPLE of 30 already-allowlisted names (or every
name in the sample, if fewer are allowlisted) on the RUNNING box and
exits nonzero if any exceeds `FAST_MS` — the same check `tests/
test_measured_safe_callables.py` runs as part of the regular suite, so
a machine that has drifted slower than the box this list was measured
on is caught rather than silently trusted.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import random
import subprocess
import sys
import textwrap
from datetime import UTC, datetime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

DATA_PATH = REPO_ROOT / "codecalc" / "_measured_safe_callables.json"
#: The FULL per-candidate, per-shape probe log (`measure_all`'s own
#: `results`) — committed under `scripts/`, alongside the script that
#: produces it, so a reviewer can see WHY a name was or wasn't
#: allowlisted without re-running the measurement, but deliberately
#: OUTSIDE `codecalc/` so it never ships in a built package (`DATA_
#: PATH`, above, is the only measurement artifact the runtime loads or
#: a package needs).
RAW_REPORT_PATH = REPO_ROOT / "scripts" / "_measured_safe_callables_raw.json"

#: Deliberately well under the coordinator's own 100ms bar (leaves
#: margin for a slower CI runner than the box this was measured on).
FAST_MS = 60.0
#: Per-shape wall-clock cap for the ISOLATED child process — matches
#: MAX_NUMERIC_DIGITS (codecalc/safe_expr.py) so a probe's own verdict
#: uses the identical ceiling `_generic_result_size_violation` does.
PROBE_TIMEOUT_S = 8.0  # process wall clock: interpreter + sympy import + the call
PROBE_CALL_TIMEOUT_S = 1.0  # the CALL alone, enforced in-child by SIGALRM
MAX_RESULT_DIGITS = 4000
#: `ulimit -v`, in kibibytes — generous enough for an ordinary SymPy
#: call, tight enough to kill a runaway allocation (`ones(5000,5000)`-
#: shaped) well before it pressures the measuring box itself.
PROBE_MEMORY_KB = 512 * 1024

#: Names already handled elsewhere in `codecalc/safe_expr.py` — a
#: table entry (`_FUNCTION_ARG_CAPS`), the 34-name `EvaluateFalse
#: Transformer` elementary list, or a marker/internal name this
#: script's own probe has no business touching — are skipped, not
#: reprobed: this script measures the RESIDUAL surface only, never
#: duplicates or overrides an existing, individually-reasoned-about
#: bound.
def _already_handled_names() -> frozenset[str]:
    from sympy.parsing.sympy_parser import EvaluateFalseTransformer

    from codecalc import safe_expr as se

    return (
        frozenset(se._FUNCTION_ARG_CAPS)
        | frozenset(EvaluateFalseTransformer.functions)
        | frozenset(se._DEFERRED_BINOP_OPS)
        # THE-1095 round 15 (coordinator addendum, item 3): every
        # `_POLE_SENSITIVE_NAMES` member (gamma/erf/the erf-adjacent
        # special-function family/...) has its own provable analytic
        # bound already, independent of measurement — `erf` had been
        # measured-safe-listed ANYWAY before this exclusion existed,
        # letting the measured list's own `MAX_HEAVY_ARG` argument cap
        # WRONGLY refuse `erf(10000)` (main evaluates it fine) despite
        # `_pole_sensitive_magnitude`'s own unconditional `<= 1` bound
        # never needing an argument cap at all.
        | frozenset(se._POLE_SENSITIVE_NAMES)
        # THE-1095 round 18 (coordinator item 4): `discrete_log` and its
        # `factorint`-internal siblings have a cost that depends on
        # NUMBER-THEORETIC STRUCTURE, not magnitude — no measurement
        # grid, however wide, can bound them the way this allowlist
        # bounds everything else. See `_NONDETERMINISTIC_COST_NAMES`'s
        # own comment in `codecalc/safe_expr.py` for the full account.
        | frozenset(se._NONDETERMINISTIC_COST_NAMES)
        | frozenset({"Mod", "Max", "Min", "floor", "ceiling", "frac", "N", "series",
                     "summation", "product", "Sum", "Product", "integrate", "Integral",
                     "fps"})
        # THE-1095 round 16 (coordinator addendum, item 4; grok issue 3):
        # `fps` gets the SAME dedicated-position-cap treatment `series`
        # already has (`_EXTRA_BOUNDED_POSITIONS["fps"] = {5: (...)}`,
        # `_fps_order_violation`) rather than being measured — its own
        # `order` (position 5, not 3 — the round-15 comment named the
        # wrong slot) is the cost-driving argument, the identical shape
        # `series` already needed a bespoke mechanism for.
        # AST-transform-RESERVED classes: `Add`/`Mul`/`Pow`/`Or`/`And`/
        # `Not`/`Eq`/`Ne`/`Lt`/`Le`/`Gt`/`Ge` (what `visit_BinOp`/
        # `visit_Compare` construct for EVERY `+`/`*`/`**`/comparison),
        # `Integer`/`Float` (`auto_number` — EVERY plain numeric
        # literal), `Symbol`/`Function` (`auto_symbol` — EVERY
        # undefined NAME) — none of these are "a caller chose to call
        # this NAME," they are constructed for every occurrence of the
        # corresponding TOKEN, in ANY parsed expression, and are never
        # subject to a stand-in at all (see `_deferred_global_dict`'s
        # own comment for the live regression measuring/allowlisting
        # them caused).
        | frozenset(EvaluateFalseTransformer.operators.values())
        | frozenset(EvaluateFalseTransformer.relational_operators.values())
        | frozenset({"Integer", "Float", "Symbol", "Function"})
    )


#: The probe grid, by NAME (never a source string to `eval` — see
#: `_PROBE_CHILD`'s own `_SHAPE_BUILDERS` dict, a literal, hardcoded
#: dispatch table the child process indexes into instead): one/two/
#: three small integers, a small `Rational`, a small `Float`, a bare
#: `Symbol`, `Symbol, integer`, `Symbol, Symbol` — plus, THE-1095 round
#: 14 (found live, before this round's own commit, a bug in THIS
#: script rather than in `safe_expr.py`): a "_heavy" variant of every
#: shape carrying an integer argument, at `codecalc.safe_expr.
#: MAX_HEAVY_ARG` itself (1463) rather than a friendly small value.
#:
#: `_measured_safe_argument_cap_violation` enforces its generic cap
#: PER ARGUMENT, independently — it lets `ones(1463, 1463)` through
#: (each argument is individually AT, not over, the cap) exactly as
#: readily as `ones(5, 3)`. Measuring only the small shapes answers
#: "is this callable fast at a trivial input", never the actual
#: question the cap's own SAFETY depends on: "is this callable still
#: fast at the LARGEST input the cap will ever let through." Confirmed
#: live: `sympy.ones(1463, 1463)` (a 1463x1463 = ~2.14M-element matrix
#: of `Integer(1)`) does not return within 3 SECONDS, yet the small-
#: shapes-only version of this script measured `ones`/`zeros`/
#: `randMatrix`/`eye`/`diag` allowlisted (their `two_ints` shape,
#: `(5, 3)`, finishes in under a millisecond) — `safe_parse('ones(1463,
#: 1463)')` hung past 15s as a direct result, reopening the EXACT class
#: of hang (`ones(5000,5000)`, one of Codex's own named round-13
#: repros) this whole default-deny mechanism exists to close, just at
#: a slightly higher argument value than the ORIGINAL bug. The heavy
#: shapes below now catch this AT measurement time — `ones`/`zeros`/
#: `randMatrix`/`eye`/`diag` all correctly drop off the allowlist once
#: `two_ints_heavy` is included (each times out past `PROBE_TIMEOUT_S`)
#: — rather than trusting a small-value probe to stand in for the
#: cap's own actual boundary.
#: THE-1095 round 15 (grok issue 2b, `verify-1095-r14-grok.log`): four
#: more shapes, found needed by grok's own concrete counter-examples --
#: `(2, HEAVY)`/`(HEAVY, 2)` (a WIDE, range-like span between two
#: arguments — `1463 - 2` — rather than two values both individually
#: near the cap, which is a DIFFERENT cost shape for a name whose real
#: cost scales with the DISTANCE between two arguments, e.g. a range
#: or interval); a `Float` with a huge EXPONENT (`1e300` — large in
#: MAGNITUDE while carrying only a handful of significant digits,
#: unlike `float_heavy`'s plain large mantissa); a `Rational` with a
#: 25-digit numerator (a big INTEGER inside an otherwise-plain
#: fraction, the shape `nsimplify`'s own review finding named); and a
#: four-positional-argument shape with the heavy value LAST (the
#: `fps(sin(x), x, 0, 1463)` class grok's own issue 2 table names —
#: `PROBE_SHAPES` had no shape with more than 3 positions at all before
#: this round, so a name whose cost hides behind its 4th argument was
#: never probed there regardless of magnitude).
#: THE-1095 round 16 (coordinator addendum, item 4; grok issue 3,
#: `verify-1095-r15-grok.log`): `four_arg_heavy_last` alone is
#: HEAVY-LAST-ONLY — it never puts the heavy value in position 0 or 1
#: of a 4-argument call, exactly why `jacobi_normalized(1463, 1, 2, x)`
#: (heavy FIRST) measured allowlisted and had to be patched with a
#: table row instead of a grid fix; the SAME gap is still live for
#: `Ynm`/`Ynm_c`/`Znm`/`betainc`/`betainc_regularized`/`random_poly`
#: (each has its own cost-driving argument in an EARLY position a
#: last-only heavy probe never reaches) and `fps` (whose own `order`
#: is position 5, not even reached by a 4-arg shape at all — see its
#: own `_FUNCTION_ARG_CAPS` entry, given a dedicated cap instead of
#: relying on measurement). `four_arg_heavy_first`/`four_arg_heavy_
#: middle` (heavy in position 0 / position 1, small/symbolic fillers
#: elsewhere) and `five_arg_heavy_last` (heavy in position 4, for a
#: name whose own cost hides even further back) close the position
#: coverage gap.
_HEAVY_SHAPES = frozenset({
    "one_int_heavy", "two_ints_heavy", "three_ints_heavy", "symbol_int_heavy",
    "small_heavy", "heavy_small", "float_heavy_exp", "rational_heavy_numerator",
    "four_arg_heavy_last", "four_arg_heavy_first", "four_arg_heavy_middle",
    "five_arg_heavy_last", "four_arg_heavy_all_numeric",
})

PROBE_SHAPES: tuple[str, ...] = (
    "one_int", "two_ints", "three_ints", "rational", "float",
    "symbol", "symbol_int", "symbol_symbol",
    "one_int_heavy", "two_ints_heavy", "three_ints_heavy", "symbol_int_heavy",
    "small_heavy", "heavy_small", "float_heavy_exp", "rational_heavy_numerator",
    "four_arg_heavy_last", "four_arg_heavy_first", "four_arg_heavy_middle",
    "five_arg_heavy_last", "four_arg_heavy_all_numeric",
)

#: The probe CHILD process — run standalone (`python -c PROBE_SCRIPT`)
#: inside a subprocess this script's own `_probe_shape` spawns, one per
#: (name, shape). Kept as a template string, not a separate committed
#: file, so the whole measurement contract lives in ONE script. Builds
#: its args from `_SHAPE_BUILDERS`, a literal dict keyed by the SAME
#: shape names as `PROBE_SHAPES` (above) — no `eval`/`exec` on any
#: string, caller-supplied or otherwise: `check_no_eval.py`'s own gate
#: (this module's file included in the scan) holds this script to the
#: identical bar `codecalc/safe_expr.py` itself already is.
_PROBE_CHILD = textwrap.dedent("""
    import sys, time
    # THE-1095 round 15 (grok issue 3, `verify-1095-r14-grok.log`): the
    # FIRST version of this child called `resource.setrlimit(RLIMIT_AS,
    # ...)` unconditionally -- `resource` does not exist on Windows at
    # all (`import resource` itself crashes the child, before a single
    # shape ever runs) and `RLIMIT_AS` is documented to MISBEHAVE on
    # macOS (confirmed live on PR #343's own CI: every sampled name,
    # every shape, came back CRASH on both `windows-latest` and
    # `macos-latest`, at the exact outer wall-clock bound -- the child
    # was dying before printing anything recognizable, on EVERY
    # platform but Linux).
    #
    # THE-1095 round 16 (coordinator addendum, item 6): the round-15
    # fix (try `RLIMIT_DATA` on macOS) was STILL not enough -- confirmed
    # live on PR #343's own macOS py3.14 job, EVERY sampled name, EVERY
    # shape, STILL came back CRASH at the exact 8000ms process-wall
    # bound, meaning `RLIMIT_DATA` itself raises there too on at least
    # some macOS/Python combinations (it is not universally supported
    # either). Two independent fixes: (1) the WHOLE setup block is now
    # wrapped in `try`/`except`, falling through Linux `RLIMIT_AS` ->
    # macOS `RLIMIT_DATA` -> NO LIMIT AT ALL if both raise -- a
    # documented, weaker guarantee rather than a silent crash; (2) the
    # child now ALWAYS prints a verdict line, even when every limit
    # attempt failed (`SETUP_FAILED <reason>` if some genuinely
    # unexpected error occurs during setup itself, distinct from
    # `OK`/`UNSUPPORTED_TYPE`/`UNSUPPORTED_VALUE`/`TIMEOUT`) -- the
    # PARENT (`_probe_shape`, below) now tells "the child died before
    # printing anything at all" (a real `CRASH`, worth investigating)
    # apart from "the child ran and told us setup failed on this
    # platform" (a `SETUP_FAILED` verdict, `measure_name`'s own
    # classification treats as NOT MEASURABLE here -- skipped, never a
    # regression signal, and surfaced in test output so a genuine new
    # platform gap is still visible rather than silently invisible the
    # way an unconditional CRASH used to make every shape look
    # identical to a hang).
    _mem_limit_note = "none"
    try:
        if sys.platform.startswith("linux"):
            import resource
            resource.setrlimit(resource.RLIMIT_AS, ({mem_kb} * 1024, {mem_kb} * 1024))
            _mem_limit_note = "RLIMIT_AS"
        elif sys.platform == "darwin":
            import resource
            try:
                resource.setrlimit(resource.RLIMIT_DATA, ({mem_kb} * 1024, {mem_kb} * 1024))
                _mem_limit_note = "RLIMIT_DATA"
            except (ValueError, OSError):
                _mem_limit_note = "none (RLIMIT_DATA unavailable on this macOS/Python)"
        # Windows: no rlimit equivalent at all -- stays "none".
    except Exception as _setup_exc:
        print("SETUP_FAILED", type(_setup_exc).__name__, str(_setup_exc))
        sys.exit(0)
    import sympy as sp
    from sympy import Rational, Symbol, Float
    _x, _y = Symbol('x'), Symbol('y')
    _H = {heavy}
    _SHAPE_BUILDERS = {{
        "one_int": (5,), "two_ints": (5, 3), "three_ints": (5, 3, 2),
        "rational": (Rational(1, 3),), "float": (Float(0.5),),
        "symbol": (_x,), "symbol_int": (_x, 5), "symbol_symbol": (_x, _y),
        # DISTINCT heavy values (not `_H` repeated) -- a callable whose
        # real signature reads extra positional args as sympy
        # "generators" (`factor`, `Poly`, ...) raises `GeneratorsError:
        # duplicated generators` for two EQUAL arguments regardless of
        # their magnitude, a shape-mismatch artifact that has nothing
        # to do with whether a large VALUE is itself slow -- confirmed
        # live: `factor(1463, 1463, 1463)` raised that error while
        # `factor(1463, 999, 500)` (same magnitudes, distinct values)
        # did not, and the identical-value probe wrongly excluded
        # `factor` from the allowlist over it (`factor(30)` -- ordinary
        # calculator input -- started refusing outright). Distinct
        # values near `_H` still probe the SAME magnitude-cost question
        # the heavy shapes exist for, without this artifact.
        "one_int_heavy": (_H,), "two_ints_heavy": (_H, _H - 1),
        "three_ints_heavy": (_H, _H - 1, _H - 2), "symbol_int_heavy": (_x, _H),
        # THE-1095 round 15 (grok issue 2b): a WIDE range between two
        # arguments (rather than both individually near the cap) --
        # `1463 - 2`, not `1463 - 1462` -- probes a DIFFERENT cost
        # shape (an interval/range-scaling algorithm, not a per-
        # argument-magnitude one).
        "small_heavy": (2, _H), "heavy_small": (_H, 2),
        # A `Float` whose EXPONENT is huge (`1e300`) while its own
        # MANTISSA carries only a handful of significant digits --
        # unlike `float_heavy`'s large-but-ordinary-precision value,
        # this probes whether a callable's cost scales with a Float's
        # own MAGNITUDE, not merely its digit count.
        "float_heavy_exp": (Float("1e300"),),
        # A `Rational` whose NUMERATOR alone is a 25-digit integer --
        # the shape grok's own review named for `nsimplify`/
        # `egyptian_fraction`/`continued_fraction*`.
        "rational_heavy_numerator": (Rational(1234567890123456789012345, 3),),
        # A FOUR-positional-argument shape with the heavy value LAST --
        # `fps(sin(x), x, 0, 1463)`-class calls (`series`'s own already-
        # dedicated order cap is the model for what such a name would
        # need if it survives measurement here; see `main()`'s own
        # `--write` docstring for how a survivor is expected to be
        # handled). No shape before this round had more than THREE
        # positions at all, so a name whose real cost hides behind a
        # 4th argument was never probed here regardless of magnitude.
        "four_arg_heavy_last": (_x, 0, 0, _H),
        # THE-1095 round 16 (coordinator addendum, item 4): heavy in
        # position 0 and position 1 of a 4-arg call -- `jacobi_
        # normalized(1463, 1, 2, x)`-class (heavy FIRST) and a
        # `random_poly(x, 1463, 0, 0)`-class (heavy at position 1,
        # `random_poly(x, degree, ...)`'s own actual cost driver).
        "four_arg_heavy_first": (_H, 1, 2, _x),
        "four_arg_heavy_middle": (_x, _H, 0, 0),
        # A FIVE-positional-argument shape, heavy LAST -- `fps`'s own
        # real `order` is position 5 (0-indexed 4th EXTRA arg after
        # `f`), which no shape before this round reached at all.
        "five_arg_heavy_last": (_x, 0, 0, 0, _H),
        # THE-1095 round 17 (grok issue 3, `verify-1095-r16-grok.log`):
        # EVERY 4-arg heavy shape above keeps a `Symbol` filler in at
        # least one position -- a lazy `Function` subclass whose own
        # `.eval()` only forces real numeric work when ALL of its
        # arguments are numbers (`betainc`/`betainc_regularized`'s own
        # shape: `nargs == {{4}}`, `__new__(cls, *args, **options)`)
        # stays symbolic, and cheap, at every one of those shapes
        # regardless of magnitude. Four DISTINCT (not repeated -- see
        # `two_ints_heavy`'s own comment above for why) heavy values,
        # no symbol anywhere, closes that gap.
        "four_arg_heavy_all_numeric": (_H, _H - 1, _H - 2, _H - 3),
    }}
    fn = getattr(sp, {name!r})
    args = _SHAPE_BUILDERS[{shape_name!r}]

    # THE-1095 round 15 (grok issue 3): a daemon THREAD, not `signal.
    # SIGALRM`/`setitimer` (POSIX-only, confirmed the direct cause of
    # the CI crash above) -- works identically on Linux, macOS, and
    # Windows. It cannot forcibly ABORT a CPU-bound call the way a
    # signal can (a thread that is still running when the join below
    # times out just keeps running) -- but the ISOLATED CHILD PROCESS
    # itself still dies, taking the daemon thread with it, the moment
    # the PARENT's own `subprocess.run(timeout=PROBE_TIMEOUT_S)` gives
    # up waiting -- the SAME hard backstop this script already had.
    import threading

    class _ProbeSlot:
        result = None
        error = None
        done = False

    def _target():
        try:
            _ProbeSlot.result = fn(*args)
        except BaseException as exc:  # noqa: BLE001 -- classified below
            _ProbeSlot.error = exc
        _ProbeSlot.done = True

    t0 = time.perf_counter()
    _thread = threading.Thread(target=_target, daemon=True)
    _thread.start()
    _thread.join({call_timeout})
    if not _ProbeSlot.done:
        # The CALL exceeded its own budget; interpreter start-up and the
        # sympy import are deliberately outside this timer (the thread
        # starts only after both), so a loaded box cannot turn a
        # trivial call into a false TIMEOUT.
        print("TIMEOUT")
        sys.exit(0)
    _exc = _ProbeSlot.error
    if isinstance(_exc, TypeError):
        # "This shape does not apply to this callable" -- an arity/
        # TYPE mismatch at `inspect.signature`'s own binding level
        # (never a verdict on whether a shape this callable DOES
        # accept is itself slow or dangerous -- that is what "OK",
        # below, still answers). THE-1095 round 15 (grok issue 2b):
        # tagged distinctly from the "value/domain" exceptions below --
        # `measure_name`'s own Rule-3 gate treats a TypeError at a
        # HEAVY shape as definitive evidence "this whole shape category
        # does not apply here," unlike a same-shape ValueError.
        print("UNSUPPORTED_TYPE", str(_exc))
        sys.exit(0)
    if isinstance(_exc, (AttributeError, ValueError, NotImplementedError,
                          sp.polys.polyerrors.BasePolynomialError,
                          ZeroDivisionError, KeyError, IndexError)):
        # The shape's ARGUMENTS bind fine (a real `Function.__call__`
        # ran), but the VALUES are outside this callable's own accepted
        # domain (e.g. gcd(5, 3, 2) probing `2` as a third positional
        # GENERATOR argument, which sympy's own gcd() reads as a Poly
        # option and chokes on with an AttributeError). THE-1095 round
        # 15 (grok issue 2b, `verify-1095-r14-grok.log`): at a NON-heavy
        # shape this stays the same "does not apply, no signal either
        # way" verdict round 13/14 already gave it -- but a HEAVY-shape
        # exception of this kind is now DISTINCT evidence: the call
        # bound and RAN, at the actual cap-boundary VALUE, and failed
        # fast rather than hanging -- reassuring, but (per `measure_
        # name`'s own Rule-3 gate) not BY ITSELF proof this name is
        # safe at every other value near the cap the way an actual
        # heavy-shape "OK" completion, or a heavy-shape TypeError
        # (proving the whole shape category is irrelevant here), would
        # be -- `discrete_log(5, 3, 2)` measuring 0.04ms while its own
        # HEAVY shape raised this exact kind of exception, with NO
        # heavy shape ever confirmed cheap OR confirmed inapplicable,
        # was allowlisted under the OLD rules on the strength of the
        # small shape alone; this tag is what lets `measure_name` catch
        # that gap instead of silently treating it as if the heavy
        # boundary had been checked at all.
        print("UNSUPPORTED_VALUE", str(_exc))
        sys.exit(0)
    if _exc is not None:
        raise _exc  # anything else genuinely unexpected -> CRASH, parent-side
    result = _ProbeSlot.result
    dt_ms = (time.perf_counter() - t0) * 1000.0
    digits = 0
    try:
        if isinstance(result, sp.Basic):
            if result.is_number and not result.free_symbols:
                from sympy import Float as _F
                if isinstance(result, _F):
                    digits = int(result._prec * 0.30103)
                else:
                    digits = len(str(result))
            else:
                digits = sp.count_ops(result)
        elif isinstance(result, (list, tuple, dict, set, frozenset)):
            digits = len(result)
        else:
            digits = len(str(result))
    except Exception:
        digits = -1
    print(f"OK {{dt_ms:.3f}} {{digits}}")
""")


def _probe_shape(name: str, shape_name: str) -> tuple[str, float, int]:
    """`(verdict, elapsed_ms, digits)` for ONE (name, shape) pair,
    `verdict` one of `"OK"`, `"UNSUPPORTED_TYPE"` (an arity/type
    mismatch binding this shape — not a failure, just not applicable,
    regardless of whether the shape is heavy), `"UNSUPPORTED_VALUE"`
    (the shape's arguments bound fine but the VALUES were outside this
    callable's own accepted domain — THE-1095 round 15, grok issue 2b:
    kept distinct from `UNSUPPORTED_TYPE` because `measure_name`'s own
    Rule-3 gate treats the two very differently at a HEAVY shape — see
    `_PROBE_CHILD`'s own comment for the full reasoning), `"TIMEOUT"`,
    `"SETUP_FAILED"` (THE-1095 round 16, coordinator addendum item 6:
    the child's own memory-limit setup raised — a platform gap this
    script cannot measure past, NOT a verdict on the callable at all —
    `measure_name` treats it as "not measurable here", skipped, never
    a regression), or `"CRASH"` (the child died WITHOUT printing any
    recognized verdict line at all — genuinely worth investigating,
    now that `SETUP_FAILED` exists to catch the platform-gap case
    separately). Spawns a fresh, `ulimit -v`-capped child process per
    call — see this module's own docstring for why isolation is per
    (name, shape), not merely per name.
    """
    from codecalc.safe_expr import MAX_HEAVY_ARG
    child_src = _PROBE_CHILD.format(mem_kb=PROBE_MEMORY_KB, name=name,
                                     shape_name=shape_name, heavy=MAX_HEAVY_ARG,
                                     call_timeout=PROBE_CALL_TIMEOUT_S)
    try:
        proc = subprocess.run(
            [sys.executable, "-c", child_src],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return "TIMEOUT", PROBE_TIMEOUT_S * 1000.0, -1
    out = proc.stdout.strip()
    if out.startswith("OK "):
        _, dt_ms, digits = out.split(maxsplit=2)
        return "OK", float(dt_ms), int(digits)
    if out.startswith("UNSUPPORTED_TYPE"):
        return "UNSUPPORTED_TYPE", 0.0, 0
    if out.startswith("UNSUPPORTED_VALUE"):
        return "UNSUPPORTED_VALUE", 0.0, 0
    if out.startswith("TIMEOUT"):
        return "TIMEOUT", PROBE_CALL_TIMEOUT_S * 1000.0, -1
    if out.startswith("SETUP_FAILED"):
        return "SETUP_FAILED", 0.0, 0
    return "CRASH", PROBE_TIMEOUT_S * 1000.0, -1


#: Verdicts a repeated probe treats as "worse" than an `OK`, in the
#: order `_probe_shape_repeated` (below) prefers when combining
#: multiple runs of the SAME (name, shape) pair — `CRASH` outranks
#: `TIMEOUT`, which outranks anything else, so a single bad run among
#: several good ones still surfaces as the combined verdict.
_PROBE_VERDICT_SEVERITY = {"CRASH": 3, "TIMEOUT": 2, "OK": 1}


def _probe_shape_repeated(name: str, shape_name: str,
                           repeats: int = 3) -> tuple[str, float, int]:
    """`_probe_shape`'s own `(verdict, elapsed_ms, digits)`, but run
    `repeats` times and combined WORST-CASE, not just once.

    THE-1095 round 18 (coordinator item 4, macOS `tests (macos-latest,
    py3.14)` CI failure on `711242f`): `discrete_log(1463, 1462, 1461,
    1460)` measured `OK` (a few ms) on the Linux box that wrote the
    committed allowlist, then `CRASH` (an 8000ms process-wall timeout)
    on macOS py3.14 — SAME call, SAME allowlist entry, genuinely
    DIFFERENT outcomes, because `discrete_log`'s own cost depends on
    number-theoretic structure a random Pollard's-Rho walk can get
    lucky or unlucky on, not magnitude (see `_NONDETERMINISTIC_COST_
    NAMES`'s own comment in `codecalc/safe_expr.py` — that whole family
    is now excluded from measurement candidacy entirely, which is the
    PRIMARY fix; this is the secondary, general-purpose backstop for
    any OTHER name with a similar hidden randomized-cost shape this
    module has not identified yet). A single probe run cannot tell
    "genuinely fast" from "got lucky this once" apart — `repeats`
    independent runs (a fresh child process each time, the SAME
    isolation `_probe_shape` already uses per call) can: allowlisting
    now requires EVERY run to be `OK` and under `FAST_MS`, not just one
    of them.

    Combining rule: if ANY run is `CRASH` or `TIMEOUT`, that verdict
    wins (worse of the two if both occur) with the WORST (largest)
    elapsed time among the bad runs. Otherwise, if every run is `OK`,
    the run with the LARGEST `ms`/`digits` wins (the worst of an
    otherwise-consistent set — still a real measurement, not an
    artifact). Otherwise (a mix of `OK` and `UNSUPPORTED_TYPE`/
    `UNSUPPORTED_VALUE`/`SETUP_FAILED`, or all non-`OK`) the FIRST
    run's verdict is kept — these are DETERMINISTIC outcomes (arity/
    domain/platform-setup, none of them randomized), so repeats add no
    information there; costs three subprocess spawns instead of one
    only where it can actually change the verdict.
    """
    results = [_probe_shape(name, shape_name) for _ in range(repeats)]
    bad = [r for r in results if r[0] in _PROBE_VERDICT_SEVERITY and r[0] != "OK"]
    if bad:
        return max(bad, key=lambda r: (_PROBE_VERDICT_SEVERITY[r[0]], r[1]))
    ok = [r for r in results if r[0] == "OK"]
    if ok and len(ok) == len(results):
        return max(ok, key=lambda r: (r[1], r[2]))
    return results[0]


def measure_name(name: str, repeats: int = 3) -> dict:
    """Every probed shape's own verdict for `name`, plus the overall
    classification (`allowlisted`: bool) — a name is allowlisted only
    when: every `"OK"` shape stayed under `FAST_MS` and `MAX_RESULT_
    DIGITS`; at least one shape was applicable at all (a name every
    probe shape rejects as unsupported — an unusual signature this grid
    does not happen to match — is left OFF the allowlist: `unknown !=
    safe`, no exception for "never measured accepting anything"); no
    shape TIMED OUT or CRASHED; and (THE-1095 round 15, grok issue 2b —
    the Rule-3 gate) at least one HEAVY shape (`_HEAVY_SHAPES`) came
    back `"OK"` or `"UNSUPPORTED_TYPE"` — a name whose EVERY heavy
    shape came back `"UNSUPPORTED_VALUE"` (the call ran, at the actual
    cap-boundary value, and failed fast — reassuring, but not proof of
    safety at every OTHER value near the cap the way an actual `"OK"`
    completion, or a `"UNSUPPORTED_TYPE"` proving the whole shape
    category is irrelevant here, would be) is left OFF the allowlist
    too: the heavy boundary was never actually CONFIRMED safe for it,
    only never caught failing slow — `discrete_log` is the concrete

    THE-1095 round 18 (coordinator item 4): `repeats` (default 3, per
    the coordinator's own "measure three times, allowlist only if all
    three are fast" instruction) is passed straight to `_probe_shape_
    repeated` — see its own docstring for why a SINGLE probe cannot
    tell "genuinely fast" from "got lucky this once" apart for a
    randomized-cost name. `shard_check` (below) passes `repeats=1`
    explicitly: it is a CI re-verification pass run on every push, not
    the (far rarer) allowlist-WRITING decision this default targets,
    and relies on its own separate "nondeterministic cost" finding
    category instead of tripling its own runtime to catch the same
    class of name (`discrete_log` is the concrete case grok's own
    review found this gap in — see `_NONDETERMINISTIC_COST_NAMES`).
    """
    shapes = []
    applicable = 0
    allowlisted = True
    heavy_confirmed = False
    setup_failed_shapes = []
    for shape_src in PROBE_SHAPES:
        verdict, dt_ms, digits = _probe_shape_repeated(name, shape_src, repeats)
        shapes.append({"shape": shape_src, "verdict": verdict,
                        "ms": round(dt_ms, 3), "digits": digits})
        is_heavy = shape_src in _HEAVY_SHAPES
        if verdict == "OK":
            applicable += 1
            if is_heavy:
                heavy_confirmed = True
            if dt_ms >= FAST_MS or (digits >= 0 and digits > MAX_RESULT_DIGITS):
                allowlisted = False
        elif verdict == "UNSUPPORTED_TYPE":
            if is_heavy:
                heavy_confirmed = True
        elif verdict == "UNSUPPORTED_VALUE":
            pass  # no signal either way -- see this function's own docstring
        elif verdict == "SETUP_FAILED":
            # THE-1095 round 16 (coordinator addendum, item 6): a
            # PLATFORM gap (the child's own memory-limit setup raised),
            # not a verdict on the callable at all -- "not measurable
            # HERE", never counted toward allowlisting OR disqualifying
            # it, but tracked separately so it stays VISIBLE (a name
            # allowlisted with every shape SETUP_FAILED on some platform
            # was never actually confirmed safe there at all).
            setup_failed_shapes.append(shape_src)
        else:  # TIMEOUT or CRASH
            allowlisted = False
    return {"name": name,
            "allowlisted": bool(allowlisted and applicable > 0 and heavy_confirmed),
            "shapes": shapes, "setup_failed_shapes": setup_failed_shapes}


def measure_all() -> tuple[dict, dict]:
    """`(data, results)` — `data` is the TRIMMED record (names, the
    measurement date, and the two thresholds every name was measured
    against — no per-shape timing), `results` is the FULL per-
    candidate, per-shape probe log (name -> `measure_name`'s own
    return). Split so `main()` can ship only `data` in
    `codecalc/_measured_safe_callables.json` (the file `_load_
    measured_safe_callables` actually loads, and the one that ends up
    in a built package) while the much larger raw `results` — useful
    for auditing WHY a name did or didn't make the allowlist, not for
    runtime loading — goes to a separate, non-shipped report instead
    (THE-1095 round 14, coordinator follow-up: the FIRST version of
    this script wrote both into the SAME committed file, 1.3MB of raw
    per-shape timing data next to the ~10KB of names the runtime
    actually needs — correct to regenerate and audit, wasteful to ship
    in a package meant to install quickly).
    """
    from codecalc import safe_expr as se

    real = se.safe_global_dict()
    handled = _already_handled_names()
    candidates = sorted(
        n for n, obj in real.items()
        if callable(obj) and n not in handled and not n.startswith("_")
    )
    results = {}
    for i, name in enumerate(candidates):
        results[name] = measure_name(name)
        if (i + 1) % 100 == 0:
            print(f"... measured {i + 1}/{len(candidates)}", file=sys.stderr)
    allowlist = sorted(n for n, r in results.items() if r["allowlisted"])
    data = {
        "measured_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fast_ms_threshold": FAST_MS,
        "max_result_digits": MAX_RESULT_DIGITS,
        "candidates_measured": len(candidates),
        "allowlist": allowlist,
    }
    return data, results


def _prune_handled_names(raw_results: dict) -> dict:
    """`raw_results` with every key `_already_handled_names()` NOW
    excludes dropped — a name that used to be a genuine measurement
    candidate but has SINCE been given a dedicated table/pole-sensitive/
    elementary row (round 16 addendum: `erf`, `li`, `airyai`,
    `airyaiprime`, `expint`, `Si`, `Ci`, `fps`, `Piecewise`, `summation`,
    `product`, ...) stays in an OLD committed raw report (`refresh_
    excluded`/`refresh_names` both start from it) even after `measure_
    all`'s own candidate list stops including it — its OWN dedicated
    mechanism should be the ONLY thing deciding it, never the measured
    list. THE-1095 round 16 (coordinator's own live spot-check of the
    uncommitted round-16 tree): `--refresh-names`'s own merge put `erf`
    (and six siblings) BACK on the allowlist this exact way, wrongly
    re-enabling `_measured_safe_argument_cap_violation`'s own generic
    `MAX_HEAVY_ARG` cap for a name whose dedicated bound never needed
    one at all (`erf(10000)` wrongly refused again). Called by every
    write path that starts from an EXISTING raw report — `measure_all`
    never needs it, its own candidate list is already filtered up
    front.
    """
    handled = _already_handled_names()
    return {name: rec for name, rec in raw_results.items() if name not in handled}


def refresh_excluded() -> tuple[dict, dict, list[str]]:
    """Re-measure ONLY the names the committed raw report left off the
    allowlist on a verdict that MAY be a load artifact rather than a
    real exclusion. A heavy-shape TIMEOUT at the in-child `PROBE_CALL_
    TIMEOUT_S` bound is the measurement doing its job (the call itself
    is slow at the cap) — kept as-is. Four load-artifact-prone shapes,
    all re-measured here:

    - a `CRASH` (any shape) — most often the old process-wall timer
      counting interpreter start-up on a loaded box (the timer now
      bounds the call alone, in-child, but a `CRASH` can still mean
      the child died before printing anything recognizable at all);
    - a `TIMEOUT` on a NON-heavy shape (never expected to be slow);
    - THE-1095 round 15 (coordinator addendum): a `TIMEOUT`/`CRASH`
      whose own `ms` sits AT the OUTER process-wall bound (`PROBE_
      TIMEOUT_S * 1000`, not the in-child `PROBE_CALL_TIMEOUT_S * 1000`
      one) — a signal the PARENT's own `subprocess.run(timeout=...)`
      gave up, not that the CALL itself was measured slow, which can
      happen from box-wide contention (another process saturating the
      CPU) regardless of which shape it hit;
    - a MARGINAL `OK` (`FAST_MS <= ms < 3 * FAST_MS`) — comfortably
      over the tight `FAST_MS` bar this module measures against, but
      well under a magnitude that would suggest a real, structural
      slowness rather than a loaded runner (confirmed live: `LambertW`/
      `Ei`/`lambdify` all measured 76-79ms on a box independently
      confirmed to have a stray process pinning one CPU core at 100%
      for over 10 hours during that exact run).

    The rest of the report is kept as measured; `candidates_measured`,
    thresholds and the allowlist are recomputed from the merged log.
    """
    process_wall_ms = PROBE_TIMEOUT_S * 1000.0
    marginal_floor_ms = FAST_MS
    marginal_ceiling_ms = FAST_MS * 3.0
    raw_results = _prune_handled_names(json.loads(RAW_REPORT_PATH.read_text()))
    targets = sorted(
        name for name, rec in raw_results.items()
        if not rec["allowlisted"] and any(
            p["verdict"] == "CRASH"
            or (p["verdict"] == "TIMEOUT" and p["shape"] not in _HEAVY_SHAPES)
            or (p["verdict"] in ("TIMEOUT", "CRASH") and p["ms"] >= process_wall_ms)
            or (p["verdict"] == "OK" and marginal_floor_ms <= p["ms"] < marginal_ceiling_ms)
            for p in rec["shapes"])
    )
    for i, name in enumerate(targets):
        raw_results[name] = measure_name(name)
        if (i + 1) % 25 == 0:
            print(f"... refreshed {i + 1}/{len(targets)}", file=sys.stderr)
    # THE-1095 round 17 (grok's own methodology note,
    # `verify-1095-r16-grok.log`): the FIRST `_prune_handled_names` call
    # (above, before this loop) only protects names ALREADY in the
    # report when this function started — a name inside `targets` that
    # became handled AFTER that prune (its own `_already_handled_names`
    # entry landed the same round as this refresh) would be written
    # straight back in by this loop's own `raw_results[name] = measure_
    # name(name)`, since `targets` is computed from the PRE-prune
    # report and the loop does not re-check. Pruned again, after the
    # loop, so the disjointness test in `tests/test_measured_safe_
    # callables.py` stays a backstop rather than the only thing
    # catching this.
    raw_results = _prune_handled_names(raw_results)
    allowlist = sorted(n for n, r in raw_results.items() if r["allowlisted"])
    data = {
        "measured_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fast_ms_threshold": FAST_MS,
        "max_result_digits": MAX_RESULT_DIGITS,
        "candidates_measured": len(raw_results),
        "allowlist": allowlist,
    }
    return data, raw_results, targets


def refresh_names(names: list[str]) -> tuple[dict, dict, list[str]]:
    """Re-measure an EXPLICIT list of names (any that exist in the
    committed raw report; a name not there yet is measured fresh and
    added), merge into the raw report, and recompute the allowlist.

    THE-1095 round 16 (coordinator addendum, item 4; grok issue 3,
    `verify-1095-r15-grok.log`): a full `--write` run takes ~2 hours;
    the probe-grid widening this round added (heavy value in position
    0/1/4, not only 3) only matters for names whose own real signature
    accepts 4+ positional arguments — re-measuring the WHOLE 767-name
    surface to re-check a widened grid that cannot change the verdict
    for a 1-3-arg name at all wastes ~2 hours to re-confirm ~750
    unaffected verdicts. `--refresh-names <file>` (one name per line,
    `#`-prefixed lines and blank lines ignored) targets exactly the
    names that COULD be affected instead — the CLI path computes that
    list itself (every candidate `inspect.signature` reports 4+
    accepted positional parameters for), so a caller does not have to
    hand-enumerate it.
    """
    raw_results = (_prune_handled_names(json.loads(RAW_REPORT_PATH.read_text()))
                   if RAW_REPORT_PATH.exists() else {})
    for i, name in enumerate(names):
        raw_results[name] = measure_name(name)
        if (i + 1) % 10 == 0:
            print(f"... refreshed {i + 1}/{len(names)}", file=sys.stderr)
    # THE-1095 round 17 (grok's own methodology note,
    # `verify-1095-r16-grok.log`): see `refresh_excluded`'s own,
    # identical, second call for the full reasoning — a HANDLED name
    # inside `names` itself (hand-written, or the auto-generated
    # `_names_with_min_positional_args` file) would otherwise be
    # written straight back into `raw_results` by the loop above,
    # regardless of the FIRST prune before it.
    raw_results = _prune_handled_names(raw_results)
    allowlist = sorted(n for n, r in raw_results.items() if r["allowlisted"])
    data = {
        "measured_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fast_ms_threshold": FAST_MS,
        "max_result_digits": MAX_RESULT_DIGITS,
        "candidates_measured": len(raw_results),
        "allowlist": allowlist,
    }
    return data, raw_results, names


def _names_with_min_positional_args(min_args: int) -> list[str]:
    """Every candidate name (per `measure_all`'s own `_already_handled_
    names` exclusion) whose real callable's own `inspect.signature`
    reports at least `min_args` EXPLICIT (fixed) positional parameters
    — the set a wider heavy-probe-position grid could actually change
    the verdict for. A name `inspect.signature` cannot introspect (a
    C-implemented callable, `TypeError` on `signature()`) is included
    defensively — `unknown != safe`, better to re-measure a name that
    turns out unaffected than skip one that was.

    Deliberately EXCLUDES a bare `*args` (`VAR_POSITIONAL`) alone from
    counting toward `min_args` — a name like `sympify(*args, **kwargs)`
    is technically "4+ positional capable" but is not the concrete
    "fixed slot N has the real cost" shape this round's own probe-grid
    widening targets (`jacobi_normalized`/`fps`/`random_poly`/`Ynm` all
    have ORDINARY, fixed signatures) — including every `*args` name
    would balloon this list from ~95 to ~300+ without adding coverage
    for the actual class of concern.
    """
    import inspect

    from codecalc import safe_expr as se

    real = se.safe_global_dict()
    handled = _already_handled_names()
    candidates = sorted(
        n for n, obj in real.items()
        if callable(obj) and n not in handled and not n.startswith("_")
    )
    result = []
    for name in candidates:
        obj = real[name]
        # THE-1095 round 17 (grok issue 3, `verify-1095-r16-grok.log`):
        # a SymPy `Function` subclass (e.g. `betainc`/`betainc_
        # regularized`, both `nargs == {4}`) has `__new__(cls, *args,
        # **options)` — `inspect.signature` on it reports ONLY a bare
        # `VAR_POSITIONAL`, which the `positional` count below
        # deliberately excludes (this function's own docstring), so its
        # TRUE runtime arity was invisible to this check entirely and
        # it never got the round-16 heavy-first/middle re-probe.
        # `nargs` (a `FiniteSet` of the arities SymPy itself will
        # accept) is the authoritative source for that family — checked
        # ALONGSIDE `inspect.signature`, not instead of it, since a
        # plain function's true arity still only comes from its
        # signature.
        nargs = getattr(obj, "nargs", None)
        nargs_max = 0
        if nargs is not None:
            # `nargs` is a SymPy `FiniteSet` for a FIXED-arity name
            # (`betainc` -> `{4}`) but an INFINITE set for a variadic
            # one (`Max`/`Min` -> `Naturals0`, every non-negative
            # integer) -- `max()` over that would iterate forever.
            # Only a genuinely finite set can be maxed directly; an
            # infinite one already means "unbounded, re-probe it"
            # without iterating at all.
            from sympy import FiniteSet as _FiniteSet
            if isinstance(nargs, _FiniteSet):
                try:
                    nargs_max = max(int(n) for n in nargs)
                except (TypeError, ValueError):
                    nargs_max = min_args  # unresolvable shape -- unknown != safe, re-probe it
            else:
                nargs_max = min_args  # infinite/unbounded arity -- unknown != safe, re-probe it
        try:
            sig = inspect.signature(obj)
        except (TypeError, ValueError):
            result.append(name)
            continue
        positional = sum(
            1 for p in sig.parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        )
        if max(positional, nargs_max) >= min_args:
            result.append(name)
    return result


def resample_check(sample_size: int) -> bool:
    """Re-probes a random SAMPLE of already-allowlisted names on the
    RUNNING box; returns `True` iff every one still measures under
    `FAST_MS` at every shape it accepted when the committed list was
    generated. A developer-facing spot check (this script's own
    `--sample` CLI path) — `tests/test_measured_safe_callables.py`
    itself uses `shard_check`, below, which covers every allowlisted
    name across the CI matrix instead of a fixed random 30, and tolerates
    a loaded runner instead of re-applying `FAST_MS` at full strictness
    (see that function's own docstring for why).
    """
    data = json.loads(DATA_PATH.read_text())
    names = data["allowlist"]
    sample = random.Random(0).sample(names, min(sample_size, len(names)))  # noqa: S311 — deterministic sampling, not crypto
    ok = True
    for name in sample:
        result = measure_name(name)
        if not result["allowlisted"]:
            print(f"REGRESSION: {name!r} no longer measures allowlist-fast "
                  f"on this box -> {result['shapes']}")
            ok = False
    return ok


#: THE-1095 round 15 (grok issue 3, `verify-1095-r14-grok.log`): matches
#: `ci-python.yml`'s own `tests` job matrix (3 OS x 2 Python versions) --
#: if that matrix ever grows or shrinks, this should follow it, so every
#: shard still gets covered by SOME CI job. A mismatch is not silently
#: wrong either way: `_shard_id` falls back to a hash-based shard for
#: any `(platform, python-minor)` pair not in this table, so a NEW CI
#: leg (or a local run on a python version not in the matrix) still
#: gets SOME shard, just not guaranteed to line up 1:1 with a specific
#: CI job the way the six explicit entries do.
_SHARD_KEYS: list[tuple[str, int]] = [
    ("linux", 11), ("linux", 14),
    ("darwin", 11), ("darwin", 14),
    ("win32", 11), ("win32", 14),
]
NUM_SHARDS = len(_SHARD_KEYS)
#: Deliberately well above `FAST_MS` (60ms) -- a LOADED CI runner
#: crossing the original, tightly-measured threshold is not evidence
#: this NAME regressed, only that the MACHINE is slower right now (grok
#: issue 3's own finding: `continued_fraction_periodic`'s own `three_
#: ints_heavy` shape measured 57.789ms on ONE run, uncomfortably close
#: to `FAST_MS` already without any real regression). 4x leaves ample
#: margin for that noise while still catching a GENUINE order-of-
#: magnitude regression (a name that used to be a few ms and is now
#: hundreds).
REGRESSION_MS_TOLERANCE = FAST_MS * 4.0


def _shard_id(num_shards: int = NUM_SHARDS) -> int:
    """This process's own shard index, `0 <= id < num_shards` —
    deterministic from `(platform, python minor version)` so the SAME
    CI job always re-measures the SAME shard of the allowlist, and the
    six jobs in `ci-python.yml`'s own `tests` matrix collectively cover
    every allowlisted name across a single CI run instead of each job
    re-probing an overlapping random 30 (THE-1095 round 15, grok issue
    3's own request: "sample deterministically but cover all 457 names
    across the CI matrix ... rather than 30").
    """
    plat = "linux" if sys.platform.startswith("linux") else sys.platform
    key = (plat, sys.version_info.minor)
    if key in _SHARD_KEYS:
        return _SHARD_KEYS.index(key) % num_shards
    return hash(key) % num_shards


def shard_check(num_shards: int = NUM_SHARDS) -> list[str]:
    """Re-probes THIS process's own SHARD of the committed allowlist
    (`_shard_id`, above) and returns the names that show a GENUINE
    regression — empty means clean. THE-1095 round 15 (grok issue 3):
    two changes from `resample_check`'s own all-or-nothing `FAST_MS`
    re-check, both needed to run this on the FULL CI matrix (`ci-
    python.yml`'s `windows-latest`/`macos-latest` legs, not just
    `ubuntu-latest`) rather than being red every run there regardless
    of any real regression:

    (1) Only a `TIMEOUT`, a `CRASH`, or an over-`MAX_RESULT_DIGITS`
    result counts as a regression — a merely-SLOWER-than-`FAST_MS` `OK`
    result does not, as long as it stays under `REGRESSION_MS_
    TOLERANCE` (4x `FAST_MS`, above): the ORIGINAL measurement's own
    `FAST_MS` bar was already "deliberately well under the
    coordinator's own 100ms bar, to leave margin against machine-to-
    machine variance" (this module's own docstring) — re-applying that
    SAME tight bar on a re-check, on a runner that may be more loaded
    than the one the list was measured on, conflates "this box is busy
    right now" with "this name got slower," exactly the false-positive
    `continued_fraction_periodic` already measured crossing 60ms on one
    run with no real regression at all.

    (2) Every allowlisted name is covered, split into `num_shards`
    disjoint shards by SORTED index — this process only re-measures its
    OWN shard (`_shard_id`), not a random subset every job re-picks
    independently (which could let SOME allowlisted name go unchecked
    by any CI job at all, purely by bad luck in the random draw).
    """
    # THE-1095 round 18 (coordinator item 4, macOS `tests (macos-
    # latest, py3.14)` CI failure on `711242f`): a `CRASH`/`TIMEOUT`
    # whose own `ms` sits AT the outer process-wall bound (`PROBE_
    # TIMEOUT_S * 1000`, the SAME signal `refresh_excluded`'s own
    # `process_wall_ms` check already uses) is a DIFFERENT finding from
    # an ordinary regression: it means the CALL ITSELF never returned
    # within the wall-clock bound at all, on a name whose committed
    # measurement (and `measure_name`'s own THREE-repeat re-probe,
    # here) has otherwise shown `OK` — the concrete shape a
    # NONDETERMINISTIC-cost name (`discrete_log`'s own Pollard's-Rho
    # walk; see `_NONDETERMINISTIC_COST_NAMES`'s comment in
    # `codecalc/safe_expr.py`) takes, not a genuine regression in the
    # callable's own typical cost. Reported as a SEPARATE finding
    # category (`NONDETERMINISTIC COST` wording) rather than folded
    # into a plain `REGRESSION` line — still a FAILURE (returned in
    # `regressions`, still fails the calling test), just labeled so a
    # human reading CI output does not have to re-derive "was this
    # ever fast at all?" from the raw shape data by hand.
    process_wall_ms = PROBE_TIMEOUT_S * 1000.0
    data = json.loads(DATA_PATH.read_text())
    names = sorted(data["allowlist"])
    shard_id = _shard_id(num_shards)
    my_shard = [n for i, n in enumerate(names) if i % num_shards == shard_id]
    regressions = []
    for name in my_shard:
        result = measure_name(name, repeats=1)  # see measure_name's own docstring
        nondeterministic_shapes = [
            s for s in result["shapes"]
            if s["verdict"] in ("TIMEOUT", "CRASH") and s["ms"] >= process_wall_ms
        ]
        bad_shapes = [
            s for s in result["shapes"]
            if s not in nondeterministic_shapes
            and (s["verdict"] == "TIMEOUT"
                 or s["verdict"] == "CRASH"
                 or (s["verdict"] == "OK"
                     and (s["ms"] >= REGRESSION_MS_TOLERANCE
                          or (s["digits"] >= 0 and s["digits"] > MAX_RESULT_DIGITS))))
        ]
        if nondeterministic_shapes:
            print(f"NONDETERMINISTIC COST: {name!r} -> {nondeterministic_shapes} "
                  "(hit the process-wall bound on a re-probe of a name this "
                  "module's committed measurement found OK -- a randomized-cost "
                  "shape, not a magnitude regression; consider "
                  "_NONDETERMINISTIC_COST_NAMES)")
            regressions.append(f"{name} (nondeterministic cost)")
        if bad_shapes:
            print(f"REGRESSION: {name!r} -> {bad_shapes}")
            regressions.append(name)
        if result.get("setup_failed_shapes"):
            # THE-1095 round 16 (coordinator addendum, item 6): never a
            # regression by itself (a platform gap, not the callable's
            # own fault) but surfaced anyway -- a name every one of
            # whose shapes hits SETUP_FAILED on this platform was never
            # actually re-CONFIRMED safe here at all, worth knowing.
            print(f"SETUP_FAILED (not a regression): {name!r} -> "
                  f"{result['setup_failed_shapes']}")
    return regressions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true",
                         help=f"write the measurement to {DATA_PATH}")
    parser.add_argument("--sample", type=int, default=0,
                         help="re-probe N already-allowlisted names on this box")
    parser.add_argument("--shard-check", action="store_true",
                         help="re-probe THIS process's own shard of the "
                              "allowlist (by platform + python minor version, "
                              "see _shard_id) and fail only on a genuine "
                              "TIMEOUT/CRASH/over-digit regression; what "
                              "tests/test_measured_safe_callables.py itself runs")
    parser.add_argument("--refresh-excluded", action="store_true",
                         help="re-measure only the names the committed raw "
                              "report excluded on a CRASH or a non-heavy "
                              "TIMEOUT verdict (both can be artifacts of the "
                              "box being loaded when the full run happened), "
                              "merge them into the raw report, and rewrite "
                              "both files with a fresh measurement date; "
                              "implies --write")
    parser.add_argument("--refresh-names", metavar="FILE",
                         help="re-measure only the names listed in FILE (one "
                              "per line; # comments and blank lines ignored), "
                              "merge into the raw report, and rewrite both "
                              "files; implies --write. Combine with "
                              "--min-positional-args to generate the list "
                              "instead of hand-writing it")
    parser.add_argument("--min-positional-args", type=int, default=0,
                         help="with --refresh-names, if FILE does not exist, "
                              "write it first as every candidate name whose "
                              "own signature accepts at least this many "
                              "positional arguments (see "
                              "_names_with_min_positional_args), then use it")
    args = parser.parse_args()

    if args.refresh_names:
        target_file = pathlib.Path(args.refresh_names)
        if not target_file.exists():
            if not args.min_positional_args:
                print(f"{target_file} does not exist and --min-positional-args "
                      "was not given -- nothing to generate it from", file=sys.stderr)
                return 1
            names = _names_with_min_positional_args(args.min_positional_args)
            target_file.write_text("\n".join(names) + "\n")
            print(f"wrote {target_file} -- {len(names)} names with >= "
                  f"{args.min_positional_args} positional args", file=sys.stderr)
        else:
            names = [line.strip() for line in target_file.read_text().splitlines()
                      if line.strip() and not line.strip().startswith("#")]
        data, raw_results, refreshed = refresh_names(names)
        DATA_PATH.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        RAW_REPORT_PATH.write_text(json.dumps(raw_results, indent=2, sort_keys=True) + "\n")
        newly = sorted(n for n in refreshed if raw_results[n]["allowlisted"])
        print(f"refreshed {len(refreshed)} named targets; {len(newly)} "
              f"allowlisted: {newly}", file=sys.stderr)
        print(f"wrote {DATA_PATH} -- {len(data['allowlist'])} names allowlisted "
              f"of {data['candidates_measured']} measured", file=sys.stderr)
        return 0

    if args.refresh_excluded:
        data, raw_results, refreshed = refresh_excluded()
        DATA_PATH.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        RAW_REPORT_PATH.write_text(json.dumps(raw_results, indent=2, sort_keys=True) + "\n")
        newly = sorted(n for n in refreshed if raw_results[n]["allowlisted"])
        print(f"refreshed {len(refreshed)} previously excluded names; "
              f"{len(newly)} now allowlisted: {newly}", file=sys.stderr)
        print(f"wrote {DATA_PATH} -- {len(data['allowlist'])} names allowlisted "
              f"of {data['candidates_measured']} measured", file=sys.stderr)
        return 0

    if args.sample:
        ok = resample_check(args.sample)
        print("OK" if ok else "FAIL")
        return 0 if ok else 1

    if args.shard_check:
        regressions = shard_check()
        print("OK" if not regressions else f"FAIL: {regressions}")
        return 0 if not regressions else 1

    data, raw_results = measure_all()
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    if args.write:
        DATA_PATH.write_text(text)
        RAW_REPORT_PATH.write_text(json.dumps(raw_results, indent=2, sort_keys=True) + "\n")
        print(f"wrote {DATA_PATH} -- {len(data['allowlist'])} names allowlisted "
              f"of {data['candidates_measured']} measured", file=sys.stderr)
        print(f"wrote {RAW_REPORT_PATH} (raw per-shape probe log, not shipped)",
              file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
