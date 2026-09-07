"""Per-run dependencies for `execute_code` and `session_run`.

A run can declare packages to install BEFORE it executes, two ways:

  * a PEP 723 inline script metadata block (`# /// script` ... `# ///`) in the
    source, for python3 — the canonical regex and reference `read()` shape
    below are copied from PEP 723's own reference implementation, as published
    in packaging.python.org's "inline script metadata" specification, parsed
    with `tomllib`. Only `dependencies` and `requires-python` are standardised
    by the spec; this module reads `dependencies` and ignores `requires-python`
    and any other table (`[tool.*]` included) rather than erroring on their
    presence.
  * an explicit `dependencies: list[str]` tool argument — the only mechanism
    for node, and a convenience for python3 that MERGES with the block, deduped
    by PEP 503 normalized name with the explicit argument winning a conflict
    (reference for the merge shape: mcp-python-exec-sandbox's script.py:187-222,
    MIT licensed).

THE FETCH NEVER RUNS INSIDE THE SANDBOXED STEP. Every dependency here is
installed by calling the existing `packages.install` path — the deny-by-default
allowlist, the argv-injection checks, `--only-binary=:all:`/`--ignore-scripts`,
Landlock/Seatbelt confinement, the 600s timeout — BEFORE the code is handed to
the executor. The executor's own `--no-net` is unrelated and unchanged: this
module governs a separate step that runs earlier, outside the sandbox, as a
direct subprocess (see packages.py's own module docstring for why that install
step has never been inside the sandbox for any package).

THE REFUSAL RULE IS THE SECURITY PROPERTY. A run that declares dependencies but
asked for `no_net=True`, or is subject to a `deny-network`/`strict` capability
policy, must not perform the fetch — that would grant egress the run did not
request (or that policy withheld) through a side door the sandboxed step's own
`--no-net` never touches. `network_refused` is the one place that decision is
made; every caller (sessionless execute_code, a session's execute_code, and
session_run) routes through it rather than re-deriving the condition.
`network_refused` deliberately does NOT consult `policy.grant`: a policy that
both denies network by default AND grants it back (`deny-network,allow-network`)
still refuses a dependency install. The broker's own `grant` exists for a spec
that explicitly asked for network and is trusted to use it accountably inside
the sandbox; a dependency fetch is a SEPARATE, unsandboxed side-channel that
grant was never written to reason about, so this stays fail-closed rather than
inheriting an escalation meant for a different step.

THE IMPLICIT TRIGGER IS WORTH NAMING OUT LOUD. A PEP 723 block is read from
`code` whether or not the caller ALSO passed an explicit `dependencies`
argument — so source text alone, with no `dependencies` argument at all, can
start a confined-but-real subprocess and real network egress the moment
`execute_code`/`session_run` is called, wherever the network refusal above does
not apply. `resolve()` reports this as `Resolution.implicit`, and every caller
audits it (`audit.DEPENDENCY_INSTALL_IMPLICIT`) the same way `packages.install`
audits a denied install — so an implicit, source-driven install is
distinguishable from an operator explicitly calling `install_package` or
passing `dependencies=[...]`. See SECURITY.md and the README's network-boundary
table for the caller-facing statement of this, and how to disable it
(`no_net=True`, or a `deny-network`/`strict` capability policy).

TWO BUDGETS, NOT ONE. The sandboxed step already has `timeout` — a wall-clock
ceiling on THE CODE. Installing dependencies first, unbounded, would let
`execute_code(timeout=5, dependencies=[a, b, c])` block for up to three times
`packages.install`'s own 600s subprocess ceiling before that 5s deadline even
starts — the run's own timeout says nothing about the step that precedes it.
`DEFAULT_DEPENDENCY_INSTALL_BUDGET_SECONDS` is a SEPARATE, fixed aggregate
ceiling across every dependency of one run, independent of `timeout`; exceeding
it stops before the next install and is reported as `errors.TIMEOUT` naming
the budget (`DEPENDENCY_INSTALL_BUDGET_EXCEEDED`), not folded into a generic
install failure. `execute_code_stream`, `run_submit`, and `compare_execution`
never call into this module at all — a PEP 723 block in code passed to any of
them is inert prose, not a trigger, because none of them accept a
`dependencies` argument or route through `resolve()`.

THE SESSIONLESS WORKDIR HAS NO SESSION TO QUOTA AGAINST, SO THIS REUSES ONE.
`packages.install`'s own docstring says it runs no quota check on a bare
`workdir` — there is no session for it to measure against. `install_dependencies`
closes that gap itself: after every successful install into a sessionless
`workdir`, it measures the directory the same way `sessions._session_dir_size`
does (`sessions._iter_regular_files`) and refuses to continue past
`sessions._session_disk_quota_bytes()` — the SAME constant/env var
(`CODECALC_SESSION_DISK_QUOTA_MB`) a session workspace is already held to,
rather than inventing a second, independently-tunable cap for what is, in
every way that matters here, the same kind of workspace a session already has.
A session-targeted install is unaffected: `sessions.quota_precheck`/
`quota_postcheck` already cover it.
"""

