# Design: self-sustaining, self-updating language runtimes

Written 2026-10-27 against `main` at `53aeb9d`.

> [!important] Status: designed, Phase 0 done, Phases 1–5 not built
> codecalc is a product for other people, on macOS, Windows, Linux and other systems. Its language
> runtimes must stay current on their own and must never break a user's machine while doing it.
> This document is the plan. Each phase below is one pull request.

## Why the current runtime code cannot do this

`codecalc/runtimes.py` reports and updates runtimes through the package managers it finds on the
host. Its manager map was written on and for one Linux ARM64 server:

| Manager | Languages | Problem for a general user |
|---|---|---|
| mise | 16 | Not installed on most machines, and it updates the user's *global* toolchain |
| apt (`sudo -n apt-get`) | 13 | Debian/Ubuntu only. Needs root and moves system packages. Absent on macOS and Windows |
| rustup, swiftly, npm, uv | 1 each | Mutate the user's own toolchains |
| nix (on demand) | 1 | Rare outside NixOS |

Four further problems showed up on 2026-10-27, three fixed in #368:

1. `swiftly update` uninstalled the in-use toolchain and left none selected, so every Swift run
   failed while the update reported success (fixed: re-select the global default).
2. A missing apt language (PHP, C#) was reported as "up to date" (fixed: `installed: false`).
3. apt's "latest" meant "latest in this distro's sources" without saying so (fixed: `latest_scope`).
4. `update_runtimes(apply=True)` asks for confirmation through MCP elicitation, and that prompt
   was cancelled in Claude Code, so nothing can apply an update unattended. **Open.**

Even with every bug fixed, the model stays wrong: an MCP server that rewrites the toolchains a
person uses for their own work is invasive, and it cannot be made reliable across operating
systems it does not control.

## Goals and non-goals

Goals:

- A user on macOS, Windows or Linux (x86_64 and arm64) gets working runtimes without installing a
  package manager first.
- Runtimes update without anyone running a command, and only to versions CI has proven.
- A bad update is detected and rolled back automatically. The previous version keeps working.
- The project keeps itself current: new upstream releases flow in through CI, and humans only see
  failures.

Non-goals:

- Updating the user's own toolchains. codecalc reports on them and suggests commands; it changes
  them only on explicit, per-manager opt-in.
- Supporting every language on every OS. Coverage is published honestly per OS.
- Using elevated privileges (`sudo`, UAC) anywhere in the update path.

## Design

### 1. Two kinds of runtime

| | Host runtimes | Managed runtimes |
|---|---|---|
| Source | Whatever is on `PATH`: brew, winget, scoop, apt, dnf, pacman, mise, rustup… | Installed by codecalc into its own data directory |
| codecalc's role | Detect, report, suggest the right command for that OS | Install, update, verify, roll back |
| Touches the user's system | Only with explicit per-manager opt-in | Never outside its own directory |

Execution resolves a language in this order: a managed runtime if one is installed and healthy,
then a host runtime, then "not available" with a suggested install command. A user setting can
prefer host runtimes instead (for people who want codecalc to run exactly what they run).

The data directory comes from `platformdirs.user_data_dir("codecalc")`:

- macOS: `~/Library/Application Support/codecalc`
- Windows: `%LOCALAPPDATA%\codecalc`
- Linux and others: `$XDG_DATA_HOME/codecalc` (default `~/.local/share/codecalc`)

Layout:

```text
<data>/runtimes/
  python/3.15.1/        one directory per installed version
  python/3.15.0/        previous version, kept for rollback
  current.json          language -> active version, swapped atomically
  manifest.json         the verified manifest this install follows
  state.json            last check time, last result, failures
```

### 2. Provisioning through an isolated mise

mise runs on macOS, Linux and Windows and covers most of the languages codecalc supports.
codecalc drives it as a subprocess with its own environment, so it never reads or writes the
user's mise setup:

- `MISE_DATA_DIR`, `MISE_CACHE_DIR` and `MISE_CONFIG_DIR` point inside `<data>/`.
- `MISE_GLOBAL_CONFIG_FILE` points at a codecalc-generated config built from the manifest.
- The mise binary itself is fetched by codecalc (pinned version and checksum in the manifest) when
  the user has none, and is never put on the user's `PATH`.

Languages mise cannot serve on a given OS are not "managed" there. They stay host-only and are
marked that way in the registry. Expected examples, to be confirmed by the Phase 2 spike: Swift on
Windows, some BEAM languages on Windows, C# (served by `dotnet-install` scripts instead).

### 3. The runtime manifest is the source of truth

Every codecalc release publishes `runtimes.lock.json`:

```json
{
  "schema": 1,
  "codecalc": "1.19.0",
  "generated": "2026-11-01T00:00:00Z",
  "languages": {
    "python3": {
      "tier": "tested",
      "targets": {
        "linux-x86_64":   {"tool": "python", "version": "3.15.1", "sha256": "…"},
        "linux-arm64":    {"tool": "python", "version": "3.15.1", "sha256": "…"},
        "macos-arm64":    {"tool": "python", "version": "3.15.1", "sha256": "…"},
        "windows-x86_64": {"tool": "python", "version": "3.15.1", "sha256": "…"}
      }
    },
    "swift": {
      "tier": "best_effort",
      "targets": {
        "linux-x86_64": {"tool": "swift", "version": "6.4.1", "sha256": "…"},
        "macos-arm64":  {"host_only": true}
      }
    }
  }
}
```

Rules:

- A version enters the manifest only after the conformance suite passes on that target in CI.
  Clients update to the manifest, never to "whatever upstream released last night".
- The manifest is signed with Sigstore keyless signing from the release workflow (GitHub OIDC
  identity), and clients verify the signature and the workflow identity before using it.
  An unverifiable manifest is ignored, and the client keeps its current runtimes.
- The existing reliability tiers (`tested`, `best_effort`, `plan_only`; today 4, 25 and 1
  languages) carry over per target, so the published support table *is* the manifest.

### 4. The updater on the user's machine

No system timer is needed for the common case:

1. On server start, if more than 24 hours have passed since the last check, fetch the latest
   manifest in the background. Tool calls are never blocked on this.
2. For each language whose manifest version differs from `current.json`, install the new version
   into its own directory.
3. Run the language's smoke test (the existing `doctor --deep` probe for that language).
4. Pass: swap `current.json` atomically (write a temp file, then rename). Fail: keep the old
   version active, record the failure in `state.json`, and surface it in `codecalc doctor`.
5. Keep one previous version per language. Delete older ones after a successful swap.

Controls:

- `CODECALC_AUTO_UPDATE=0` turns the background updater off.
- `CODECALC_RUNTIMES=host` prefers host runtimes everywhere.
- A version pin per language in user config stops that language from moving.

### 5. A CLI that needs no MCP client

Phase 1 adds:

```text
codecalc runtimes status [--json] [--languages L]
codecalc runtimes update [--apply] [--languages L] [--json]
codecalc runtimes rollback LANGUAGE
codecalc runtimes schedule --install | --uninstall
```

Running the CLI is the user's consent, so `update --apply` needs no MCP elicitation, which
resolves problem 4. `schedule --install` writes a launchd agent (macOS), a Task Scheduler entry
(Windows) or a systemd user timer (Linux) for people whose server never restarts. All three run
`codecalc runtimes update --apply --json` and never need elevation.

### 6. codecalc updates itself through its install channel

| Channel | Update path |
|---|---|
| `uvx codecalc` (recommended default) | latest on each launch unless pinned |
| `pipx`, `uv tool` | channel upgrade; `codecalc doctor` reports when it is behind |
| MCPB bundle (Claude Desktop) | the host's extension update |
| Homebrew tap, winget, Scoop | manifests generated and submitted by the release workflow |
| Docker image | rebuilt per release with the manifest's runtimes baked in |

### 7. The project maintains itself

1. A scheduled workflow queries upstream for new runtime releases and opens a PR bumping the
   manifest.
2. The conformance suite runs on the ubuntu, macOS and Windows runners (the matrix already exists).
3. Green: the PR auto-merges, and the release workflow cuts a release that publishes the signed
   manifest.
4. Red: the workflow files an issue, and that language stays on its previous version.

This loop needs branch protection with required checks, because auto-merge on an unprotected
branch merges immediately. That is Phase 0, done on 2026-10-27: 42 required GitHub Actions checks,
repository auto-merge enabled, admins can still bypass.

## Phases

| # | Pull request | Unblocks |
|---|---|---|
| 0 | Protect `main` with required checks; enable repository auto-merge. **Done 2026-10-27.** | every automated merge |
| 1 | `codecalc runtimes` CLI. Per-OS host detection (brew, winget, scoop, dnf, pacman, apt) that reports and suggests, never mutates | users on every OS see their real status; unattended apply |
| 2 | Managed runtime directory and isolated mise provisioner; execution prefers managed runtimes. Starts with a spike that publishes the real per-OS support table | codecalc stops depending on the host |
| 3 | Manifest format, Sigstore signing and verification, conformance suite across 3 OSes | safe target versions |
| 4 | Background updater: install, smoke-test, swap, roll back; `schedule --install` | self-updating on user machines |
| 5 | Scheduled bump workflow and auto-merge, release workflow publishing the manifest, Homebrew, winget and Scoop manifests | self-sustaining releases |

## Risks and open questions

- **Windows coverage is uneven.** Several languages may never be managed on Windows. Phase 2
  publishes the real table before anything is promised.
- **Disk use.** Managed runtimes plus one rollback version can reach several gigabytes. The
  default should install a language on first use, not all of them up front.
- **Network-restricted environments.** The updater must fail quietly and keep working offline;
  `CODECALC_AUTO_UPDATE=0` must be documented for air-gapped installs.
- **mise as a dependency.** If mise changes its CLI or its Windows support, the provisioner breaks.
  Phase 2 pins the mise version in the manifest and runs it only through one adapter module.
- **Sigstore on clients.** Verifying in Python adds a dependency. If it is too heavy, the fallback
  is checksum files signed by the release key, verified with a vendored public key.
- **Who confirms MCP-initiated applies?** Problem 4 (elicitation cancelled in some clients) still
  affects the MCP `update_runtimes` tool. The CLI path does not depend on it; the MCP tool may
  become report-only once the background updater exists.
