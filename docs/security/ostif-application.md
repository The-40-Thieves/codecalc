# Independent security audit — request for engagement (draft)

**Status: draft.** This is a prepared application for a coordinated,
independently-funded security audit — written for [OSTIF](https://ostif.org)
(which pairs open-source projects with a security firm and helps fund the work),
and equally usable as a scope document for a direct RFP to a firm
(Trail of Bits, NCC Group, Cure53, Doyensec, Atredis) or as the technical brief
for an [OpenSSF](https://openssf.org) engagement. **The owner completes the
contact and funding sections and submits it.** Everything above those is factual
and can be checked against the repository today.

Why this project asks for an *independent* review, in one sentence: codecalc is a
remote-code-execution service by design, and every audit it has had so far was
performed by its own author. `SECURITY.md` and `AUDIT.md` both say so in their own
words; this engagement is meant to close exactly that gap.

---

## 1. Project summary

| | |
|---|---|
| **Name** | codecalc |
| **What it is** | A Model Context Protocol (MCP) server that executes untrusted code in 31 languages, plus symbolic-math and SMT-logic tools, exposed to AI agents over stdio. Ships with a Rust sandbox executor. |
| **Repository** | https://github.com/The-40-Thieves/codecalc (public) |
| **Licence** | Apache-2.0 |
| **Languages** | Python (server + tool surface) and Rust (`executor/`, the sandbox binary). |
| **Size** | ~52,000 lines total: Python ~47k (of which the `codecalc/` package is ~21.5k; the rest is tests and gate scripts) and Rust ~4.4k (`executor/`). Measured, see §7. |
| **Maturity** | Published to PyPI, crates.io, and the MCP registry; latest release 0.4.0. Single maintainer (`MAINTAINERS.md`). |
| **Users at risk** | Anyone running an AI agent that can call these tools — the caller string reaching the executor and the expression parser is, by construction, adversary-controlled. |

## 2. Why an audit matters here

codecalc's function *is* running code it did not write. That makes it a different
risk class from most OSS libraries: a bug in the isolation boundary is not a
crash, it is a host compromise. It is increasingly wired into agent toolchains,
where the "user" supplying input is itself a language model that can be steered.
The isolation claims — sandbox escape, filesystem confinement, network denial,
resource ceilings, one session's isolation from another — are the crown jewels,
and they are exactly the claims a self-audit is least able to certify.

## 3. Threat model (summary; full version in `SECURITY.md`)

**In scope** — the boundary between executed code and the host:

- Escaping the sandbox to run outside the executor's declared constraints.
- Reading or writing host files outside the session workspace.
- Reaching the network when `no_net` was requested on a platform that claims to
  enforce it.
- Escaping the process, memory, output, or CPU ceilings.
- Reading environment variables outside the 23-entry allowlist.
- Corrupting one session's state or protocol stream from another.
- **Anything that makes the server report a guarantee it did not apply.** (The
  2026-08-21 audit found and fixed one of these — see §5.)

**Out of scope** (documented behaviour, not defects): code consuming resources up
to the declared ceilings; network access when `no_net` was not requested;
`install_package` reaching the network to fetch a package. `SECURITY.md` gives the
full list and the reasons.

**Design assumption under test:** codecalc is built for a *single operator running
it locally over stdio*, and does not claim multi-tenant or hosted-deployment
hardening. An auditor should test the single-operator model as stated — and is
explicitly invited to say where that assumption is thinner than the docs imply.

## 4. Requested scope — the six surfaces

An adversarial source-and-runtime review of, in rough priority order:

1. **The expression safety screen** (`codecalc/safe_expr.py`). A token denylist
   standing between a caller string and SymPy's `eval`-based `parse_expr`. Upstream
   SymPy calls the same approach "security theatre." **Question for auditors: can
   the denylist be bypassed to reach an evaluator?** A forked-child RLIMIT/wall-clock
   guard now backstops it — is that backstop itself escapable?
2. **The session jail** (`codecalc/sessions.py`). Path-traversal confinement,
   disk quotas, cleanup liveness (PID lockfile), and the `O_NOFOLLOW` read/write
   path. **Known residual to hand the auditor:** a parent-directory-symlink TOCTOU
   the server process (not Landlock-confined) may lose to executed code — the real
   fix is `openat2(RESOLVE_BENEATH)`, not yet applied. Confirm the exposure and
   whether the current mitigation is as narrow as documented.
3. **The executor sandbox** (`executor/`, `codecalc/executor.py`). rlimits, the
   environment allowlist, process-group kill, and the `no_net` shim. **Question:
   can executed code escape the resource ceilings or the process group?** The
   `no_net` shim is now *disclosed* as best-effort symbol interposition (see §5) —
   confirm the disclosure is complete and no other guarantee is over-reported.
4. **The package installer** (`codecalc/packages.py`). argv-only invocation,
   `--ignore-scripts` / `--only-binary` / `--no-scripts`, Landlock confinement, and
   quota pre/post-checks. **Question: can install-time code run, or the quota be
   evaded?**
5. **The HTTP transport** (the optional streamable-HTTP surface). DNS-rebinding
   guard, auth, request-size limits. **Question: SSRF, auth bypass, resource
   exhaustion?**
6. **Information leaks and the result contract.** Whether errors, audit output, or
   pydantic dumps leak host paths, secrets, or environment; whether the result
   schema can be made to misreport enforcement.

## 5. What has already been done (so the audit starts above the waterline)

- **`AUDIT.md`** — a full pre-publication audit by the author, two critical
  findings, both fixed before the repo was public. Explicitly *not* independent.
- **`docs/security/audit-2026-08-21.md`** — a second adversarial pass (an in-family
  Opus review plus fuzzing). It found **E-1**: on the Rust backend, `no_net` blocked
  `socket.socket()` but not a `ctypes`/`dlsym` or raw-syscall call, while reporting
  `unenforced: []` — a guarantee over-reported. Fixed: the result now discloses
  `no_net` as best-effort on the symbol-shim path. A cross-vendor pass was attempted
  and **blocked by the other vendor's own security content filter** — recorded, and
  part of why an independent human review is wanted.
- **Fuzzing** — `scripts/fuzz.py` (THE-899), a deterministic mutation fuzzer over
  the safe-expression screen and the session path guards, wired into CI as a smoke
  gate, with coverage-guided continuous fuzzing (ClusterFuzzLite) added on top. It
  has already surfaced two DoS-shaped findings, since fixed.
- **CI gates** — `check_no_eval`, `check_parity`, `check_claims`, `check_contract`,
  `check_portability`, `check_version`, `check_changelog`, plus permanent regression
  suites (`test_security.py`, `test_session_jail.py`, `test_platform_contract.py`)
  that make every un-disclaimed ceiling bite on both backends.

**Next step beyond this engagement:** application to upstream **OSS-Fuzz** for
continuous fuzzing on Google's infrastructure, once the ClusterFuzzLite harnesses
have proven out in-repo.

## 6. Concrete questions we want answered

1. Can executed code escape the rlimit/process-group sandbox on any supported
   platform (Linux, macOS, Windows Job Object / optional AppContainer)?
2. Can the `safe_expr.py` denylist be bypassed to reach SymPy's evaluator, and can
   the forked-child guard that backstops it be escaped or starved?
3. Can a session read or write outside its jail — in particular via the documented
   parent-directory TOCTOU residual?
4. Is the `no_net` best-effort disclosure now *complete*, or is there still a
   platform/backend path where egress is possible while the result implies it is
   blocked?
5. Do the Landlock (Linux) and AppContainer (Windows) confinements deliver what the
   docs claim on a real deployment, versus a CI runner that cannot exhibit them?
6. Does any output path leak host data (paths, env, secrets) that the redaction
   logic misses?

## 7. Build, run, and measure

```bash
git clone https://github.com/The-40-Thieves/codecalc && cd codecalc
uv sync                                   # Python side
cargo build --release --manifest-path executor/Cargo.toml   # Rust executor
uv run pytest                             # full suite
# gates: see CONTRIBUTING.md "Running the gates locally"
```

Source size (fill from the repo before submitting):

```bash
# Python
find codecalc scripts tests -name '*.py' | xargs wc -l | tail -1
# Rust
find executor/src -name '*.rs' | xargs wc -l | tail -1
```

## 8. For OSTIF / the firm (owner completes)

- **Primary contact:** `<name, email>`
- **Preferred disclosure window:** coordinated; codecalc uses GitHub private
  advisories (`SECURITY.md`).
- **Funding:** `<OSTIF-coordinated / self-funded / grant>` — OSTIF's model is to
  help source and fund the audit; state here what portion, if any, the project can
  contribute.
- **Timeline:** `<target window>`
- **Deliverable we will publish:** the report, added to `docs/security/` alongside
  the existing audits, with fixes tracked as issues and permanent regression tests
  (the project's standing practice — every audit finding becomes a gate).

---

*Bounty note: codecalc is a single-maintainer project and does not run a paid
bug-bounty. Valid reports are credited in the advisory and the changelog, and the
project is open to a hosted bounty via a platform such as [huntr](https://huntr.com)
if a sponsor steps forward. See `SECURITY.md` for reporting.*
