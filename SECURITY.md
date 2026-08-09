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
- `install_package` running installer hooks (`postinstall`, build backends, Cargo build scripts) **outside the sandbox**. This is a known limitation, tracked in [#23](https://github.com/The-40-Thieves/codecalc/issues/23), not a private vulnerability. Do not expose that tool to untrusted input.
- The pure-Python fallback executor providing fewer guarantees than the Rust one. It reports what it could not enforce in the `unenforced` array of every result.

## Known limitations

Stated here rather than discovered later:

| Limitation | Effect |
|---|---|
| `install_package` is not sandboxed | Install-time hooks run with the server user's filesystem access ([#23](https://github.com/The-40-Thieves/codecalc/issues/23)) |
| `update_runtimes` can update system packages | `apply=True` runs each manager's update command; the apt one is elevated. Gated on the host setting `CODECALC_ALLOW_RUNTIME_APPLY=1`, because `apply` is an argument a connected model controls ([#63](https://github.com/The-40-Thieves/codecalc/issues/63)) |
| Python fallback lacks the `no_net` shim | `no_net` is reported in `unenforced` rather than applied |
| Same-UID execution by default | Isolation is rlimits and process groups, not a container or VM |
| No signed release artifacts yet | Verify what you build; there is nothing to verify against yet ([#24](https://github.com/The-40-Thieves/codecalc/issues/24)) |

codecalc is built for a **single operator running it locally over stdio**. It has not been hardened for multi-user or hosted deployment, and the audit says so in its own verdict. If you intend to expose it to input you do not control, put it inside a container or microVM boundary of your own.

## Supported versions

No release has been tagged. The supported version is `main`. Fixes land there and nowhere else.

## What is gated

These invariants are enforced in CI on every pull request, so a regression fails rather than ships:

- `check_no_eval.py` — `eval` nowhere, `exec` in exactly one documented file
- `check_parity.py` — the env allowlist, runtime path, and language registry cannot drift between the Rust and Python backends
- `check_claims.py` — the counts and licence declarations in README, AUDIT.md, and the repository description match the code
- `check_portability.py` — no machine-specific paths or private addresses, including in tests
- `tests/test_security.py`, `tests/test_session_jail.py` — the audit's findings as permanent regression tests
