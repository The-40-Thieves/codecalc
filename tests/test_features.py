"""Verify the new feature set over MCP: sessions, files, artifacts, packages,
verdicts, limits, streaming, compact mode."""
import asyncio
import json
import pathlib
import re as _re_mod
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _mcp_client import over_stdio

FAILS = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL':4} {name} {detail}")
    if not cond:
        FAILS.append(name)


async def _txt(r) -> str:
    """Best-effort text extraction from a CallToolResult."""
    if hasattr(r, "structured_content") and r.structured_content is not None:
        return json.dumps(r.structured_content)
    if hasattr(r, "content") and r.content:
        return r.content[0].text
    return str(r)


async def main():
    async with over_stdio() as client:
        tools = (await client.list_tools()).tools
        names = sorted(t.name for t in tools)
        print("tools:", len(names), names)
        for want in ["session_start", "session_stop", "session_list",
                     "session_files", "session_read_file", "session_write_file",
                     "session_artifacts", "install_package", "execute_code_stream"]:
            check(f"tool present: {want}", want in names)

        # 1. session lifecycle + stateful python
        s = await client.call_tool("session_start", {"language": "python3"})
        sid = json.loads(await _txt(s))["session_id"]
        check("session_start returns id", bool(sid), f"-> {sid}")

        r1 = await client.call_tool("execute_code", {"language": "python3",
                                                     "code": "x = 42", "session_id": sid})
        r2 = await client.call_tool("execute_code", {"language": "python3",
                                                     "code": "print(x)", "session_id": sid})
        check("stateful: x persists", "42" in await _txt(r2))

        # 2. file tools
        w = await client.call_tool("session_write_file", {"session_id": sid, "path": "in/data.csv", "content": "a,b\n1,2"})
        r = await client.call_tool("session_read_file", {"session_id": sid, "path": "in/data.csv"})
        check("write+read file", "a,b" in await _txt(r))
        f = await client.call_tool("session_files", {"session_id": sid})
        check("list files", "in/" in await _txt(f))

        # 3. artifact from executed code
        a = await client.call_tool("execute_code", {"language": "python3",
                                                    "code": "open('out.json','w').write('[9,9]')",
                                                    "session_id": sid})
        art = await client.call_tool("session_artifacts", {"session_id": sid})
        check("artifacts detected", "out.json" in await _txt(art))

        # 4. verdict + limits on plain execute
        v = await client.call_tool("execute_code", {"language": "python3",
                                                    "code": "print('hi')"})
        vt = await _txt(v)
        check("verdict present", '"verdict": "OK"' in vt, f"-> {vt[:80]}")
        t = await client.call_tool("execute_code", {"language": "python3",
                                                    "code": "while True: pass", "timeout": 2})
        tt = await _txt(t)
        check("TLE verdict", "TLE" in tt)

        # 5. compact mode
        c = await client.call_tool("execute_code", {"language": "python3",
                                                    "code": "print('z')", "compact": True})
        ct = await _txt(c)
        cj = json.loads(ct)
        # Assert the DECODED value, not the serialized spelling: Python emits
        # CRLF line endings on Windows, so a literal '"stdout": "z\\n"' search
        # against the raw JSON text never matches there even though the
        # decoded stdout is the same one line of output either way.
        check("compact mode",
              cj.get("stdout", "").strip() == "z" and "cpu_ms" not in cj,
              f"-> {ct[:80]}")

        # 6. streaming
        st = await client.call_tool("execute_code_stream", {"language": "python3",
                                                            "code": "import time\nfor i in range(3):\n print('tick', i); time.sleep(0.3)"})
        stt = await _txt(st)
        sj = json.loads(stt)
        if sj.get("streamed") is False:
            # The documented no-native fallback: execute_code_stream runs
            # non-streaming and says so via streamed=False plus a note,
            # rather than raising or silently omitting the field. That is a
            # supported outcome, not a failure — assert its actual content.
            check("streaming falls back to non-streaming (documented no-native mode)",
                  "tick" in sj.get("stdout", "") and "no native executor" in sj.get("note", ""),
                  f"-> streamed={sj.get('streamed')} note={sj.get('note')!r}")
        else:
            check("streaming returns result",
                  sj.get("streamed") is True and "tick" in stt and "streamed_partial" in sj,
                  f"-> streamed={sj.get('streamed')}")

        # 7. package install (network; small pure-python pkg)
        p = await client.call_tool("install_package", {"language": "python3",
                                                       "package": "six", "session_id": sid})
        pt = await _txt(p)
        check("install_package six", '"ok": true' in pt, f"-> {pt[:120]}")

        # 8. cleanup
        await client.call_tool("session_stop", {"session_id": sid})

    # ── #88: the two entry points and `doctor` ────────────────────────────────
