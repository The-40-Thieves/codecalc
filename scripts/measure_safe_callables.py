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
        | frozenset({"Mod", "Max", "Min", "floor", "ceiling", "frac", "N", "series",
                     "summation", "product", "Sum", "Product", "integrate", "Integral"})
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
_HEAVY_SHAPES = frozenset({
    "one_int_heavy", "two_ints_heavy", "three_ints_heavy", "symbol_int_heavy",
    "small_heavy", "heavy_small", "float_heavy_exp", "rational_heavy_numerator",
    "four_arg_heavy_last",
})

PROBE_SHAPES: tuple[str, ...] = (
    "one_int", "two_ints", "three_ints", "rational", "float",
    "symbol", "symbol_int", "symbol_symbol",
    "one_int_heavy", "two_ints_heavy", "three_ints_heavy", "symbol_int_heavy",
    "small_heavy", "heavy_small", "float_heavy_exp", "rational_heavy_numerator",
    "four_arg_heavy_last",
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
    # platform but Linux). `RLIMIT_DATA` (heap size, not the WHOLE
    # address space) is the standard macOS workaround; Windows has no
    # equivalent rlimit at all, so it gets NONE -- a documented, weaker
    # guarantee on that one platform only, not silently pretended away.
    if sys.platform.startswith("linux"):
        import resource
        resource.setrlimit(resource.RLIMIT_AS, ({mem_kb} * 1024, {mem_kb} * 1024))
    elif sys.platform == "darwin":
        import resource
        resource.setrlimit(resource.RLIMIT_DATA, ({mem_kb} * 1024, {mem_kb} * 1024))
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
    or `"CRASH"`. Spawns a fresh, `ulimit -v`-capped child process per
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
    return "CRASH", PROBE_TIMEOUT_S * 1000.0, -1


def measure_name(name: str) -> dict:
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
    case grok's own review found this gap in.
    """
    shapes = []
    applicable = 0
    allowlisted = True
    heavy_confirmed = False
    for shape_src in PROBE_SHAPES:
        verdict, dt_ms, digits = _probe_shape(name, shape_src)
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
        else:  # TIMEOUT or CRASH
            allowlisted = False
    return {"name": name,
            "allowlisted": bool(allowlisted and applicable > 0 and heavy_confirmed),
            "shapes": shapes}


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
    raw_results = json.loads(RAW_REPORT_PATH.read_text())
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
    allowlist = sorted(n for n, r in raw_results.items() if r["allowlisted"])
    data = {
        "measured_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fast_ms_threshold": FAST_MS,
        "max_result_digits": MAX_RESULT_DIGITS,
        "candidates_measured": len(raw_results),
        "allowlist": allowlist,
    }
    return data, raw_results, targets


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
    data = json.loads(DATA_PATH.read_text())
    names = sorted(data["allowlist"])
    shard_id = _shard_id(num_shards)
    my_shard = [n for i, n in enumerate(names) if i % num_shards == shard_id]
    regressions = []
    for name in my_shard:
        result = measure_name(name)
        bad_shapes = [
            s for s in result["shapes"]
            if s["verdict"] == "TIMEOUT"
            or s["verdict"] == "CRASH"
            or (s["verdict"] == "OK"
                and (s["ms"] >= REGRESSION_MS_TOLERANCE
                     or (s["digits"] >= 0 and s["digits"] > MAX_RESULT_DIGITS)))
        ]
        if bad_shapes:
            print(f"REGRESSION: {name!r} -> {bad_shapes}")
            regressions.append(name)
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
    args = parser.parse_args()

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
