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

for t in tests/test_*.py; do uv run python "$t"; done
```

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

## Reporting security issues

Do not open a public issue. See [SECURITY.md](SECURITY.md).
