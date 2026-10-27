# Design: self-sustaining, self-updating language runtimes

Written 2026-10-27 against `main` at `53aeb9d`. Revised the same day: the provisioning engine
changed from an isolated mise to rattler and conda-forge (see "Alternatives considered").

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
- Runtimes update without anyone running a command, and only to the exact bytes CI has proven.
- A bad update is detected and rolled back automatically. The previous version keeps working.
- The project keeps itself current: new upstream releases flow in through CI, and humans only see
  failures.

Non-goals:

- Updating the user's own toolchains. codecalc reports on them and suggests commands; it changes
  them only on explicit, per-manager opt-in.
- Supporting every language on every OS. Coverage is published honestly per OS.
- Using elevated privileges (`sudo`, UAC) anywhere in the update path.
- Compiling anything on the user's machine. Every managed runtime is a prebuilt binary.

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
<data>/
  pkgs/                   shared package cache; prefixes hardlink from it
  runtimes/
    python/3.15.1/        one prefix per installed version
    python/3.15.0/        previous version, kept for rollback
    current.json          language -> active version, swapped atomically
    manifest.json         the verified manifest this install follows
    state.json            last check time, last result, failures
```

The cache and the prefixes live under the same directory so hardlinks work. Where they cannot
(a cross-device data directory), rattler falls back to copying.

### 2. Provisioning: three tiers in one manifest

Every managed language comes from exactly one tier per target OS. The tier is recorded in the
manifest, so the client never guesses.

**Tier 1: conda-forge through rattler (most languages).** codecalc embeds
[`py-rattler`](https://github.com/conda/rattler), the Rust installer behind pixi, as an ordinary
pip dependency. It ships wheels for Linux (glibc and musl hosts), macOS and Windows on x86_64 and
arm64. conda-forge packages are prebuilt and relocatable, and they bundle their own libraries, so
the same bytes work on any glibc Linux distribution.

- CI owns solving. A `pixi.toml` in the repo declares one environment per language; CI solves it
  into `pixi.lock`, which pins every package *and every dependency* by URL and sha256 for each
  platform.
- The client never solves. It reads the locked package list for its platform from the manifest
  and installs it into a fresh prefix with rattler. The result is byte-identical to what CI
  tested.
- No external tool is fetched, nothing is put on the user's `PATH`, and nothing outside
  `<data>/` is written. The Phase 2 spike proves the last claim on each OS.

**Tier 2: direct official binaries (the gaps).** For a language and OS that conda-forge does not
serve, the manifest lists the vendor's own archive by URL and sha256. codecalc downloads,
verifies, and unpacks it into the same prefix layout. Planned entries:

| Language | Target | Source |
|---|---|---|
| Swift | Linux, macOS | swift.org toolchain archives |
| Haskell | all | GHC bindists from ghcup's metadata (conda-forge is stuck at GHC 8.10.7) |
| Erlang, Elixir | Windows | official Erlang/OTP and Elixir Windows builds |
| jq, DuckDB | Windows | upstream release binaries |

**Tier 3: host only.** Anything left is detected on the host and never installed. Expected:
Swift and Mojo on Windows, zsh on Windows, and C/C++ on Windows where conda-forge's clang needs
Microsoft's MSVC build tools, which codecalc will not install.

Expected coverage, to be replaced by the Phase 2 spike's measured table:

| Platform | Tier 1 | Tier 2 | Host only |
|---|---|---|---|
| Linux, macOS (x86_64, arm64) | ~28 | Swift, Haskell | Mojo where unsupported |
| Windows x86_64 | ~20 | Haskell, Erlang, Elixir, jq, DuckDB | Swift, Mojo, zsh, C/C++ without MSVC |
| Windows arm64 | thin; measured in the spike | as x86_64 where builds exist | the rest |

### 3. The runtime manifest is the source of truth

Every codecalc release publishes `runtimes.lock.json`, generated from `pixi.lock` plus the Tier 2
entries:

```json
{
  "schema": 1,
  "codecalc": "1.19.0",
  "generated": "2026-11-01T00:00:00Z",
  "languages": {
    "python3": {
      "tier": "tested",
      "targets": {
        "linux-64":  {"source": "conda-forge", "version": "3.15.2", "packages": [{"url": "…", "sha256": "…"}]},
        "osx-arm64": {"source": "conda-forge", "version": "3.15.2", "packages": [{"url": "…", "sha256": "…"}]},
        "win-64":    {"source": "conda-forge", "version": "3.15.2", "packages": [{"url": "…", "sha256": "…"}]}
      }
    },
    "swift": {
      "tier": "best_effort",
      "targets": {
        "linux-64":  {"source": "direct", "version": "6.4.1", "url": "…", "sha256": "…"},
        "osx-arm64": {"source": "direct", "version": "6.4.1", "url": "…", "sha256": "…"},
        "win-64":    {"source": "host"}
      }
    }
  }
}
```

Target names follow conda's platform names (`linux-64`, `linux-aarch64`, `osx-64`, `osx-arm64`,
`win-64`, `win-arm64`).

Rules:

- A version enters the manifest only after the conformance suite passes on that target in CI.
  Clients update to the manifest, never to "whatever upstream released last night".
- Every downloaded file, in either tier, is checked against its sha256 before use.
- The manifest is signed with Sigstore keyless signing from the release workflow (GitHub OIDC
  identity), and clients verify the signature and the workflow identity before using it.
  An unverifiable manifest is ignored, and the client keeps its current runtimes. conda-forge does
  not yet serve per-package signatures, so the chain of trust is: signed manifest, then hashes.
- The existing reliability tiers (`tested`, `best_effort`, `plan_only`; today 4, 25 and 1
  languages) carry over per target, so the published support table *is* the manifest.

### 4. The updater on the user's machine

No system timer is needed for the common case:

1. On server start, if more than 24 hours have passed since the last check, fetch the latest
   manifest in the background. Tool calls are never blocked on this.
2. For each language whose manifest entry differs from `current.json`, install the new version
   into its own prefix. Only languages already in use are updated; a language is installed the
   first time it is run.
3. Run the language's smoke test (the existing `doctor --deep` probe for that language).
4. Pass: swap `current.json` atomically (write a temp file, then rename). Fail: keep the old
   version active, record the failure in `state.json`, and surface it in `codecalc doctor`.
5. Keep one previous version per language. Delete older ones after a successful swap, then remove
   cache entries no prefix uses.

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
| Offline bundle | `pixi-pack` archive of the manifest's runtimes, attached to each release |

### 7. The project maintains itself

1. A scheduled workflow runs `pixi update` and checks Tier 2 upstreams for new releases, then opens
   a PR bumping `pixi.lock` and the Tier 2 entries.
2. The conformance suite runs on the ubuntu, macOS and Windows runners (the matrix already exists),
   installing each runtime exactly as a client would.
3. Green: the PR auto-merges, and the release workflow cuts a release that publishes the signed
   manifest.
4. Red: the workflow files an issue, and that language stays on its previous version.

This loop needs branch protection with required checks, because auto-merge on an unprotected
branch merges immediately. That is Phase 0, done on 2026-10-27: 42 required GitHub Actions checks,
repository auto-merge enabled, admins can still bypass.

## Alternatives considered

| Option | Verdict | Why |
|---|---|---|
| Isolated mise (this document's first draft) | Rejected | About 12 different upstreams; Erlang and Ruby compile from source on non-Ubuntu Linux, so users can run a binary CI never tested; checksums only where a backend provides them; Rust leaks into `~/.rustup` and `~/.cargo`; a fast-moving external CLI to fetch and pin |
| proto, pkgx, devbox, Flox, asdf | Rejected | Same many-upstreams problem, or no native Windows support |
| Containers (Docker, Podman) | Optional provider later | Windows needs admin rights for WSL2 or Hyper-V, or a Docker Desktop licence; macOS needs a VM |
| WebAssembly runtimes | Optional provider later | Only about 4–6 languages, in reduced form, mostly community-maintained or archived builds |
| Remote execution (E2B and others) | Optional provider later | Nothing works offline; Daytona's repo is archived, Piston and Judge0 are near-dormant |

Checked on 2026-10-27: `py-rattler` 0.28.0 publishes win_amd64, win_arm64 and musllinux wheels;
conda-forge's latest Python builds cover linux-64, linux-aarch64, osx-64, osx-arm64, win-64 and
win-arm64; conda-forge's latest Erlang and DuckDB CLI builds have no win-64.

## Phases

| # | Pull request | Unblocks |
|---|---|---|
| 0 | Protect `main` with required checks; enable repository auto-merge. **Done 2026-10-27.** | every automated merge |
| 1 | `codecalc runtimes` CLI. Per-OS host detection (brew, winget, scoop, dnf, pacman, apt) that reports and suggests, never mutates | users on every OS see their real status; unattended apply |
| 2 | Spike first (below), then the managed runtime directory, the rattler (Tier 1) and direct-binary (Tier 2) provisioners, and execution preferring managed runtimes | codecalc stops depending on the host |
| 3 | Manifest generation from `pixi.lock`, Sigstore signing and verification, conformance suite across 3 OSes | safe target versions |
| 4 | Background updater: install, smoke-test, swap, roll back; `schedule --install` | self-updating on user machines |
| 5 | Scheduled bump workflow and auto-merge, release workflow publishing the manifest and offline bundle, Homebrew, winget and Scoop manifests | self-sustaining releases |

### Phase 2 spike (about one day)

- A `pixi.toml` with one environment per language, solved for linux-aarch64, osx-arm64, win-64
  and win-arm64.
- A throwaway installer that installs each environment with `py-rattler` from the lock and runs
  the language's hello-world.
- A GitHub Actions run on ubuntu-24.04-arm, macos-latest, windows-latest and windows-11-arm that
  records pass/fail, install time and disk use per language, and diffs the filesystem to confirm
  nothing was written outside the data directory.

Decision gate: at least 20 of 22 sampled language/OS cells pass, and no writes outside the data
directory. Otherwise this design returns for revision before any provisioner code is written.
The spike's output replaces the expected coverage table in section 2.

## Risks and open questions

- **Windows coverage is uneven, and win-arm64 is thin.** Tier 2 closes some gaps; the rest stay
  host only. Phase 2 publishes the measured table before anything is promised.
- **conda-forge lags upstream** by days for most packages and longer for a few. The bump workflow
  surfaces the lag; Tier 2 can carry a language that falls too far behind.
- **No Alpine or other musl hosts.** conda-forge builds target glibc. musl users get host runtimes
  only.
- **Disk use.** Each runtime bundles its own libraries, so the full set can reach several
  gigabytes. Mitigated by installing on first use, hardlinking from the shared cache and keeping
  one rollback version.
- **Network-restricted environments.** The updater must fail quietly and keep working offline;
  `CODECALC_AUTO_UPDATE=0` and the offline bundle must be documented for air-gapped installs.
- **rattler as a dependency.** `py-rattler` is pre-1.0 and its API moves. The provisioner calls it
  through one adapter module, and the version is pinned in codecalc's own lockfile.
- **Package trust.** conda-forge per-package signatures are not served yet, so trust rests on the
  signed manifest and sha256 checks. Adopt package attestations when conda-forge serves them.
- **Sigstore on clients.** Verifying in Python adds a dependency. If it is too heavy, the fallback
  is checksum files signed by the release key, verified with a vendored public key.
- **Who confirms MCP-initiated applies?** Problem 4 (elicitation cancelled in some clients) still
  affects the MCP `update_runtimes` tool. The CLI path does not depend on it; the MCP tool may
  become report-only once the background updater exists.
