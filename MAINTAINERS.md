# Maintainers

This project has **one maintainer**. That is a bus factor of one, stated
here rather than left for someone to discover the first time a security
report goes unanswered for a fortnight. `SECURITY.md` already says the same
thing about response time — "a single-maintainer project with no SLA behind
it" — and this file is where that fact becomes a plan rather than a
disclaimer.

## Roster

| Area | Maintainer |
|---|---|
| Everything | `<maintainer GitHub handle>` (The 13th Letter / [The-40-Thieves](https://github.com/The-40-Thieves)) |

<!-- Fill in the handle before this file goes out. It is left as a placeholder
     deliberately: a MAINTAINERS.md should name a real, current, public
     identity, and that is the owner's to confirm rather than an agent's to
     assert. -->

Security reports do **not** go here. Use
[private vulnerability reporting](https://github.com/The-40-Thieves/codecalc/security/advisories/new)
— see `SECURITY.md`.

## How decisions are made

For now, the maintainer decides. There is no committee, no vote, and pretending
otherwise would be theatre. Two habits keep that from being arbitrary:

- **Design decisions are written down before they are built, and the reasoning
  survives even when the answer is "no".** The tool-facade proposal
  (`docs/design/2026-08-10-tool-facade.md`) is the worked example: two
  independent reviews said don't build it, so it is documented as *deliberately
  unbuilt* rather than silently dropped. A future contributor who wants it finds
  the reasons, not a blank.
- **Corrections are written into the document they correct.** `AUDIT.md` carries
  inline `CORRECTION` blocks; the changelog folds superseded sections instead of
  deleting them. The record is meant to show its own history.

## Review policy

Every change goes through the CI gates (`CONTRIBUTING.md` lists them) and at
minimum the author's own review. That is the floor. Above it, one class of
change carries an extra, non-negotiable requirement.

### Security-critical changes require an independent adversarial review

A change is **security-critical** if it touches any of:

- the executor sandbox — rlimits, the 23-entry environment allowlist, process-group
  kill, the `no_net` shim (`executor/`, `codecalc/executor.py`);
- the expression safety screen (`codecalc/safe_expr.py`) — the denylist between a
  caller string and SymPy's `eval`-based parser;
- the session jail (`codecalc/sessions.py`) — path traversal, quotas, cleanup
  liveness, the `O_NOFOLLOW` read/write path;
- the package installer (`codecalc/packages.py`) — argv construction,
  `--ignore-scripts`, Landlock confinement, quota pre/post-checks;
- the result contract or any `scripts/check_*.py` gate — because weakening the
  thing that proves a guarantee is itself a security change.

Such a change must not merge until an **adversarial review** has been recorded on
the pull request. Adversarial means the reviewer's task is to *break the claim* —
find the input, platform, or race that defeats it — not to confirm the diff looks
reasonable. The distinction is load-bearing: this project's own history has the
author writing down a closed-bug claim that a second, skeptical pass then
overturned.

Until there is a second human maintainer, that review comes from an **independent
model of a different family** (a cross-vendor pass) and/or a **fresh adversarial
agent** given only the diff and the claim, with the result quoted in the PR. When
a second human maintainer exists, they are the reviewer of record and the
automated pass becomes corroboration rather than the whole of it. The one place
this has a known blind spot — a vendor's own content filter refusing to review
security code — is recorded where it bit (`docs/security/audit-2026-08-21.md`),
so it is not silently absent.

This is a formalisation of what the project already does, not a new burden
invented on paper. Writing it down means it cannot quietly lapse the first time a
security fix feels small.

## Wanted: a second maintainer

A bus factor of one is the single biggest risk to this project's continuity, and
closing it is an explicit goal, not a someday. The path in:

1. Sustained, substantive contributions — not volume, but changes that show you
   understand *why* the gates and the sandbox are shaped the way they are.
2. A track record of the review above: having adversarially reviewed
   security-critical PRs and found real problems in them.
3. An invitation from the maintainer to co-maintain, at which point your handle
   joins the roster and the review-of-record shifts from a model to a person.

If you are a security researcher or an OSS-maintenance organisation who would
take this on — or fund the independent audit described in
`docs/security/ostif-application.md` — open a discussion or use the security
contact above.

## See also

- `SECURITY.md` — threat model, coordinated disclosure, what is gated.
- `AUDIT.md` and `docs/security/audit-2026-08-21.md` — the audits performed so
  far, and their own honest verdict that neither is a third-party review.
- `CONTRIBUTING.md` — the gates, the sign-off requirement, and the rule that a
  gate is not trusted until it has been watched failing.