# `uvx codecalc` and `pipx run codecalc` always worked — they use the console
# script in [project.scripts]. `python -m codecalc` did not, because there was
# no __main__.py, and that is the form people reach for inside a venv they have
# not activated.
import subprocess as _sp2
import sys as _sys2

_r = _sp2.run([_sys2.executable, "-m", "codecalc", "doctor"],
              capture_output=True, text=True, timeout=180)
check("`python -m codecalc doctor` runs", _r.returncode == 0,
      f"-> rc={_r.returncode} {(_r.stderr or '')[-90:]!r}")
for _needle in ("execution backend", "runtimes", "mcpServers"):
    check(f"  ...and reports {_needle!r}", _needle in _r.stdout,
          f"-> {_r.stdout[:70]!r}")

# The count it prints must be the one the README states and check_claims
# enforces. A raw len(LANGUAGES) is 32 because `c++` and `cpp` are one language
# written twice; printing that would put a third number into circulation.
import re as _re2

from codecalc import registry as _reg2

_readme_n = int(_re2.search(r"\*\*(\d+) languages\*\*",
                            pathlib.Path("README.md").read_text()).group(1))
_m = _re2.search(r"runtimes\s+(\d+)/(\d+)", _r.stdout)
check("doctor's language count matches the README's",
      _m is not None and int(_m.group(2)) == _readme_n,
      f"-> doctor={_m.group(2) if _m else '?'} README={_readme_n} "
      f"raw_registry={len(_reg2.LANGUAGES)}")

# The no-argument path is what an MCP client spawns. Anything that made it
# print to stdout or exit would look like a protocol error to the client.
_probe_src = (
    "import sys; sys.argv=['codecalc']; "
    "from codecalc import server; print('MAIN_RESOLVES', callable(server.main))"
)
_r2 = _sp2.run([_sys2.executable, "-c", _probe_src],
               capture_output=True, text=True, timeout=120)
check("the no-argument entry point is still the MCP server",
      "MAIN_RESOLVES True" in _r2.stdout, f"-> {_r2.stdout[:60]!r}")

# ── #117: compact mode must not hide an unapplied guarantee ───────────────
# The old compact result was exactly {ok, verdict, stdout, exit_code}, so
# `execute_code(no_net=True, compact=True)` on a platform without the shim came
# back looking like a clean sandboxed run with no `unenforced` at all. Token
# efficiency that drops the disclosure is the defect SECURITY.md names by name:
# "anything that makes the server report a guarantee it did not apply".
#
# Driven through a stateful session, because that is the shape where
# `unenforced` is reliably NON-empty on every platform (#104 made the structural
# gaps unconditional). Asserting on a one-shot run would pass on Linux with the
# Rust backend, where `unenforced` is legitimately [] and the whole risk is
# invisible.
from codecalc import server as _srv117
from codecalc import sessions as _sess117

_s117 = _srv117.session_start("python3")
if not _s117.get("ok"):
    print(f"SKIP #117 compact disclosure (no python3 worker: {_s117.get('error')})")
