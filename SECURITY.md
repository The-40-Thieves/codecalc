# Security policy

## Reporting a vulnerability

**Use [private vulnerability reporting](https://github.com/The-40-Thieves/codecalc/security/advisories/new).** It is enabled on this repository. Do not open a public issue for a vulnerability.

Nine security-relevant reports arrived as public issues before this file existed, which is the reason it does now. Public issues are fine for ordinary bugs, and several of those reports were excellent, but a working sandbox escape should reach the maintainer before it reaches everyone else.

Expect an acknowledgement within a week. This is a single-maintainer project with no SLA behind it; that sentence is the honest version rather than a number nobody is on call to meet.

## What this software is

codecalc **runs untrusted code in 31 languages**. That is its function, not a side effect. Treat it as a remote code execution service, because it is one, and read the threat model below before deciding it is safe for your deployment.

`AUDIT.md` records a full audit performed before first publication, including two critical findings, both fixed before the repository was public. It is a dated report by the author, not an independent review. Weigh it accordingly.

## Threat model

**In scope.** The isolation boundary between executed code and the host:

- Escaping the sandbox to run code outside the executor's constraints
- Reading or writing host files outside the session workspace
- Reaching the network when `no_net` was requested on a platform where it is enforced
- Escaping the process, memory, output, or CPU ceilings the executor declares
- Reading environment variables outside the 23-entry allowlist
- Corrupting one session's state or protocol stream from another session
- Anything that makes the server report a guarantee it did not apply

**Explicitly out of scope**, because it is documented behaviour rather than a defect:

- Executed code consuming CPU, memory, or disk up to the declared ceilings
- Executed code reaching the network when `no_net` was not requested
- `install_package` reaching the network to fetch a package, and the metadata syscalls Landlock cannot restrict (`chmod`, `chown`, `stat`, `utime`, `fcntl`, `access`). Both are reported in `unenforced` rather than implied.
- UDP egress from a confined installer on kernels below Landlock ABI 10, DNS included. TCP is restrictable from ABI 4; UDP is not, and the result says so.
- The pure-Python fallback executor providing fewer guarantees than the Rust one. It reports what it could not enforce in the `unenforced` array of every result.

## Known limitations

Stated here rather than discovered later:

| Limitation | Effect |
|---|---|
| `install_package` runs a package manager | Install-time code is **not run** (`--ignore-scripts`, `--only-binary=:all:`, `--no-scripts`), and on Linux every manager is confined to its workspace with Landlock — nothing outside it is writable. What that does not cover is reported in `unenforced` ([#23](https://github.com/The-40-Thieves/codecalc/issues/23), [#90](https://github.com/The-40-Thieves/codecalc/issues/90)) |
| `update_runtimes` can update system packages | `apply=True` runs each manager's update command; the apt one is elevated. Gated on the host setting `CODECALC_ALLOW_RUNTIME_APPLY=1`, because `apply` is an argument a connected model controls ([#63](https://github.com/The-40-Thieves/codecalc/issues/63)) |
| Python fallback lacks the `no_net` shim | `no_net` is reported in `unenforced` rather than applied |
| Windows AppContainer isolation | The optional AppContainer strict backend (opt-in `CODECALC_WIN_APPCONTAINER=1`, **OFF by default**) adds a *security* boundary distinct from the Job Object's *resource* limits: a least-privilege profile with no network capability; the per-run workdir granted to that run's own AppContainer SID; the interpreter granted read to *ALL APPLICATION PACKAGES* — a **persistent, cached, read-only** ACE on a public interpreter, the deliberate trade-off for not re-walking thousands of files each run. It **fails closed**. Verified on a Windows 11 box (user-profile secrets unreadable, writes confined to the workdir, network denied, ambient privileges stripped), but every run still emits `appcontainer_isolation_unverified_on_windows` — a Server-SKU CI runner cannot exhibit it and the guarantee depends on the deployment (THE-829) |
| Same-UID execution by default | Isolation is rlimits and process groups, not a container or VM |
| No signed release artifacts yet | Verify what you build; there is nothing to verify against yet ([#24](https://github.com/The-40-Thieves/codecalc/issues/24)) |
| The expression tools sit on an `eval`-based parser | SymPy evaluates what it parses. `codecalc/safe_expr.py` screens every caller string before SymPy sees it, and that screen is a denylist — SymPy's own maintainers describe the same approach as insufficient on its own. See below |

### The expression tools and SymPy's `eval`

`evaluate_expression`, `simplify_expression`, `solve_linear` and the other symbolic tools reach SymPy's `parse_expr`/`sympify`. Those **evaluate what they parse**, and SymPy says so in its own docstring:

> .. warning:: Note that this function uses ``eval``, and thus shouldn't be used on unsanitized input.

That is the whole reason `codecalc/safe_expr.py` exists: every caller string is screened at the token level, before SymPy is handed it. What that screen refuses, and why each rule is there, is documented in the module itself.

**The screen is a denylist, and a denylist is not a sandbox.** This is not a hedge — it is upstream's own assessment of the same design. SymPy [PR #12524](https://github.com/sympy/sympy/pull/12524) added a `safe=` flag to `sympify()` built on an AST whitelist plus a name blacklist. It was **never merged** and has been abandoned since 2020. On it:

> "I am sure that someone malicious would be able to circumvent what we have here so I would still describe this as unsafe rather than 'mostly safe'." — *oscarbenjamin*

> "security theater that leads users into a false sense of security, because it can still be bypassed" — *asmeurer*, the PR's own author

[Issue #10805](https://github.com/sympy/sympy/issues/10805) (*"sympify shouldn't use eval"*) is still open, and the fix upstream advocates is a complete direct evaluator rather than any form of screening.

Three consequences worth stating plainly:

- **There is no `safe=` flag to reach for.** Verified against the pinned version: sympy 1.14.0's `sympify` takes `a, locals, convert_xor, strict, rational, evaluate` and nothing else. Code written against that PR gets a `TypeError`.
- **Order is load-bearing.** The screen must run before *any* parse, including the `evaluate=False` shape inspection that bounds expression cost — `evaluate=False` suppresses arithmetic, not `eval`. `tests/test_security.py` asserts that a rejected string never reaches `parse_expr` at all, rather than leaving it to the order the lines happen to appear in.
- **Cost is bounded separately from reach.** A screen that stops `__import__` does nothing about `9**9**9**9`, which is ten characters. Those bounds are in `safe_expr.py` too, with their measured thresholds, and are a distinct mechanism from the safety screen ([#67](https://github.com/The-40-Thieves/codecalc/issues/67)).

**The bound no longer depends on the screen being complete.** Every symbolic tool — `evaluate_expression`, `solve_linear`, `simplify_expression`, `solve_expression`, `limit_expression`, `algebraic_equiv` — runs its work in a forked child under `RLIMIT_CPU` and `RLIMIT_AS`, with a wall clock enforced by the parent and `SIGKILL` on expiry ([#78](https://github.com/The-40-Thieves/codecalc/issues/78)). A denylist has to anticipate; a bound does not. Demonstrated on expressions the screen has no opinion about — a 62-digit semiprime through `factorint`, and `nextprime(10**2000)` — both of which run past 25 seconds unguarded and are killed with a structured error naming the limit.

This costs 9–13 ms per call, measured per tool ([#84](https://github.com/The-40-Thieves/codecalc/issues/84)). It does not apply where there is no `fork`: on Windows the call runs in-process and the result says so in `unenforced`, in the executor's own vocabulary.

If you are exposing these tools to input you do not control, the honest reading is still the one upstream gives: treat an expression string as reaching an evaluator. The difference is that the evaluator now runs somewhere it can be stopped.

codecalc is built for a **single operator running it locally over stdio**. It has not been hardened for multi-user or hosted deployment, and the audit says so in its own verdict. If you intend to expose it to input you do not control, put it inside a container or microVM boundary of your own.

## Supported versions

No release has been tagged. The supported version is `main`. Fixes land there and nowhere else.

## Verifying a release artifact

Every release carries three things beyond the wheels and archives themselves.

**`SHA256SUMS`** — basenames, not paths, so `sha256sum -c` works wherever you put
the files. The release job generates it and then verifies it in the same job,
because "it wrote a file that looks like checksums" is not the claim.

```bash
sha256sum -c SHA256SUMS
```

**`codecalc-sbom.cyclonedx.json`** — a CycloneDX SBOM of the **dependency
closure**, covering PyPI packages, Cargo crates and the GitHub Actions pinned in
the workflows. It is scoped to the source tree rather than to the wheel on
purpose: the wheel vendors no dependencies, so a wheel-scoped scan returns an
empty document, and an empty file with a bill-of-materials name is worse than no
file. The generating step asserts a component from each ecosystem is present and
fails if the scan reached only one of them. It is listed in `SHA256SUMS` like
everything else.

**PEP 740 attestations** on the PyPI wheels — generated automatically by
`pypa/gh-action-pypi-publish` (pinned at v1.14.2, above the 1.11.0 floor) as part
of Trusted Publishing. These are Sigstore-backed build provenance tied to the
workflow identity, with no long-lived key that could be lost or stolen. `pip`
verifies them where supported; PyPI shows them on the file listing.

There is no PGP key, and that is deliberate rather than an omission: a signing
key held by one maintainer is a worse story than provenance bound to the
workflow that actually built the artifact.

## What is gated

These invariants are enforced in CI on every pull request, so a regression fails rather than ships:

- `check_no_eval.py` — `eval` nowhere, `exec` in exactly one documented file
- `check_parity.py` — the env allowlist, runtime path, and language registry cannot drift between the Rust and Python backends
- `check_claims.py` — the counts and licence declarations in README, AUDIT.md, and the repository description match the code
- `check_portability.py` — no machine-specific paths or private addresses, including in tests
- `check_contract.py` — the published result schema matches the code that produces it, both backends' verdict vocabularies agree with it, and every Rust envelope return carries every envelope field
- `check_version.py` — one version across `pyproject.toml`, `executor/Cargo.toml`, `CHANGELOG.md`, and the git tag when running on one
- `tests/test_security.py`, `tests/test_session_jail.py` — the audit's findings as permanent regression tests
- `tests/test_platform_contract.py` — every ceiling NOT named in `unenforced` is made to bite, on **both** backends. The Rust one runs in the sandbox job, which asserts `backend == "rust"`; the pure-Python fallback runs in the test matrix, which asserts `backend == "python"`. Neither can be tested by accident ([#66](https://github.com/The-40-Thieves/codecalc/issues/66))
