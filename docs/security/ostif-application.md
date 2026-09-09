# Independent security audit — request for engagement (draft)

**Status: submitted 2026-09-09 via OSTIF's intake form ("Reach out to OSTIF!"); awaiting the coordination call.** OpenSSF Scorecard at submission: 4.7/10 (v5 CLI; zero-scoring checks and their follow-ups are tracked in the project tracker). This is a prepared application for a
coordinated, independently-funded security audit — written for
[OSTIF](https://ostif.org) (which pairs open-source projects with a security
firm and helps fund the work), and equally usable as a scope document for a
direct RFP to a firm (Trail of Bits, NCC Group, Cure53, Doyensec, Atredis) or
as the technical brief for an [OpenSSF](https://openssf.org) engagement.
**The owner completes the contact and funding sections and submits it** —
see the submission checklist at the end. Everything above that section is
factual and can be checked against the repository today.

Why this project asks for an *independent* review, in one sentence: codecalc is
a remote-code-execution service by design, and every audit it has had so far
was performed by its own author or an in-family model. `SECURITY.md` and
`AUDIT.md` both say so in their own words; this engagement is meant to close
exactly that gap.

---

## 1. Project summary

| | |
|---|---|
| **Name** | codecalc |
| **What it is** | A Model Context Protocol (MCP) server that executes untrusted code in 31 languages, plus symbolic-math and SMT-logic tools, exposed to AI agents over stdio (and, optionally, stateless Streamable HTTP). Ships with a Rust sandbox executor. |
| **Repository** | https://github.com/The-40-Thieves/codecalc (public) |
| **Licence** | Apache-2.0 |
| **Languages** | Python (server + tool surface) and Rust (`executor/`, the sandbox binary). |
| **Size** | ~81,000 lines total: Python ~75.7k (of which the `codecalc/` package is ~33.4k; the rest is tests and gate scripts) and Rust ~5.3k (`executor/src/`). Measured, see §8. |
| **Tool surface** | **57 MCP tools**, **31 execution languages** (`README.md`, gated live against the code by `scripts/check_claims.py` so this table cannot go stale the way the numbers below once did). |
| **Maturity** | Published to PyPI, crates.io, and the MCP registry; latest release **0.11.0** (`pyproject.toml`, `executor/Cargo.toml`). Every release since 0.2.0 carries a keyless sigstore build-provenance attestation and, on PyPI, PEP 740 attestations from Trusted Publishing, plus a CycloneDX SBOM of the dependency closure — see §5. Single maintainer (`MAINTAINERS.md`). |
| **Users at risk** | Anyone running an AI agent that can call these tools — the caller string reaching the executor and the expression parser is, by construction, adversary-controlled, and (§3) may itself be attacker-steered rather than operator-typed. |

## 2. Why an audit matters here

codecalc's function *is* running code it did not write. That makes it a different
risk class from most OSS libraries: a bug in the isolation boundary is not a
crash, it is a host compromise. It is increasingly wired into agent toolchains,
where the "user" supplying input is itself a language model that can be steered
by content the model reads, not just by the operator's own prompt (§3). The
isolation claims — sandbox escape, filesystem confinement, network denial,
resource ceilings, one session's isolation from another — are the crown jewels,
and they are exactly the claims a self-audit is least able to certify.

## 3. Threat model

**In scope** — the boundary between executed code and the host:

- Escaping the sandbox to run outside the executor's declared constraints.
- Reading or writing host files outside the session workspace.
- Reaching the network when `no_net` was requested on a platform that claims to
  enforce it.
- Escaping the process, memory, output, or CPU ceilings.
- Reading environment variables outside the 23-entry allowlist.
- Corrupting one session's state or protocol stream from another.
- **Anything that makes the server report a guarantee it did not apply.** (The
  2026-08-21 adversarial pass found and fixed one of these — see §5.)

**Out of scope** (documented behaviour, not defects): code consuming resources
up to the declared ceilings; network access when `no_net` was not requested;
`install_package` (and a per-run `dependencies` install, see §4) reaching the
network to fetch a package. `SECURITY.md` gives the full list and the reasons.

**Design assumption under test:** codecalc is built for a *single operator
running it locally over stdio*, and does not claim multi-tenant or
hosted-deployment hardening. An auditor should test the single-operator model
as stated — and is explicitly invited to say where that assumption is thinner
than the docs imply.

### Named incident classes, and where codecalc sits against each

This project's own two self-audits were written by finding what breaks under
adversarial testing of *this* code. The classes below are named instead from
the outside — documented 2026 incidents and a fresh academic taxonomy against
code-execution MCP servers generally — so the auditor starts from "here is
what has actually gone wrong in this category of software," not only from what
this repository already suspects about itself. For each, what codecalc claims
to mitigate and how, cited to the file that does it, and — where the mitigation
is partial — the honest gap rather than a claim the code cannot back up:

1. **Filesystem-MCP symlink escape.** The reference Anthropic Filesystem MCP
   Server's "EscapeRoute" vulnerabilities (CVE-2025-53109, CVE-2025-53110) let
   a crafted symlink walk a supposedly-confined path outside its sandbox root.
   codecalc's session workspace (`codecalc/sessions.py`) is the same shape of
   surface — a caller-supplied relative path resolved against a jail root —
   and defends with a component-wise `is_relative_to` boundary check plus
   `O_NOFOLLOW` on the final path component for both the read (`_read_nofollow`)
   and write (`_write_nofollow`) paths, refusing rather than following a
   symlink there. **The gap is already named, not hidden:** the final-component
   `O_NOFOLLOW` does not close a parent-directory TOCTOU — a race between
   `_jail`'s `resolve()` and this function's `open()` that plants a symlink in
   a *parent* segment, which the server process (not Landlock-confined) can
   lose. `docs/security/audit-2026-08-21.md` (finding N-2) and `SECURITY.md`
   both record this as accepted pending `openat2(RESOLVE_BENEATH)` (Linux
   5.6+, no portable equivalent) — precisely the residual an OSTIF-sourced
   audit is asked to confirm or defeat (§6, Q3).
2. **Prompt-injection exfiltration through tool results.** The GitHub MCP
   Server disclosure (Invariant Labs, 2025) showed an attacker planting
   instructions in content an agent later reads (a public issue) to make the
   agent misuse an unrelated tool call and leak private data back through the
   tool's own result. codecalc's threat model already treats the caller string
   as adversary-controlled *by construction* (§1) for exactly this reason — an
   agent's tool arguments can be shaped by content it read, not only by the
   operator. What codecalc mitigates: the 23-entry environment allowlist
   bounds what a running program can read out of its own process environment
   (`codecalc/executor.py`); the 64 KiB per-stream output cap plus the
   `OLE`/truncation-with-byte-count contract (README, "Sandbox guarantees")
   bounds how much can ride out through the result itself; and
   `docs/security/audit-2026-08-21.md`'s "Info leaks" surface review found no
   redaction gap in error paths or the audit log. **What it does not mitigate:**
   once code is running, if `no_net` was not requested, network egress is
   documented, in-scope, ordinary behaviour (`SECURITY.md`, "Explicitly out of
   scope") — an injected instruction that gets an agent to call `execute_code`
   without `no_net=True` can have that code read whatever the session
   workspace and env allowlist expose and send it out, exactly as any code the
   operator ran themselves could. codecalc's defense here is disclosure and an
   opt-in denial (`no_net`, or `CODECALC_CAPABILITY_POLICY=deny-network`/`strict`
   via `codecalc/capabilities.py`), not a default block — worth an auditor's
   opinion on whether that default is the right one for an agent-facing tool.
3. **Trojanized registry packages.** The `postmark-mcp` npm package (Koi
   Security, September 2025) shipped a working MCP server with a silently
   added BCC backdoor — malicious code arriving through the *dependency*, not
   the tool's own logic. codecalc's `install_package`
   (`codecalc/packages.py`) is the surface that fetches third-party registry
   content on a caller's behalf, and layers two mitigations: **Layer 1** —
   install-time code never runs (`--ignore-scripts`, `--only-binary=:all:`,
   `--no-scripts`, argv-only invocation, never a shell); **Layer 2** — the
   installer *binary itself* is confined to its workspace by Landlock on
   Linux and a generated Seatbelt profile on macOS (PR #130, PR #173-era
   work; extended cross-platform through 0.2.0), so a compromised package
   manager cannot read or write outside it even if Layer 1 is defeated.
   **What neither layer touches:** the package's own runtime code, once
   legitimately fetched and then imported by the caller's own script, runs
   under the same rlimit/`no_net`/env-allowlist sandbox as any other executed
   code — same as `postmark-mcp`'s payload would have, had it been a genuine
   dependency rather than the tool itself. The newest instance of this same
   attack surface is the **per-run dependency install** added in PR #265: a
   PEP 723 block or a `dependencies` argument on `execute_code` /
   `session_run` / `execute_code_stream` / `run_submit` triggers this same
   confined install path *implicitly*, from source text alone, before the
   caller's code runs (`SECURITY.md`, "Explicitly out of scope"; logged as
   `dependency_install_implicit` in the audit trail specifically so an
   operator can tell it apart from an explicit call). It is the freshest
   surface in this class and the least battle-tested — see §4.
4. **Command injection / path traversal — the largest MCP CVE class.**
   Aggregated MCP vulnerability tracking (the "MCP Security Incident Ledger,"
   Digital Applied, and Cycode's OWASP MCP Top 10, both 2026) both name
   command injection and path traversal as the dominant vulnerability
   categories across published MCP-server CVEs — the classic
   "concatenate caller input into a shell command or a filesystem path"
   defect, now reproduced at scale by AI-authored MCP servers. codecalc has no
   `shell=True` (or equivalent) anywhere in the tree — verified: every process
   invocation is argv-only, gated indirectly by the same discipline
   `scripts/check_no_eval.py` enforces for `eval`/`exec` — and every
   caller-supplied path used by the session jail, `install_package`, and the
   executor's workdir handling goes through the resolve-then-verify jail
   checks in `codecalc/sessions.py` rather than string concatenation. This is
   an area where "we found nothing" from two in-family audits is the weakest
   possible form of assurance, precisely because it is the class attackers
   find most often elsewhere — a first question for the auditor (§6, Q1/Q3).
5. **The CE-MCP taxonomy (arXiv:2602.15945, Felendler et al., Feb 2026).**
   This paper formalizes "Code Execution MCP" (an agent submits one program
   instead of many discrete tool calls — exactly codecalc's shape) as a
   *distinct* risk category from tool-orchestration MCP, and applies the
   MAESTRO framework to name sixteen attack classes across five execution
   phases, including two specific to code-execution servers: **exception-mediated
   code injection** (an error/exception path that re-interprets
   caller-controlled text as code) and **unsafe capability synthesis** (a
   program assembling, at runtime, a capability request the static tool
   surface never declared). It proposes two defenses: containerized
   sandboxing and semantic gating. codecalc already ships **both**, partially:
   containerized sandboxing is the opt-in strict backend (gVisor+Docker on
   Linux, AppContainer on Windows — §4), off by default; semantic gating is
   the capability broker (`codecalc/capabilities.py`) whose one invariant is
   that approved capabilities can never exceed requested ones, also
   off by default until `CODECALC_CAPABILITY_POLICY` is set. On
   exception-mediated code injection specifically: `codecalc/safe_expr.py`'s
   own module docstring states order is load-bearing — the safety screen must
   run before *any* parse, and `tests/test_security.py` asserts a rejected
   string never reaches `parse_expr` even via the `evaluate=False` shape-cost
   path — but that discipline has not been checked against this paper's own
   taxonomy by anyone outside the project. Worth handing the auditor
   directly: does codecalc's error-handling anywhere fall into this class, and
   does either off-by-default defense need to be on-by-default for an
   agent-facing deployment?

## 4. Requested scope — prioritized surfaces

An adversarial source-and-runtime review of, in priority order:

1. **The Rust executor sandbox** (`executor/`, `codecalc/executor.py`) —
   rlimits, the 23-entry environment allowlist, process-group kill, and the
   per-platform `no_net` mechanism: a seccomp-bpf filter refusing
   `socket(AF_INET/AF_INET6)` in-kernel where the Linux kernel supports it
   (PR #242, shipped 0.5.0), a `DYLD_INSERT_LIBRARIES`/`LD_PRELOAD` symbol
   shim as the macOS and no-seccomp-Linux fallback (disclosed, bypassable via
   `ctypes`/`dlsym`/raw syscalls), Landlock confinement for `install_package`
   on Linux, a Windows Job Object for resource limits, and an opt-in Windows
   AppContainer security boundary (`CODECALC_WIN_APPCONTAINER=1`, off by
   default, fails closed, verified once on a real Windows 11 box but every
   run still discloses `appcontainer_isolation_unverified_on_windows`).
   **Question: can executed code escape the resource ceilings, the process
   group, or any of these platform-specific boundaries?**
2. **The pure-Python fallback executor's weaker guarantees**
   (`codecalc/executor.py`, non-native path). When the Rust binary is
   missing, `no_net` is reported in `unenforced` rather than applied,
   `peak_memory_kb` comes back `None` (no per-run attribution), and the
   `CODECALC_REQUIRE_NATIVE=1` refuse-to-start knob is the only thing
   standing between an operator and silently running every call on this
   weaker path. **Question: is anything claimed as enforced on the fallback
   that is not — the same class of over-report §3/§5's E-1 finding closed on
   the Rust backend — and is the disclosure honest everywhere the fallback is
   reachable (stateful session workers included, where several ceilings are
   already disclosed `unenforced` by design)?**
3. **The session jail, sessions, and artifact paths** (`codecalc/sessions.py`).
   Path-traversal confinement, the `O_NOFOLLOW` read/write path, disk quotas,
   cleanup liveness (the per-worker-session PID lockfile, not directory
   mtime), and the `codecalc://session/{id}/files/...` artifact-URI surface
   that oversized session output and inline artifacts (PR #263) spill into.
   **Known residual to hand the auditor directly:** the parent-directory
   symlink TOCTOU named in §3(1) above — confirm the exposure and whether the
   current mitigation is as narrow as documented.
4. **Per-run dependency install** (`codecalc/packages.py`,
   `codecalc/dependencies.py`, PR #265). The newest and least-audited surface:
   an implicit, PEP-723-triggered `install_package` call ahead of the
   sandboxed step, its own 120-second aggregate budget
   (`DEFAULT_DEPENDENCY_INSTALL_BUDGET_SECONDS`) independent of the run's own
   timeout, and a disk quota on the sessionless install workdir. **Question:
   can the install-time budget or quota be evaded, and does the "before the
   sandbox, never inside it" boundary actually hold on every entry point
   (`execute_code`, `session_run`, `execute_code_stream`, `run_submit`'s
   background worker)?**
5. **The HTTP transport** (`serve-http`, `codecalc/server.py`,
   `codecalc/mcp_middleware.py`). Loopback-by-default binding, mandatory
   constant-time bearer-token auth (`CODECALC_HTTP_TOKEN`) for any
   non-loopback bind, and the request/response size guard. **Question: SSRF,
   auth bypass (constant-time comparison actually constant-time in practice,
   token handling in logs/errors), DNS-rebinding, or resource exhaustion?**
6. **The expression safety screen** (`codecalc/safe_expr.py`). A token
   denylist standing between a caller string and SymPy's `eval`-based
   `parse_expr` — upstream SymPy's own maintainers call the same approach
   "security theatre" (`SECURITY.md` quotes the PR discussion directly).
   **Question: can the denylist be bypassed to reach an evaluator?** The
   forked-child RLIMIT/wall-clock guard that backstops it on POSIX (not
   applied on Windows, where the call runs in-process) — is that backstop
   itself escapable or starvable?
7. **Information leaks and the result contract.** Whether errors, audit
   output, or pydantic dumps leak host paths, secrets, or environment; whether
   the result schema (`docs/contract/result-v1.schema.json`) can be made to
   misreport enforcement — the specific class §3's in-scope list calls out
   as "anything that makes the server report a guarantee it did not apply."

## 5. What has already been done (so the audit starts above the waterline)

- **`AUDIT.md`** — a full pre-publication audit by the author, two critical
  findings, both fixed before the repo was public, with corrections tracked
  inline as the document itself has grown across every subsequent protocol
  and transport change. Explicitly *not* independent.
- **`docs/security/audit-2026-08-21.md`** — a second adversarial pass (an
  in-family Opus review plus fuzzing) against v0.4.0 and unreleased `main`.
  It found four issues, all now resolved or accepted with a stated reason:
  - **E-1** (Important): on the Rust backend, `no_net` blocked
    `socket.socket()` but not a `ctypes`/`dlsym` or raw-syscall call, while
    reporting `unenforced: []` — a guarantee over-reported. **Fixed**, and
    since strengthened past the original disclosure-only fix: PR #242
    (shipped **0.5.0**, 2026-08-22) added an actual in-kernel seccomp-bpf
    filter on Linux that refuses the syscall outright, closing the bypass
    rather than only disclosing it; the symbol-shim disclosure remains the
    mechanism on macOS and as the Linux no-seccomp fallback.
  - **N-1** (Minor): a namespace-based mitigation flagged as a future
    fragility if ever leaned on as load-bearing. **Holds** — no live bypass,
    not fixed, tracked as hardening.
  - **N-2** (Minor): a session-jail docstring overclaimed Landlock coverage
    of the write path's parent-directory TOCTOU. **Fixed** (docstring-only);
    the underlying residual is accepted pending `openat2(RESOLVE_BENEATH)`,
    same as before — only the stated reason for accepting it changed. This
    is the residual named in §3(1) and §4(3) above.
  - **N-3** (Minor): installer/setup scripts print host paths to stderr on
    some failure branches. **Accepted as benign** for the documented
    single-operator threat model.
  - The same document records that a cross-vendor (Codex) second opinion was
    **attempted and refused by Codex's own security-content filter** before
    it could examine the sandbox code at all — recorded as a residual (§7,
    "No independent third-party review"), not glossed over, and part of why
    this application exists.
- **Fuzzing** — `scripts/fuzz.py`, a deterministic mutation fuzzer over the
  safe-expression screen and the session path guards, wired into CI as a
  smoke gate. Since PR #239 (2026-08-21), coverage-guided **ClusterFuzzLite**
  (`.clusterfuzzlite/`, the same libFuzzer+atheris+sanitizer engine OSS-Fuzz
  uses, run in this repo's own CI rather than requiring upstream acceptance)
  runs alongside it. Combined, the two have found and fixed several
  DoS-and-crash-shaped findings since (`CHANGELOG.md` `[Unreleased]` and the
  0.4.0–0.5.0 entries): uncaught `UnicodeEncodeError`/`UnicodeDecodeError` on
  malformed surrogate/multibyte input to `safe_expr.classify_unsafe`, a bare
  `TypeError` on a heavy-function reference with no call parens, an
  unbounded-`Path.resolve()` CPU-cost DoS in the session jail (fixed with a
  pre-resolve length/segment-count cap), and three extreme-magnitude `Pow`
  shapes that made the safety screen itself raise while computing a refusal.
- **Release integrity** — every release since **0.2.0** (PR #173, PR #130)
  carries a `SHA256SUMS` file, a keyless sigstore build-provenance
  attestation (`gh attestation verify <file> --repo The-40-Thieves/codecalc`),
  and a CycloneDX SBOM of the dependency closure (PyPI + Cargo + pinned
  GitHub Actions); PyPI wheels additionally carry PEP 740 attestations from
  Trusted Publishing. No PGP key, deliberately — provenance is bound to the
  workflow that built the artifact rather than to a long-lived key one
  maintainer holds.
- **CI gates** (`scripts/check_*.py`) — `check_no_eval`, `check_parity`,
  `check_claims`, `check_contract`, `check_portability`, `check_version`,
  `check_changelog`, plus tier-evidence, tool-dispatch, tool-naming and
  opengrep-baseline gates added since, and permanent regression suites
  (`test_security.py`, `test_session_jail.py`, `test_platform_contract.py`)
  that make every un-disclaimed ceiling bite on **both** backends.
- **Governance** — `MAINTAINERS.md` (added alongside this document, PR #239)
  states the single-maintainer bus factor plainly and makes independent
  adversarial review of every security-critical change a standing,
  non-negotiable policy rather than an ad hoc habit, until a second human
  maintainer exists.

**Next step beyond this engagement:** application to upstream **OSS-Fuzz** for
continuous fuzzing on Google's infrastructure, now that the ClusterFuzzLite
harnesses have run in-repo across multiple releases (0.4.0 through 0.11.0)
and proven able to catch real crashes, per the findings listed above.

## 6. Concrete questions we want answered

1. Can executed code escape the rlimit/process-group sandbox on any supported
   platform (Linux, macOS, Windows Job Object / optional AppContainer)?
2. Can the `safe_expr.py` denylist be bypassed to reach SymPy's evaluator, and
   can the forked-child guard that backstops it be escaped or starved?
3. Can a session, or a per-run dependency install, read or write outside its
   jail — in particular via the documented parent-directory-symlink TOCTOU
   residual (§3(1), §4(3))?
4. Is the `no_net` disclosure now *complete*, or is there still a
   platform/backend path — including the pure-Python fallback and stateful
   session workers — where egress is possible while the result implies it is
   blocked?
5. Do the Landlock (Linux), Seatbelt (macOS), and AppContainer (Windows)
   confinements deliver what the docs claim on a real deployment, versus a CI
   runner that cannot exhibit them?
6. Does any output path — including a session artifact URI, an audit-log
   entry, or a validation error — leak host data (paths, env, secrets) that
   the redaction logic misses?
7. Against the CE-MCP taxonomy in §3(5): does codecalc have an
   exception-mediated code-injection path anywhere in its error handling, and
   should either off-by-default defense (the strict container backend, the
   capability broker) be on-by-default for a deployment exposed to untrusted
   or agent-steered input?

## 7. What we want from this review, and what we commit to

**What we want.** Not a checklist pass — an adversarial attempt to defeat the
specific claims in §3 and §4, on real platforms rather than only a CI runner,
with priority on the surfaces least exercised by an in-family review: the
platform-specific confinements (seccomp/Landlock/Seatbelt/Job
Object/AppContainer) that a self-audit can describe but not credibly attack
from the inside, and the newest surface (per-run dependency install) that has
had the least adversarial attention of anything in §4. A finding that the
*disclosure* is wrong — a guarantee reported that does not hold — is treated
with the same severity as a finding that the *mechanism* is wrong, per the
in-scope list in §3: this project has already fixed one of those (E-1) and
wants a reviewer who checks for the same class again, harder.

**What we commit to.**

- **Response time.** `SECURITY.md`'s standing commitment — acknowledgement
  within a week — applies to this engagement's findings the same as to any
  other report; there is no separate SLA behind it because there is no team
  behind it, and that is stated rather than promised away.
- **Disclosure.** Coordinated disclosure via GitHub private security
  advisories is this project's standing channel (`SECURITY.md`). For findings
  from an OSTIF-sourced audit specifically, we will also follow OSTIF's own
  stated disclosure policy — the Google Project Zero model of 90 days, plus
  30 if requested in advance — rather than asking the audit team to adapt to
  a different clock mid-engagement.
- **Fix window.** Every finding becomes a permanent regression test before
  the fix is considered done, the same discipline `AUDIT.md`,
  `docs/security/audit-2026-08-21.md`, and every fuzz-found crash listed in
  §5 already follow — a finding here does not get fixed and forgotten, it
  gets fixed and made impossible to reintroduce silently.
- **Publication.** The finished report is published under `docs/security/`
  alongside the existing audits, with every fix tracked as a public issue —
  the same standing practice this project already applies to itself.
- **Credit.** Findings are credited in the security advisory, the CHANGELOG,
  and `SECURITY.md`'s Hall of fame, per the existing researcher-recognition
  policy — there is no paid bounty (single-maintainer project, no budget to
  promise one), which is exactly the gap an OSTIF-coordinated engagement is
  positioned to fill from the funding side.

## 8. Build, run, and measure

```bash
git clone https://github.com/The-40-Thieves/codecalc && cd codecalc
uv sync                                   # Python side
cargo build --release --manifest-path executor/Cargo.toml   # Rust executor
uv run pytest                             # full suite
# gates: see CONTRIBUTING.md "Running the gates locally"
```

Source size (fill from the repo before submitting — re-run, do not reuse the
counts in §1, which are a snapshot as of this draft's last edit):

```bash
# Python
find codecalc scripts tests -name '*.py' | xargs wc -l | tail -1
# Rust
find executor/src -name '*.rs' | xargs wc -l | tail -1
```

## 9. Submission checklist (owner action — nothing above this line needs it)

**Channel.** OSTIF's current process starts with its intake form, not an
email or a GitHub issue: **https://forms.clickup.com/90132124106/f/2ky4p4ea-3833/O6UZRESBTKJLR0VB72**
(the "TALK TO OSTIF" link on https://ostif.org/get-an-audit/, verified
2026-09-08). Submitting the form gets a one-on-one coordination call, not an
immediate audit slot — OSTIF's own process is Coordinate → Audit → Patch →
Release-report-and-maintain, with a scoping questionnaire and a firm-facing
RFP built collaboratively *after* first contact, not before it. This document
is written to answer that questionnaire in one pass rather than starting from
nothing on the call.

**What to have ready, per OSTIF's own published guidance** (`ostif.org/get-an-audit`):

- [ ] **Project link** — the GitHub URL above; this document as the
      supporting technical brief.
- [ ] **Project documentation** — `README.md`, `SECURITY.md`, `AUDIT.md`,
      `docs/security/audit-2026-08-21.md`, `MAINTAINERS.md`, this file.
- [ ] **Prior reviews** — the two self-audits above, named as self-audits,
      not offered as if they were independent.
- [ ] **Security practices already in place** — the CI gate list in §5, the
      fuzzing setup (`scripts/fuzz.py` + ClusterFuzzLite) — OSTIF's own
      checklist specifically asks about fuzzing, dynamic-analysis, and CI/CD
      tooling.
- [ ] **An OpenSSF Scorecard run** (`github.com/ossf/scorecard`) — OSTIF's
      page recommends checking this before reaching out; codecalc does not
      run one in CI today, so generate a current score first rather than
      submitting without one. Same for the OpenSSF/CII Best Practices badge
      if time allows — neither exists for this repo yet.
- [ ] **Components/dependencies** — none of codecalc's runtime dependencies
      are closed-source; no cryptographic library is used at runtime (the
      sigstore/PEP 740 signing in §5 is release-pipeline tooling, not a
      codecalc runtime dependency) — worth stating plainly since OSTIF's
      questionnaire asks about encryption libraries specifically.
- [ ] **Primary contact** — name and email (fill in; not committed to the
      tree per this project's standing practice of keeping contact details
      out of version control until they are actually needed for an active
      engagement).
- [ ] **Funding** — state what portion, if any, the project can contribute;
      OSTIF's own page gives a $30k–$200k range for an initial audit
      depending on scope and prior testing, and offers to help source or
      split funding.
- [ ] **Timeline** — target window (fill in).

**Cover message (drafted, ready to paste into the intake form or an
introductory email):**

> Hi OSTIF — I maintain codecalc, an open-source MCP (Model Context Protocol)
> server that executes untrusted code in 31 languages on behalf of AI agents,
> plus symbolic-math and SMT-logic tools (https://github.com/The-40-Thieves/codecalc,
> Apache-2.0, ~81k lines across Python and Rust). Its whole job is running
> code it did not write, which makes an isolation bug a host compromise
> rather than a crash — and every audit it has had so far was performed by
> me or by an in-family AI model, which both documents say plainly is not
> the same thing as an independent review. I've prepared a full scope
> document (attached / linked: `docs/security/ostif-application.md` in the
> repo) covering the threat model, prioritized audit surfaces, what's
> already been fixed from two internal adversarial passes, and what we
> commit to on response time, disclosure, and fix tracking. I'd like to talk
> about what a coordinated, funded review through OSTIF would look like for
> a project this size and at this stage.

---

*Bounty note: codecalc is a single-maintainer project and does not run a paid
bug-bounty. Valid reports are credited in the advisory and the changelog, and
the project is open to a hosted bounty via a platform such as
[huntr](https://huntr.com) if a sponsor steps forward. See `SECURITY.md` for
reporting.*