else:
    _sid117 = _s117["session_id"]
    try:
        _full117 = _srv117.execute_code("python3", "print(1)", session_id=_sid117, no_net=True)
        _comp117 = _srv117.execute_code("python3", "print(1)", session_id=_sid117,
                                        no_net=True, compact=True)
        _fu = _full117.get("unenforced") or []
        _cu = _comp117.get("unenforced") or []
        check("a non-empty `unenforced` survives compact mode",
              bool(_cu), f"-> full={len(_fu)} compact={len(_cu)}")
        check("  ...naming every guarantee the full result named",
              len(_cu) == len(_fu) and
              all(e.split(":", 1)[0].strip() in _cu for e in _fu),
              f"-> full={[e.split(':',1)[0] for e in _fu]} compact={_cu}")
        check("  ...and points at where the explanations are",
              "unenforced_detail" in _comp117,
              f"-> {_comp117.get('unenforced_detail')!r}")
        check("compact is still smaller than full",
              len(json.dumps(_comp117)) < len(json.dumps(_full117)),
              f"-> {len(json.dumps(_comp117))} vs {len(json.dumps(_full117))} chars")
    finally:
        _sess117.stop(_sid117)

# An EMPTY disclosure is omitted — saying nothing costs tokens for nothing.
_clean117 = _srv117.execute_code("python3", "print(6*7)", compact=True)
check("an empty `unenforced` is omitted from a compact result",
      "unenforced" not in _clean117 and _clean117.get("stdout", "").strip() == "42",
      f"-> keys={sorted(_clean117)}")

# output_error is the other disclosure field and takes the same path.
check("compact_result keeps a non-empty output_error",
      _srv117.compact_result({"ok": False, "verdict": "RTE", "stdout": "",
                              "exit_code": 1, "output_error": "could not read stdout"}
                             ).get("output_error") == "could not read stdout")
check("  ...and omits a null one",
      "output_error" not in _srv117.compact_result(
          {"ok": True, "verdict": "OK", "stdout": "x", "exit_code": 0,
           "output_error": None}))


# ── #88 item 5: the extras degrade, they do not crash ─────────────────────
# The imports were ALWAYS lazy; what was missing is what happens when one
# fails. An ImportError escaping a tool says "this server is broken" when the
# truth is "you installed the small variant and asked for a tool outside it".
from codecalc import optional as _opt

check("optional.have() answers without importing",
      _opt.have("sympy") is True and _opt.have("definitely_not_a_module") is False,
      f"-> sympy={_opt.have('sympy')}")

# Simulated absence, because the test venv HAS sympy. Asserting the message
# rather than the type: the message is what a caller reads out of the error
# field, and it is the only part that tells them what to do.
try:
    raise _opt.MissingExtra("sympy", "symbolic")
except ImportError as _exc:          # MissingExtra subclasses ImportError
    _msg = str(_exc)
for _needle in ("sympy is not installed", "symbolic", "pip install", "codecalc[full]"):
    check(f"MissingExtra names {_needle!r}", _needle in _msg, f"-> {_msg[:80]!r}")
check("  ...and lists the tools it affects",
      "evaluate_expression" in _msg, f"-> {_msg[-90:]!r}")

# Every module that reaches a heavy dependency must go through require(), or a
# raw ImportError gets back out. Checked against the source, since the test
# environment has the extras installed and cannot prove it by running.
import pathlib as _pl2

for _mod in ("logic.py", "exact.py", "units.py", "parsing.py"):
    _src = _pl2.Path("codecalc") / _mod
    _text = _src.read_text()
    _raw = [l.strip() for l in _text.splitlines()
            if l.strip().startswith(("import sympy", "import z3", "import tree_sitter"))]
    check(f"{_mod}: no raw import of a heavy dependency", not _raw, f"-> {_raw}")