from __future__ import annotations

import re
import time
import tomllib
from typing import Any, NamedTuple

from . import capabilities, errors, packages, registry

#: The PEP 723 spec's own reference regex, verbatim — see the module docstring
#: for where it comes from. `(?m)` so `^`/`$` anchor per line; the named
#: groups are what the reference `read()` implementation matches against.
PEP723_REGEX = re.compile(
    r"(?m)^# /// (?P<type>[a-zA-Z0-9-]+)$\s(?P<content>(^#(| .*)$\s)+)^# ///$"
)

#: Which languages this feature installs for, and the installer name disclosed
#: in each dependency result entry — the two languages the design scopes this
#: to (python3's PEP 723 block, node's `dependencies` argument). Anything else
#: never reaches `resolve()` with a non-empty explicit list from the tools that
#: call it (execute_code/session_run only forward `dependencies` for these).
INSTALLER_NAMES = {"python3": "uv", "node": "npm"}

#: Where a dependency spec's bare project name ends — the same qualifier
#: boundary `packages._bare_name` uses, kept as its own copy here because
#: normalization (below) is PEP 503, not `packages.py`'s allowlist comparison.
_NAME_STOP_RE = re.compile(r"[\[=<>!~,\s]")

#: Aggregate wall-clock budget across EVERY dependency of one run — separate
#: from the run's own `timeout` (see the module docstring's "TWO BUDGETS").
#: 120s is generous for the common case (a handful of pure-Python/JS packages,
#: no source build — `--only-binary=:all:`/`--ignore-scripts` already refuse
#: anything that would need one) while bounding the worst case to a fixed,
#: documented multiple of a typical `execute_code` call rather than the
#: 600s-per-dependency ceiling `packages.install` alone would allow.
DEFAULT_DEPENDENCY_INSTALL_BUDGET_SECONDS = 120

#: Stable `provider_error` discriminators — ungated vocabulary, not new members
#: of the closed `code` enum (both still stamp `errors.TIMEOUT`/
#: `errors.RESOURCE_EXHAUSTED`, the same pattern `capabilities.py` uses for its
#: own specific refusals under `errors.PERMISSION_DENIED`).
DEPENDENCY_INSTALL_BUDGET_EXCEEDED = "dependency_install_budget_exceeded"
DEPENDENCY_WORKDIR_QUOTA_EXCEEDED = "dependency_workdir_quota_exceeded"


class Resolution(NamedTuple):
    """`resolve()`'s result. A NamedTuple so `specs, error = resolve(...)`
    keeps working by position while `result.implicit` is also readable by
    name at the one call site (the audit emit) that needs it."""

    specs: list[str]
    error: dict | None
    implicit: bool


class DependencyBlockError(ValueError):
    """A PEP 723 `# /// script` block is present but malformed.

    Either more than one `script` block was found (the spec requires
    uniqueness) or its content is not valid TOML. Callers surface this as a
    `validation` result — the author wrote a block and is entitled to know it
    did not parse, not to have it silently ignored.
    """


def parse_pep723_block(source: str) -> dict[str, Any] | None:
    """Return the parsed `# /// script` metadata block in `source`, or None.

    None means no such block is present — a normal, unremarkable case (most
    programs declare no inline metadata), not an error. Mirrors the spec's own
    reference `read()` function: strip the `# `/`#` comment prefix from every
    content line, then hand the joined text to `tomllib`.
    """
    matches = [
        m for m in PEP723_REGEX.finditer(source) if m.group("type") == "script"
    ]
    if len(matches) > 1:
        raise DependencyBlockError(
            "multiple '# /// script' PEP 723 blocks found; the specification "
            "requires at most one"
        )
    if not matches:
        return None
    content = "".join(
        line[2:] if line.startswith("# ") else line[1:]
        for line in matches[0].group("content").splitlines(keepends=True)
    )
    try:
        return tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        raise DependencyBlockError(f"malformed PEP 723 script block: {exc}") from exc


def normalize_name(spec: str) -> str:
    """PEP 503 normalized project name from a dependency spec, for dedup.

    Strips everything from the first extras/version/whitespace marker onward
    (`_NAME_STOP_RE`), then collapses runs of `-`/`_`/`.` to a single `-` and
    lowercases — the normalization PEP 503 defines for comparing distribution
    names.
    """
    name = _NAME_STOP_RE.split(spec, 1)[0]
    return re.sub(r"[-_.]+", "-", name).lower()


