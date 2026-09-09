# Security policy

## Reporting a vulnerability

**Use [private vulnerability reporting](https://github.com/The-40-Thieves/codecalc/security/advisories/new).** It is enabled on this repository. Do not open a public issue for a vulnerability.

Nine security-relevant reports arrived as public issues before this file existed, which is the reason it does now. Public issues are fine for ordinary bugs, and several of those reports were excellent, but a working sandbox escape should reach the maintainer before it reaches everyone else.

Expect an acknowledgement within a week. This is a single-maintainer project with no SLA behind it; that sentence is the honest version rather than a number nobody is on call to meet.

## Security researchers welcome

codecalc is a sandbox whose entire job is running untrusted code — which makes it precisely the kind of target worth attacking. If you can break the isolation boundary, the maintainer would rather hear it from you than read about it later, and will credit you for it.

**Safe harbor.** Good-faith security research on codecalc — conducted within the scope below, against an instance you run yourself, without harming anyone else's data or availability — is authorized. The maintainer will not pursue or support legal action over research that follows this policy, and will work with you on coordinated disclosure. If you are unsure whether something is in scope, ask first through the private advisory above.

**Scope is the threat model below.** A sandbox escape, a host-file read or write outside the session workspace, egress past an enforced `no_net`, a cross-session leak, or anything that makes the server report a guarantee it did not apply — all in scope, all wanted. Executed code consuming resources up to the declared ceilings, or reaching the network when `no_net` was not requested, is documented behaviour rather than a finding (see "Explicitly out of scope").

**Ground rules.** Report privately (the advisory link above — never a public issue for a real vulnerability); test against your own instance, not someone else's deployment; do not run denial-of-service against shared infrastructure; and give the maintainer a reasonable window before any public disclosure.

**Recognition.** There is no paid bounty: this is a single-maintainer project, and a bounty would be a promise nobody is funded to keep. What there is — credit in the security advisory, a line in the CHANGELOG, and a place in the Hall of fame at the end of this file, unless you would rather stay anonymous. If a sponsor ever backs a hosted bounty (for example via [huntr](https://huntr.com)), it will be announced here.

## What this software is

codecalc **runs untrusted code in 31 languages**. That is its function, not a side effect. Treat it as a remote code execution service, because it is one, and read the threat model below before deciding it is safe for your deployment.

`AUDIT.md` records a full audit performed before first publication, including two critical findings, both fixed before the repository was public. It is a dated report by the author, not an independent review. Weigh it accordingly.

A second adversarial pass (`docs/security/audit-2026-08-21.md`) found one over-reported guarantee — `no_net` reporting itself enforced when a `ctypes`/raw-syscall call could still reach the network, since fixed — and is likewise an in-family review plus fuzzing, not a third party (a cross-vendor attempt was refused by the other vendor's own security content filter). **No independent third-party review has been done yet.** One is prepared (`docs/security/ostif-application.md`), and until a second maintainer exists, security-critical changes carry a mandatory adversarial review under the policy in `MAINTAINERS.md`. That is the honest state: audited by its author, twice, with outside review openly sought rather than claimed.

An application requesting a coordinated, independently-funded third-party review (`docs/security/ostif-application.md`) was submitted to OSTIF through its intake form on 2026-09-09; the next step is OSTIF's coordination call. An OpenSSF Scorecard run the same day scored the repository 4.7/10; the actionable zero-score checks are tracked and being closed.

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
- `execute_code` / `session_run` / `execute_code_stream` / `run_submit` with declared `dependencies` (a PEP 723 block or the `dependencies` argument) reaching the network to install them: yes, before the sandboxed step (before the first progress notification, for `execute_code_stream`; on `run_submit`'s own background worker, before the code, with the run's own record — not the submit call's stack — owning the workdir until it is released), through the same confined `install_package` path — refused (`capability_not_requested`, no fetch attempted) when `no_net=True` was requested or the capability policy denies or strictly limits network. **The PEP 723 block alone is enough to trigger this** — a caller who passes no `dependencies` argument at all still gets a confined install and real egress if the submitted source happens to carry a `# /// script` block, wherever the refusal above does not apply. This is logged as a distinct audit event (`dependency_install_implicit`), separate from an explicit `install_package`/`dependencies=` call, precisely because the source alone — not an operator argument — is what started it. Disable it the same way: `no_net=True`, or a `deny-network`/`strict` `CODECALC_CAPABILITY_POLICY`. A dependency-bearing run is also bounded by a fixed, separate install-time budget (`codecalc.dependencies.DEFAULT_DEPENDENCY_INSTALL_BUDGET_SECONDS`, 120s aggregate, independent of the run's own `timeout`) and, for a sessionless run, a disk quota on its dependency workdir (`CODECALC_SESSION_DISK_QUOTA_MB`, the same cap a session workspace already has) — both refuse with a stamped, coded error naming the ceiling that fired. `compare_execution` is the one holdout: it fans out across several languages with no per-language install plumbing behind it, so it REJECTS an explicit `dependencies` argument with a `validation` error rather than approximating one, and DISCLOSES (never silently drops) an inline PEP 723 block it finds in a snippet instead of installing from it.
- UDP egress from a confined installer on kernels below Landlock ABI 10, DNS included. TCP is restrictable from ABI 4; UDP is not, and the result says so.
- The pure-Python fallback executor providing fewer guarantees than the Rust one. It reports what it could not enforce in the `unenforced` array of every result.

## Known limitations

Stated here rather than discovered later:

| Limitation | Effect |
|---|---|
| `install_package` runs a package manager | Install-time code is **not run** (`--ignore-scripts`, `--only-binary=:all:`, `--no-scripts`), and on Linux every manager is confined to its workspace with Landlock — nothing outside it is writable. What that does not cover is reported in `unenforced` ([#23](https://github.com/The-40-Thieves/codecalc/issues/23), [#90](https://github.com/The-40-Thieves/codecalc/issues/90)) |
| `update_runtimes` can update system packages | `apply=True` runs each manager's update command; the apt one is elevated. Gated on the host setting `CODECALC_ALLOW_RUNTIME_APPLY=1`, because `apply` is an argument a connected model controls ([#63](https://github.com/The-40-Thieves/codecalc/issues/63)) |
| Python fallback lacks the `no_net` shim | `no_net` is reported in `unenforced` rather than applied |
| `no_net` on the native executor | **Linux: enforced in-kernel by a seccomp-bpf filter** — it refuses every syscall-level way unprivileged code can open an `AF_INET`/`AF_INET6` socket: `socket()` by domain, `io_uring` (which can open/connect a socket inside the kernel without a `socket()`/`connect()` syscall), and the x32/foreign-arch compat range. So a `ctypes`/`dlsym` or raw-syscall network call cannot bypass it the way it walks around a symbol shim; `AF_UNIX` and other domains are left alone. A kernel with **no** seccomp falls back to the shim below and discloses it; a kernel with seccomp but not filter mode **fails the run closed** rather than downgrading silently. **macOS: best-effort** — `DYLD_INSERT_LIBRARIES` symbol interposition, which a dynamically-linked `ctypes`/`dlsym` or raw syscall bypasses, and which a SIP-protected/hardened interpreter drops entirely; disclosed in `unenforced`. The strict (gVisor) backend is the stronger tier — a full network namespace block for the whole container. (Disclosure E-1; kernel enforcement added since; `docs/security/audit-2026-08-21.md`) |
| Windows AppContainer isolation | The optional AppContainer strict backend (opt-in `CODECALC_WIN_APPCONTAINER=1`, **OFF by default**) adds a *security* boundary distinct from the Job Object's *resource* limits: a least-privilege profile with no network capability; the per-run workdir granted to that run's own AppContainer SID; the interpreter granted read to *ALL APPLICATION PACKAGES* — a **persistent, cached, read-only** ACE on a public interpreter, the deliberate trade-off for not re-walking thousands of files each run. It **fails closed**. Verified on a Windows 11 box (user-profile secrets unreadable, writes confined to the workdir, network denied, ambient privileges stripped), but every run still emits `appcontainer_isolation_unverified_on_windows` — a Server-SKU CI runner cannot exhibit it and the guarantee depends on the deployment |
| Same-UID execution by default | Isolation is rlimits and process groups, not a container or VM. **Note, 2026-09-07:** this is not merely codecalc being conservative — the stronger options have real ceilings of their own. gVisor "requires Linux" (https://gvisor.dev/docs/user_guide/faq/) and Linux 5.6+ (gVisor install docs), so it is unavailable as a same-machine option on macOS/Windows without a VM underneath. Even where a container runtime *is* available, Docker's own security page says its default capabilities "may provide incomplete isolation, either independently, or when used in combination with kernel vulnerabilities" (https://docs.docker.com/engine/security/), and Kubernetes' docs describe containers as offering "a weaker isolation boundary than virtual machines" for multi-tenancy (https://kubernetes.io/docs/concepts/security/multi-tenancy/). Podman on macOS/Windows also "requires a virtual machine" (https://docs.podman.io/en/latest/markdown/podman-machine.1.html). None of that is an argument against reaching for a container/VM boundary when you need one (§"Deploying a strict execution backend" is exactly that path on Linux) — it is why the same-UID default is honestly disclosed rather than silently assumed adequate, and why the strict backend is opt-in and explicit rather than a checkbox that quietly means less than it says |
| Fail-closed rather than fail-open on a missing native sandbox | `CODECALC_REQUIRE_NATIVE` refuses to start rather than silently answering every call on the weaker pure-Python fallback (see the README's configuration table). The peer precedent for refusing rather than silently downgrading is Codex CLI, which "refuses to run the command instead of silently running it unsandboxed" when its own sandbox is unavailable (https://learn.chatgpt.com/docs/permissions, retrieved 2026-09-07) |
| Release artifacts are signed (from v0.2.0) | Every released file carries a keyless sigstore **build-provenance attestation** — verify with `gh attestation verify <file> --repo The-40-Thieves/codecalc`; PyPI wheels additionally carry PEP 740 attestations from trusted-publishing upload ([#24](https://github.com/The-40-Thieves/codecalc/issues/24)) |
| The expression tools sit on an `eval`-based parser | SymPy evaluates what it parses. `codecalc/safe_expr.py` screens every caller string before SymPy sees it, and that screen is a denylist — SymPy's own maintainers describe the same approach as insufficient on its own. See below |

### The expression tools and SymPy's `eval`

`evaluate_expression`, `simplify_expression`, `solve_linear` and the other symbolic tools reach SymPy's `parse_expr`/`sympify`. Those **evaluate what they parse**, and SymPy says so in its own docstring:

> .. warning:: Note that this function uses ``eval``, and thus shouldn't be used on unsanitized input.

That is the whole reason `codecalc/safe_expr.py` exists: every caller string is screened at the token level, before SymPy is handed it. What that screen refuses, and why each rule is there, is documented in the module itself.

**The screen is a denylist, and a denylist is not a sandbox.** This is not a hedge — it is upstream's own assessment of the same design. SymPy [PR #12524](https://github.com/sympy/sympy/pull/12524) added a `safe=` flag to `sympify()` built on an AST whitelist plus a name blacklist. It was **never merged**; checked 2026-09-07, it is still open, with last activity 2022-10-26. On it:

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

A stateless Streamable HTTP transport (`serve-http`) also exists, for the same single-operator threat model over a different wire protocol. It binds to loopback by default, and refuses to bind a non-loopback address unless `CODECALC_HTTP_TOKEN` is set (constant-time bearer check) — see the README's "Run the server" and configuration sections. That token gates *requests*; it does not change the isolation model above, and does not make codecalc suitable for multi-tenant exposure.

`serve-http --oauth-issuer` is an alternative to that static token, off by default: bearer tokens are instead verified as JWTs (RS256/ES256, signature checked against the issuer's own JWKS) with issuer, audience, expiry and not-before all checked, and the server publishes RFC 9728 Protected Resource Metadata so a client can discover which issuer and scopes it expects. The issuer and JWKS URLs must be `https://` unless the host is loopback, and the OAuth verifier is built ONLY inside `serve-http`'s own startup path — never at module import — so setting `CODECALC_OAUTH_ISSUER` costs `doctor`, `--help`, `serve-strict`, or the bare stdio server no network call at all; only `serve-http` itself resolves the issuer, and fails closed with a stderr message and a non-zero exit if it cannot be reached, rather than starting a server no token could ever pass. It is still the resource-server half only — codecalc never runs an `/authorize` or `/token` endpoint of its own, so there is no dynamic-client-registration surface to secure here, and the RFC 9207 `iss` check on an authorization *response* and Client ID Metadata Documents (both from the 2026-07-28 MCP auth hardening) are the connecting *client*'s responsibility, not this server's. If both the static token and an issuer end up configured, the issuer wins outright: a request bearing the static token's exact value is rejected the same as any other invalid bearer value, and a startup warning names both settings so the weaker credential does not go on working unnoticed. Token values are never written to the audit log, stdout, or stderr on any code path, success or failure.

## Supported versions

Published as **`codecalc` 0.11.0** on PyPI and the **`codecalc-exec` 0.11.0** executor on crates.io
(README, "Published install"). The supported versions are the latest tagged release plus `main`;
fixes land on `main` and are picked up by the next release.

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

## Hall of fame

Security researchers who have responsibly disclosed issues in codecalc — reported privately, fixed, and credited here with their permission. Findings become permanent regression tests, so a name here also marks a hole that stays closed.

- _Be the first. See "Security researchers welcome" above._
