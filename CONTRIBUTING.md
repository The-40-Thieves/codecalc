# Contributing

## Sign your commits

Every commit needs a `Signed-off-by` trailer. Use `-s`:

```bash
git commit -s -m "fix: ..."
```

CI enforces this (`.github/workflows/dco.yml`) and a pull request without it fails on a check that previously had nothing explaining it. The trailer certifies the [Developer Certificate of Origin](https://developercertificate.org/): you wrote the patch, or you have the right to submit it under the project's licence.

Fixing a branch that already has unsigned commits:

```bash
git rebase --signoff main
```

## Licence

Apache-2.0. Contributions are accepted under the same terms. The repository was relicensed from AGPL-3.0-only before it had outside contributors; inbound patches now carry sign-off so that history stays clean.

## The rule that matters most

**A gate is not trusted until it has been watched failing.**

Every check in `scripts/` was added by first seeding the defect it claims to catch, confirming it failed, and only then keeping it. Several were wrong on the first attempt and passed a correct repository, or passed a broken one. If you add a check, seed a defect and show it failing in the pull request description.

The same applies to tests. Three suites in this repository once printed output, exited 0, and asserted nothing. A suite that cannot fail is not a suite.

Two habits follow from that:

- **Assert the value, not the shape.** `result["value"] == "0.1"`, never `isinstance(result["value"], str)`.
- **Every scan counts its inputs first.** A linter pointed at a renamed directory, a dependency scanner with no lockfile, and a genuinely clean repository all exit 0 and print the same thing. The count is what separates "clean" from "absent".

## Running the gates locally

```bash
uv sync
uvx --from 'ruff==0.16.1' ruff check codecalc/ tests/ scripts/

uv run python scripts/check_no_eval.py       # eval nowhere, exec in one file
uv run python scripts/check_parity.py        # Rust and Python backends cannot drift
uv run python scripts/check_claims.py        # stated counts match the code
uv run python scripts/check_portability.py   # no machine-specific paths
uv run python scripts/contract_check.py      # tool contract

# A bare `do uv run python "$t"; done` exits 0 whatever happens inside it — the
# loop's status is its last command, so one red suite in the middle reported
# success. Carry the failure out.
fail=0
for t in tests/test_*.py; do uv run python "$t" || { echo "FAILED: $t"; fail=1; }; done
[ "$fail" -eq 0 ]
```

**Not pytest.** These suites are standalone scripts that assert at module scope
and `sys.exit` at the end; pytest collects by importing, so it aborts on the
first file. `tests/conftest.py` disables collection and explains why, including
why wrapping the exits would be worse than the current behaviour. If you add a
suite, follow the existing shape — one script, one `sys.exit`, a printed line
per property.

The `scripts/` gates run on a bare checkout with no network, no token, and no built binary. That is deliberate: the test suites need the Rust executor and 31 language runtimes, so they cannot run everywhere, and an invariant that only holds on one machine is not an invariant. Keep new gates in that class if you can.

The Rust executor:

```bash
cd executor
cargo clippy --release --all-targets -- -D warnings
cargo test --release
cargo build --release
```

## Counts are gated

`check_claims.py` compares the tool count, language count, test-file count, assertion count, licence declarations, the env allowlist described in `AUDIT.md`, its suite snapshot, and the GitHub repository description against the code. Adding a tool or a test file therefore means updating README.md in the same commit. This is not busywork: the repository once shipped README, AUDIT.md, and the registry claiming 31, 33, and 32 languages simultaneously, all in one reviewed commit.

## Adding a language or a tool

- Languages live in `codecalc/registry.py` and must exist in **both** backends. `check_parity.py` will tell you if you missed one.
- Tools are `@mcp.tool` functions in `codecalc/server.py`. A new tool needs an entry in `TOOL_TIMEOUTS` (`codecalc/mcp_middleware.py`), coverage in `tests/test_mcp_all.py`, and the README count updated.
- Anything touching the sandbox needs a regression test in `tests/test_security.py`.

## Style

Match the file you are editing. The prevailing idiom is a comment that explains **why**, and specifically what went wrong before, rather than what the line does. Corrections are written into the documents they correct rather than quietly patched, which is why `AUDIT.md` carries inline `CORRECTION` blocks.

Measured numbers belong in the document with the method next to them. "Faster" without a figure and a spread is not a result.

## Releasing

One dispatch, one commit, every registry — so two registries cannot end up
carrying different code under the same version. `release.yml` builds and
verifies on any `v*` tag; publishing is opt-in per registry:

```
gh workflow run release.yml -f publish_to_pypi=true -f publish_to_crates=true
```

**Rehearse the upload first.** Every step before the upload has been exercised
many times; the upload itself has been exercised zero times, and it is the only
step that cannot be undone — a version number uploaded to PyPI is burned even if
the file is deleted afterwards.

```
gh workflow run release.yml -f publish_to_testpypi=true
```

Same artifacts, same action, same OIDC mechanism, different index. Register the
trusted publisher on TestPyPI too, so the rehearsal exercises the mechanism the
real release uses rather than the token path TestPyPI also accepts — a rehearsal
of a different mechanism is not a rehearsal. Then read
<https://test.pypi.org/project/codecalc/> and check the long description renders,
the URLs and classifiers are there, and every platform wheel is listed. The wheel
once shipped 700 bytes of METADATA with no description, no links and no
classifiers (#111); a project page is where that is visible before it matters.

crates.io has no equivalent rehearsal. RFC 3691 requires one manual publish
before trusted publishing can be configured at all, so that path is manual by
construction — see below.

Both publish jobs authenticate with **OIDC trusted publishing**. There is no
API token in this repository's secrets for either one, and nothing to rotate:
each job exchanges the workflow's identity for a token that lives minutes, and
crates.io's action revokes its own on the way out.

**crates.io needs one manual publish first, once.** RFC 3691 is explicit that
"a Trusted Publisher Configuration can only be created after an initial manual
publishing of a crate"; pending configurations are listed only as a future
possibility. So the first `codecalc-exec` release is:

```bash
 CARGO_REGISTRY_TOKEN='cio_...' cargo publish --manifest-path executor/Cargo.toml
```

with a token scoped to `publish-new` on that crate alone and the shortest
expiry offered, revoked immediately afterwards. Do that from a machine that is
NOT running codecalc: execution is same-uid, `CARGO_HOME` is in the executor's
env allowlist, and a token written to `~/.cargo/credentials.toml` is therefore
readable by anything you execute. Measured, not assumed. The environment
variable above is not allowlisted, so it does not reach executed code.

After that publish, configure the trusted publisher on the crate's settings
page (repository `The-40-Thieves/codecalc`, workflow `release.yml`, environment
`crates-io`) and no token is ever needed again.

PyPI has no such bootstrap: a pending publisher works before the project
exists, and the first OIDC publish creates it. It does NOT reserve the name
beforehand, so until that publish happens the name stays claimable.

**npm has no job here, and that is a decision rather than an omission.** It was
built and then dropped, so the reasoning is recorded to save anyone repeating
the exercise.

This repository publishes no JavaScript — there is no `package.json` — and the
MCP server is Python, which npm cannot deliver. The only shippable artifact is
the Rust executor, and a wrapper for it was prototyped end to end: it worked,
installed from tarballs, and ran. The cost is what settled it.

`postinstall`-downloads-a-binary is not available: npm v12 stopped running
install scripts by default in July 2026. The working pattern is
`optionalDependencies` with per-platform packages — six packages that must
version-lock on every release, since those dependencies pin exact versions and
one straggler breaks the wrapper. npm's trusted publishing is configured PER
PACKAGE ("each package can only have one trusted publisher configured at a
time"), so that is six configurations, not one. It also introduces JavaScript
into a repository whose gates — ruff, check_no_eval, the lot — do not cover it.

Against that, `release.yml` already attaches those exact binaries to GitHub
Releases with checksums. npm would have been a second, more fragile path to an
artifact that is already distributed.

If Node-ecosystem reach becomes worth that, the shape is known: a wrapper
package plus five platform packages under a scope, generated from the release
artifacts.

## Reporting security issues

Do not open a public issue. See [SECURITY.md](SECURITY.md).