def merge_dependencies(block_deps: list[str], explicit_deps: list[str] | None) -> list[str]:
    """Merge a PEP 723 block's `dependencies` with an explicit tool argument.

    Deduped by `normalize_name`; the EXPLICIT argument wins a conflicting
    version/spec for the same project (reference for this merge shape:
    mcp-python-exec-sandbox's script.py:187-222, MIT). Order: the block's own
    order first, then any explicit dependency not already named by the block,
    appended — so the common case (a block with no explicit override) returns
    the block untouched.
    """
    merged: dict[str, str] = {}
    for dep in block_deps:
        merged[normalize_name(dep)] = dep
    for dep in explicit_deps or ():
        merged[normalize_name(dep)] = dep
    return list(merged.values())


def resolve(language: str, code: str,
            dependencies: list[str] | None) -> Resolution:
    """Parse + merge one run's declared dependencies.

    Returns `Resolution([], None, False)` when nothing was declared — the
    overwhelmingly common case, and the caller's signal to skip parsing, the
    refusal check, and installation entirely: no `dependencies` field is added
    to the result, matching today's behaviour byte for byte. Returns
    `Resolution([], error_result, False)` on a malformed PEP 723 block, stamped
    `validation` — never silently dropped. Otherwise returns the merged,
    deduped spec list.

    `implicit` is True exactly when a PEP 723 block supplied at least one
    dependency and the caller passed NO explicit `dependencies` argument — the
    case the module docstring's "IMPLICIT TRIGGER" section names: source text
    alone, unaccompanied by any tool argument a caller had to type, is what
    started the install. A block merged WITH an explicit argument, or an
    explicit argument alone, is not implicit — the caller asked, in the call
    itself, for dependencies to be installed.

    Only python3 reads a PEP 723 block from `code`; every other language's
    dependencies come from the `dependencies` argument alone (there is nothing
    else to merge with).
    """
    lang = registry.canonical(language) or language
    block_deps: list[str] = []
    if lang == "python3":
        try:
            block = parse_pep723_block(code)
        except DependencyBlockError as exc:
            return Resolution([], errors.error_result(errors.VALIDATION, str(exc)), False)
        if block is not None:
            block_deps = [str(dep) for dep in (block.get("dependencies") or [])]
        specs = merge_dependencies(block_deps, dependencies)
    else:
        specs = list(dependencies or [])
    implicit = bool(block_deps) and not dependencies
    return Resolution(specs, None, implicit)


def network_refused(*, no_net: bool, policy: capabilities.CapabilityPolicy | None) -> bool:
    """Whether a dependency-bearing run must be refused before any fetch.

    True when the run itself asked for `no_net=True`, or when the active
    capability policy denies network by default or is `strict` — `strict`
    refuses even without `deny-network`, because a background fetch outside
    the sandboxed step is exactly the kind of unaccountable egress a strict
    policy exists to rule out, not something `no_net` on the run itself can
    catch (the two are enforced at different points).
    """
    if no_net:
        return True
    return policy is not None and (capabilities.NETWORK in policy.default_deny or policy.strict)


def refusal_result(no_net: bool, policy: capabilities.CapabilityPolicy | None) -> dict:
    """The stable, coded refusal for a dependency-bearing run denied network.

    Reuses `capabilities.CAPABILITY_NOT_REQUESTED` — the same `provider_error`
    a broker rejection uses for "policy would grant a capability the request
    did not ask for" — under the same taxonomy code, `errors.PERMISSION_DENIED`.
    This is not a new closed-enum member: it is the existing discriminator,
    reused for the specific case of a dependency install that would need
    network the run does not have.
    """
    if no_net:
        reason = ("dependency install needs network egress, which this run "
                  "does not have: no_net=True was requested; the fetch was "
                  "not attempted")
    else:
        reason = ("dependency install needs network egress, which this run "
                  "does not have: the active capability policy denies or "
                  "strictly limits network; the fetch was not attempted")
    return errors.error_result(
        errors.PERMISSION_DENIED, reason,
        provider_error=capabilities.CAPABILITY_NOT_REQUESTED,
        # The capability the install needed and did not have — `[]` here would
        # read as "nothing was requested", which is backwards: network WAS
        # what the install required, refused before the fetch. Mirrors
        # `capabilities.rejection_result`, which populates this from the
        # decision's own `requested` set for the same reason.
        requested_capabilities=[capabilities.NETWORK],
    )