check("pyproject declares the extras",
      all(x in _pl2.Path("pyproject.toml").read_text()
          for x in ("[project.optional-dependencies]", "symbolic", "parsing", "full")),
      "-> optional-dependencies missing")

# ── estimate vs measurement, stated in the result ─────────────────────────
# analyze_complexity and benchmark answer the same question and sit next to
# each other in the tool list. One counts loops; the other runs the program at
# increasing sizes. `analysis: tree-sitter|regex-fallback` distinguished PARSED
# from GUESSED, which is a different axis and was the only one reported — so a
# caller could see analysis="tree-sitter" and reasonably hear "we determined
# the complexity" about a number nobody measured.
from codecalc import complexity as _cx
from codecalc import tools as _tools

_static = _cx.analyze("for i in range(n):\n    for j in range(n):\n        pass", "python3")
check("analyze_complexity declares itself an estimate",
      _static.get("method") == "static-estimate", f"-> {_static.get('method')!r}")
check("  ...while still reporting HOW it read the source",
      _static.get("analysis") in ("tree-sitter", "regex-fallback"),
      f"-> {_static.get('analysis')!r}")
# The two axes are orthogonal: parsed-vs-guessed does not imply measured.
check("  ...and the two are separate fields, not one",
      _static.get("method") != _static.get("analysis"),
      f"-> method={_static.get('method')!r} analysis={_static.get('analysis')!r}")

_bench = _tools.benchmark(
    "import sys\nn=int(sys.stdin.readline())\nx=[i*i for i in range(n)]\nprint(len(x))",
    "python3", sizes="2000,4000,8000")
check("benchmark declares itself empirical",
      _bench.get("ok") is True and _bench.get("method") == "empirical",
      f"-> ok={_bench.get('ok')} method={_bench.get('method')!r}")

# A failed run measured nothing, so it must not claim to have. This is the
# case that would otherwise let a caller relay "empirical" about an error.
_failed = _tools.benchmark("import sys\nraise SystemExit(1)", "python3", sizes="100,200")
check("a failed benchmark claims no method at all",
      _failed.get("ok") is False and "method" not in _failed,
      f"-> ok={_failed.get('ok')} method={_failed.get('method')!r}")

# ── the shipped skill (#88) ───────────────────────────────────────────────
# The tools exist to stop a model asserting numbers it did not compute. Nothing
# made a model REACH for them: no skill, no prompting guide, nothing in the
# tree. codecalc/SKILL.md is that, and it ships inside the package so it
# travels with the tools rather than living in a README nobody pastes.
_skill = pathlib.Path("codecalc/SKILL.md")
check("the skill ships inside the package", _skill.is_file(), f"-> {_skill}")
_txt = _skill.read_text(encoding="utf-8")

check("  ...with skill frontmatter a client can load",
      _txt.startswith("---") and "name: codecalc" in _txt and "description:" in _txt,
      f"-> {_txt[:40]!r}")

# The two halves have different costs and different strictness, and the file
# has to say so or it is just advice.
for _needle, _why in (
    ("mandatory, no exceptions", "the call-triggers are hard rules"),
    ("Do not call these", "the anti-triggers exist, so the skill is not noise"),
    ("Never drop a field you do not understand", "the relay rule is stated"),
):
    # Detail on BOTH outcomes: a "-> missing X" printed next to PASS reads as
    # a broken check, which is how the first version of this looked.
    check(f"the skill states {_why}", _needle in _txt,
          f"-> {'found' if _needle in _txt else 'MISSING'} {_needle!r}")

# Operand count was the WRONG threshold and the file must not reintroduce it:
# 0.1 + 0.2 is two operands and the canonical failure; 2 + 3 + 4 is three and
# never wrong. Type predicts error, length does not.
_typed = "Operand count is **not** the test" in _txt and "0.1 + 0.2" in _txt
check("  ...and keys on type rather than operand count", _typed,
      f"-> type-vs-length rule {'present' if _typed else 'MISSING'}")