def _workdir_size(workdir: str) -> int:
    """Total bytes a sessionless run's dependency workdir occupies, measured
    the same way `sessions._session_dir_size` measures a session's — via
    `sessions._iter_regular_files`, so "artifact" means the same thing in both
    places. Local import, mirroring how `packages.install` already reaches
    into `sessions` only where it needs to (avoids a module-level import
    cycle: `sessions.py` does not import this module, but nothing enforces
    that staying true forever)."""
    from pathlib import Path

    from . import sessions
    d = Path(workdir)
    if not d.is_dir():
        return 0
    return sum(st.st_size for _, st in sessions._iter_regular_files(d))


def _workdir_quota_bytes() -> int:
    """The cap a sessionless run's dependency workdir is held to — reused
    from `sessions._session_disk_quota_bytes()` (`CODECALC_SESSION_DISK_QUOTA_MB`),
    not a second, independently-tunable constant. See the module docstring's
    "THE SESSIONLESS WORKDIR HAS NO SESSION TO QUOTA AGAINST" section for why."""
    from . import sessions
    return sessions._session_disk_quota_bytes()


def install_dependencies(language: str, specs: list[str], *,
                         session_id: str | None, workdir: str | None,
                         audit: object | None = None,
                         budget_seconds: float = DEFAULT_DEPENDENCY_INSTALL_BUDGET_SECONDS,
                         ) -> tuple[list[dict], dict | None]:
    """Install every declared dependency via `packages.install`, in order.

    Returns `(entries, failure)`: `entries` is the `dependencies` result field
    — `[{spec, language, ok, installer, elapsed_ms, unenforced?}]`, one entry
    per dependency ATTEMPTED so far (a failure still discloses what came
    before it). `failure` is `None` when every install succeeded; otherwise
    ONE of three shapes, and the caller returns it as-is (it already carries
    `error`/`code`) rather than inventing a new one, and must not proceed to
    execute the run:

      1. `packages.install`'s own raw result for the dependency that failed
         (denied by the allowlist, a bad spec, a genuine install failure).
      2. A stamped `errors.TIMEOUT` when `budget_seconds` (see the module
         docstring's "TWO BUDGETS") is exhausted before the next dependency
         would even start — `provider_error` is
         `DEPENDENCY_INSTALL_BUDGET_EXCEEDED` and the message names the
         budget and how far the run got.
      3. A stamped `errors.RESOURCE_EXHAUSTED` when a sessionless run's
         `workdir` grows past `_workdir_quota_bytes()` after an install that
         itself succeeded — `provider_error` is
         `DEPENDENCY_WORKDIR_QUOTA_EXCEEDED`. Session-targeted installs never
         reach this: `sessions.quota_precheck`/`quota_postcheck` already
         govern that workspace.

    Exactly one of `session_id`/`workdir` is meaningful per `packages.install`:
    a session install targets the session's own workspace; a sessionless run
    targets the workdir the caller pre-created for it. Each `packages.install`
    call is given the REMAINING budget as its own `timeout`, so one dependency
    cannot silently consume the whole aggregate on its own.
    """
    lang = registry.canonical(language) or language
    installer = INSTALLER_NAMES.get(lang, lang)
    entries: list[dict] = []
    started = time.monotonic()
    for spec in specs:
        remaining = budget_seconds - (time.monotonic() - started)
        if remaining <= 0:
            failure = errors.error_result(
                errors.TIMEOUT,
                f"dependency install budget of {budget_seconds:g}s exceeded "
                f"after installing {len(entries)} of {len(specs)} "
                f"dependencies; {spec!r} was not attempted",
                provider_error=DEPENDENCY_INSTALL_BUDGET_EXCEEDED,
                budget_seconds=budget_seconds,
            )
            return entries, failure
        dep_started = time.monotonic()
        raw = packages.install(
            lang, spec, session_id=session_id,
            workdir=None if session_id else workdir, audit=audit,
            timeout=max(1, int(remaining)),
        )
        entry: dict[str, Any] = {
            "spec": spec, "language": lang,
            "ok": bool(raw.get("ok")), "installer": installer,
            "elapsed_ms": int((time.monotonic() - dep_started) * 1000),
        }
        unenforced = raw.get("unenforced")
        if unenforced:
            entry["unenforced"] = unenforced
        entries.append(entry)
        if not raw.get("ok"):
            return entries, raw
        if workdir is not None and session_id is None:
            size = _workdir_size(workdir)
            quota = _workdir_quota_bytes()
            if size > quota:
                failure = errors.error_result(
                    errors.RESOURCE_EXHAUSTED,
                    f"the run's dependency workdir reached {size} bytes after "
                    f"installing {spec!r}, over the {quota}-byte per-run quota "
                    "(CODECALC_SESSION_DISK_QUOTA_MB, shared with the session "
                    "workspace cap)",
                    provider_error=DEPENDENCY_WORKDIR_QUOTA_EXCEEDED,
                    quota_bytes=quota, measured_bytes=size,
                )
                return entries, failure
    return entries, None