# THE GATE ITSELF MUST EXIST. This is not belt-and-braces: the skill shipped
# once already claiming to be gated, while the gate had been destroyed by a
# `git checkout scripts/check_claims.py` at the end of a mutation test — which
# reverts to HEAD, not to the previous edit. check_claims.py passed, because a
# check that is absent raises nothing. Deleting the gate is invisible to the
# gate.
#
# So the assertion lives HERE, in the suite, pointed at the script. It is the
# "every scan counts its inputs first" rule from CONTRIBUTING applied to a scan
# that had no inputs at all.
#: Strip `#` comments so a floor cannot be satisfied by prose describing the
#: gate it guards. Deliberately simple: check_claims.py has no `#` inside a
#: string literal, and the assertion below is over identifiers, not sentences.
_re_comments = _re_mod.compile(r"#[^\n]*")

_claims_src = pathlib.Path("scripts/check_claims.py").read_text(encoding="utf-8")
check("check_claims.py actually gates the skill",
      "SKILL" in _claims_src and "names tools that server.py does not define" in _claims_src,
      f"-> SKILL mentions: {_claims_src.count('SKILL')}")
check("  ...in both directions",
      "relay fields no tool returns" in _claims_src,
      f"-> relay-field half {'present' if 'relay fields no tool returns' in _claims_src else 'MISSING'}")

# Same floor, same reason, for the SECURITY.md counts. Deleting that gate would
# not turn anything red on its own — check_claims.py would simply stop reading
# the file and keep exiting 0 — which is exactly how the skill gate above was
# lost once already.
#
# Matched against the source with COMMENTS STRIPPED, which is load-bearing. The
# first version of this floor tested `"SECURITY.md" in _claims_src`, and the
# gate it guards is introduced by a long comment block that says "SECURITY.md"
# three times. Deleting the executable checks and leaving that comment kept the
# floor green — verified, it passed with the gate gone. That is the same defect
# as the `killpg` marker in check_parity.py, which matched a comment explaining
# why killpg is NOT used: a check satisfied by prose about the check.
#
# The tokens below are variable names, which cannot plausibly appear in prose,
# and they are required in the stripped source rather than anywhere in the file.
#
# Identifier presence alone is NOT enough, which a second reviewer pointed out
# after the comment fix above: `_sec_allow = []` with the comparison deleted
# keeps both names and gates nothing. So the floor also requires fragments that
# exist only inside the fail() calls — a message cannot survive the removal of
# the branch that raises it, and comment-stripping does not touch string
# literals, so these prove the comparisons are still there.
_claims_code = _re_comments.sub("", _claims_src)
_sec_names = "_sec_allow" in _claims_code and "_sec_langs" in _claims_code
# Fragments chosen to sit INSIDE one string literal each. The first attempt
# spanned an implicit f-string concatenation ("...env vars; the " + "executor
# permits...") and so matched nothing, failing a correct tree — the same
# "watched it fail for the wrong reason" trap as the \b marker in
# check_parity.py earlier today.
_sec_compares = ("allowed env vars; the" in _claims_code
                 and "languages; the registry" in _claims_code)
check("check_claims.py actually gates SECURITY.md",
      _sec_names and _sec_compares,
      f"-> names={_sec_names} comparisons={_sec_compares}")

# doctor has to name it, or nobody installs it.
_doc = _sp2.run([_sys2.executable, "-m", "codecalc", "doctor"],
                capture_output=True, text=True, timeout=180)
check("doctor points at the skill file",
      "SKILL.md" in _doc.stdout and "(MISSING)" not in _doc.stdout,
      f"-> {[l for l in _doc.stdout.splitlines() if 'skill' in l.lower()]}")

print(f"\n=== {len(FAILS)} failures ===" if FAILS else "\n=== ALL NEW-FEATURE TESTS PASS ===")
sys.exit(1 if FAILS else 0)


asyncio.run(main())
