# Design: self-sustaining, self-updating language runtimes

Written 2026-10-27 against `main` at `53aeb9d`. Revised the same day, twice: the provisioning engine
changed from an isolated mise to rattler and conda-forge, then a deeper research pass hardened the
trust model, the data directory, the updater's concurrency and the per-language sources (see
"Alternatives considered").

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
- musl hosts (Alpine). conda-forge builds target glibc; musl users get host runtimes only.

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
Managed runtimes are always run by absolute path through `current.json`, never through `PATH`.

The data directory must be short and contain no spaces. Conda packages carry their install path
inside text and binary files, and rattler rewrites shebangs to `#!/usr/bin/env` when the prefix
contains a space or a shebang grows past 127 bytes (Linux) or 512 bytes (macOS). So:

- macOS and Linux: `$XDG_DATA_HOME/codecalc` (default `~/.local/share/codecalc`). macOS does *not*
  use `~/Library/Application Support`, which contains a space.
- Windows: `%LOCALAPPDATA%\codecalc`, with short subdirectory names. A username containing a space
  is a tested case (Phase 2).
- `CODECALC_DATA_DIR` overrides the location everywhere.
- codecalc refuses to install managed runtimes on a `noexec` mount and says why.

Layout:

```text
<data>/
  pkgs/                      shared package cache; prefixes hardlink from it
  trash/                     files Windows would not let GC delete; retried at next start
  update.lock                cross-process lock held for a whole update
  runtimes/
    python/3.15.2-1a2b3c4d/  one immutable prefix per version, named <version>-<sha8>
    python/3.15.1-9f8e7d6c/  previous version, kept for rollback
    current.json             language -> active prefix, swapped atomically
    current.json.prev        the pointer before the last swap
    manifest.json            the verified manifest this install follows
    state.json               last check, highest manifest serial seen, failures
```

The `<sha8>` suffix (from the manifest entry's hash) keeps a rebuild of the same version from
colliding with the installed one. Prefixes are written once, at their final path, and never
modified after the pointer moves to them. Where hardlinks from `pkgs/` cannot work (a
cross-device data directory), rattler falls back to copying.

### 2. Provisioning: three tiers in one manifest

Every managed language comes from exactly one tier per target. The tier is recorded in the
manifest, so the client never guesses.

**Tier 1: conda packages through rattler.** codecalc embeds
[`py-rattler`](https://github.com/conda/rattler), the Rust installer behind pixi, as an ordinary
pip dependency. It ships abi3 wheels for Linux (glibc and musl hosts), macOS and Windows on x86_64
and arm64. conda-forge is the only project building portable, relocatable binaries for most of
these languages; Modular's `max` channel serves Mojo the same way.

- CI owns solving. A `pixi.toml` in the repo declares one environment per language; CI solves it
  into `pixi.lock`, which pins every package *and every dependency* by URL and sha256 per platform.
- The client never solves. It hands rattler's `install()` the explicit package list for its
  platform from the manifest. This also avoids downloading repodata (the full win-64
  `repodata.json` is 418 MB) and the documented conda-forge repodata stalls.
- Channels are pinned explicitly in the lock (rattler now follows CEP-42 channel relations by
  default). codecalc never resolves or falls back to Anaconda's `defaults` channel, which is paid;
  conda-forge itself is free to use commercially, under Anaconda's Terms of Service.
- Every package URL has mirrors, tried in order under the same sha256: `conda.anaconda.org`, then
  `repo.prefix.dev`.
- Link scripts and menuinst stay off. rattler's own docstring warns that link scripts run
  unsandboxed code.
- No external tool is fetched, nothing is put on the user's `PATH`, and nothing outside `<data>/`
  is written. The Phase 2 spike proves the last claim on each OS.

**Tier 2: checksummed vendor archives.** Where conda-forge has no build, a stale one, or none for
one target, the manifest lists the vendor's archive by URL and sha256. A small Python downloader
verifies and unpacks it into the same prefix layout. The entry format borrows aqua's registry
vocabulary (URL template, supported targets, checksum, minisign, sigstore and attestation stanzas),
but the pinned sha256 is always enforced; there is no trust-on-first-use.

**Tier 3: host only.** Anything left is detected on the host and never installed.

#### Source per language and target

Key: **CF** conda-forge (Tier 1), **MAX** Modular's channel (Tier 1), **V** vendor archive
(Tier 2), **H** host only (Tier 3), **—** unsupported. "(verify)" cells are confirmed or changed by
the Phase 2 spike.

| Language | linux-64 | linux-aarch64 | osx-64 | osx-arm64 | win-64 | win-arm64 |
|---|---|---|---|---|---|---|
| awk | CF gawk | CF | CF | CF | H (m2-gawk, verify) | H |
| bash | CF (5.2, lags) | CF | CF | CF | CF m2-bash (verify) / H | H |
| bun | CF | CF | CF | CF | CF | — (vendor, verify) |
| c / c++ | CF clang | CF clang | CF clang | CF clang | CF gcc/gxx (mingw) | CF zig cc |
| c# | CF dotnet | CF | CF | CF | CF | CF |
| deno | V | V | V | V | V | V |
| elixir | CF | CF | CF | CF | V (verify) | — |
| erlang | CF | CF | CF | CF | V otp_win64 | — |
| fortran | CF gfortran | CF | CF | CF | CF gfortran | CF flang (verify linker) |
| gleam | V + CF erlang | V + CF erlang | V + CF erlang | V + CF erlang | V + V erlang | V (JS target on CF node) |
| go | CF | CF | CF | CF | CF | CF |
| haskell | V ghcup bindist | V | V | V | V (verify) | — |
| java | V Temurin | V | V | V | V | — (verify) |
| jq | CF | CF | CF | CF | V | — (verify) |
| kotlin | CF + V JDK | CF + V JDK | CF + V JDK | CF + V JDK | CF + V JDK | — |
| lua | CF | CF | CF | CF | CF | CF |
| mojo | MAX | MAX | — | MAX | — | — |
| node | CF | CF | CF | CF | CF | CF |
| perl | CF | CF | CF | CF | CF | — |
| php | CF | CF | CF | CF | CF | — |
| python | CF | CF | CF | CF | CF | CF |
| r | CF r-base | CF | CF | CF | CF | V rig build |
| ruby | CF | CF | CF | CF | CF | V RubyInstaller2 |
| rust | CF | CF | CF | CF | CF | CF |
| sqlite | CF | CF | CF | CF | CF | CF |
| swift | V swift.org (verify) | V (verify) | H | V (verify) / H | H | — |
| tcl | CF (9.0, lags) | CF | CF | CF | CF | CF |
| typescript | V via deno | V | V | V | V | V |
| zig | CF | CF | CF | CF | CF | CF |
| zsh | CF | CF | CF | CF | — | — |

Notes on the choices:

- **C, C++, Fortran on Windows are Tier 1.** conda-forge's `clang_win-64` needs Visual Studio, but
  its mingw-w64 `gcc`/`gxx`/`gfortran` (win-64) and `zig cc` (win-arm64) need no admin rights.
  The trade-off is the GCC or Zig ABI, not MSVC's.
- **Vendor builds where conda-forge lags:** openjdk has had no upload since 2026-02-18 (Temurin
  instead); conda-forge typescript is on 6.x against upstream 7 (run TypeScript through Deno);
  deno trails by weeks and has no win-arm64 build, while Deno publishes a `.sha256sum` per asset.
  Gleam publishes sha256 and Sigstore per asset for all six targets.
- **Haskell:** conda-forge's ghc is 8.10.7 from 2021. ghcup bindists (`SHA256SUMS` plus `.sig`)
  replace it; Windows is unverified.
- **Python** stays on conda-forge, which tracks upstream within days. python-build-standalone is
  the drop-in if that ever changes.
- **win-arm64 is thin:** about 15 of 31 languages even with vendor fills.

### 3. The runtime manifest is the source of truth

Every codecalc release publishes `runtimes.lock.json`, generated from `pixi.lock` plus the Tier 2
entries:

```json
{
  "schema": 1,
  "serial": 42,
  "expires": "2026-12-01T00:00:00Z",
  "codecalc": "1.19.0",
  "generated": "2026-11-01T00:00:00Z",
  "mirrors": ["https://conda.anaconda.org", "https://repo.prefix.dev"],
  "languages": {
    "python3": {
      "tier": "tested",
      "targets": {
        "linux-64":  {"source": "conda-forge", "version": "3.15.2", "packages": [{"path": "…", "sha256": "…"}]},
        "osx-arm64": {"source": "conda-forge", "version": "3.15.2", "packages": [{"path": "…", "sha256": "…"}]},
        "win-64":    {"source": "conda-forge", "version": "3.15.2", "packages": [{"path": "…", "sha256": "…"}]}
      }
    },
    "swift": {
      "tier": "best_effort",
      "targets": {
        "linux-64":  {"source": "vendor", "version": "6.4.1", "url": "…", "sha256": "…"},
        "osx-arm64": {"source": "vendor", "version": "6.4.1", "url": "…", "sha256": "…"},
        "win-64":    {"source": "host"}
      }
    }
  }
}
```

Target names follow conda's platform names (`linux-64`, `linux-aarch64`, `osx-64`, `osx-arm64`,
`win-64`, `win-arm64`). Conda package paths are relative so each mirror can serve them.

Rules:

- A version enters the manifest only after the conformance suite passes on that target in CI.
  Clients update to the manifest, never to "whatever upstream released last night".
- Every downloaded file, in either tier, is checked against its sha256 before use. conda-forge
  does not publish package attestations, so these hashes are the whole integrity guarantee and the
  manifest's own security is load-bearing.
- The manifest is signed with Sigstore keyless signing from the release workflow (GitHub OIDC
  identity). Clients verify it with the `sigstore` Python library, offline, against a cached trust
  root refreshed on the 24-hour update cadence (by default the library checks for trust-root
  updates on every start, which a frequently started, sometimes offline server cannot rely on).
  Verification checks the workflow identity, repository and ref.
- A signature proves who signed a manifest, not that it is the newest. To stop an attacker or a
  stale CDN replaying an older signed manifest:
  - `serial` only goes up. The client stores the highest serial it has accepted and rejects lower.
  - `expires` bounds a manifest's life. An expired manifest freezes updates and warns; it never
    uninstalls anything.
  - The accepted signing identities are pinned in codecalc, and changing them needs a rotation
    record signed by the current identity.
- An unverifiable manifest is ignored, and the client keeps its current runtimes.
- The existing reliability tiers (`tested`, `best_effort`, `plan_only`; today 4, 25 and 1
  languages) carry over per target, so the published support table *is* the manifest.

### 4. The updater on the user's machine

No system timer is needed for the common case:

1. On server start, if more than 24 hours have passed since the last check, fetch the latest
   manifest in the background. Tool calls are never blocked on this.
2. Take `update.lock` (an OS file lock). Several MCP clients each starting codecalc is the normal
   case, and rustup's maintainers document how unlocked concurrent updates corrupt installs. The
   lock is held from download through pointer swap and cleanup. A second process that cannot get
   it skips this round.
3. Verify the manifest (section 3). For each language in use whose manifest entry differs from
   `current.json`, install the new version directly into its final `<version>-<sha8>` prefix.
   There is no staging directory: the path is baked into the files, and Windows cannot move a
   directory across drives. A language is installed the first time it is run.
4. Run the language's smoke test (the existing `doctor --deep` probe for that language).
5. Pass: copy `current.json` to `current.json.prev`, then swap `current.json` atomically (write a
   temp file, fsync, `os.replace`). Fail: delete the new prefix, keep the old version active, record
   the failure in `state.json`, and surface it in `codecalc doctor`. `current.json` is validated
   on load; if it is damaged, `current.json.prev` is used.
6. Keep one previous version per language. Delete older prefixes, then cache entries no prefix
   uses. On Windows a running executable cannot be deleted: anything that fails to delete moves to
   `trash/` and is retried at the next start.
7. If the OS refuses to run a managed binary (Windows Smart App Control or App Control policy,
   macOS policy), record it as a policy denial, fall back to the host runtime, and report it
   plainly. Never retry it in a loop.

Controls:

- `CODECALC_AUTO_UPDATE=0` turns the background updater off.
- `CODECALC_RUNTIMES=host` prefers host runtimes everywhere.
- `CODECALC_DATA_DIR` moves the data directory.
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
| Offline bundles | one `pixi-pack` archive per target, six per release (a pack only unpacks on the platform it was built for) |

### 7. The project maintains itself

1. A scheduled workflow runs `pixi update` and checks Tier 2 upstreams for new releases, then opens
   a PR bumping `pixi.lock` and the Tier 2 entries.
2. The conformance suite runs on the ubuntu, macOS and Windows runners (the matrix already exists),
   installing each runtime exactly as a client would.
3. Green: the PR auto-merges, and the release workflow cuts a release that publishes the signed
   manifest with the next `serial`.
4. Red: the workflow files an issue, and that language stays on its previous version.

This loop needs branch protection with required checks, because auto-merge on an unprotected
branch merges immediately. That is Phase 0, done on 2026-10-27: 42 required GitHub Actions checks,
repository auto-merge enabled, admins can still bypass.

## Alternatives considered

| Option | Verdict | Why |
|---|---|---|
| Isolated mise (this document's first draft) | Rejected | Many upstreams; Erlang and Elixir (and Ruby on musl) compile from source off Ubuntu, so users can run a binary CI never tested; checksums only where a backend provides them; a separate CLI to fetch and drive |
| aqua | Rejected as engine, adopted as the Tier 2 schema model | Best multi-tool registry (MIT, native Windows, cosign/SLSA/minisign stanzas), but checksum checks are off by default, it pins unchecksummed artifacts on first use, it has no entries for python-build-standalone, Erlang, Java, .NET, R, Swift or sqlite, and it is a Go CLI, not a library |
| micromamba | Fallback engine | Stable CLI on all six targets (2.10), but an extra binary per platform driven as a subprocess. Kept behind the same adapter in case py-rattler becomes unworkable |
| proto, pkgx, devbox, Flox, asdf, vfox | Rejected | Many upstreams, or no native Windows (pkgx experimental, asdf none, Nix-based tools) |
| Containers, micro-VMs (Podman, Apple `container`, libkrun) | Optional provider later | Windows needs admin once for WSL2 or Hyper-V; Apple `container` needs macOS 26 and an admin password; libkrun has no Windows |
| WebAssembly (WASI 0.3) | Optional provider later | About 8–11 of the 31 languages run fully under WASI (interpreters such as Python, Ruby, PHP, Lua, SQLite, jq); no practical path for the JVM, .NET, BEAM, shells or R. The VMware Wasm Labs interpreter builds are being archived |
| Remote execution (E2B, Cloudflare, Vercel, Deno Sandbox, Piston) | Optional provider later | Nothing works offline; Piston's public API closed to most users on 2026-02-15 |

The first draft also said mise leaks Rust into `~/.rustup` and `~/.cargo`. That was unfair:
rustup supports `RUSTUP_HOME` and `CARGO_HOME`. The other objections stand.

Checked on 2026-10-27 against anaconda.org, PyPI and GitHub: `py-rattler` 0.28.0 publishes
win_amd64, win_arm64 and musllinux wheels; conda-forge Python covers all six targets; conda-forge
Erlang, Elixir, jq, gawk, bash and zsh have no native win-64 build; ghc is 8.10.7; Swift is absent.
Full sources: the research report "Runtime provisioning for codecalc" (kept outside the repo).

## Phases

| # | Pull request | Unblocks |
|---|---|---|
| 0 | Protect `main` with required checks; enable repository auto-merge. **Done 2026-10-27.** | every automated merge |
| 1 | `codecalc runtimes` CLI. Per-OS host detection (brew, winget, scoop, dnf, pacman, apt) that reports and suggests, never mutates | users on every OS see their real status; unattended apply |
| 2 | Spike first (below), then the data directory, update lock, rattler (Tier 1) and vendor (Tier 2) provisioners behind one adapter, and execution preferring managed runtimes | codecalc stops depending on the host |
| 3 | Manifest generation from `pixi.lock`, Sigstore signing, offline verification with serial, expiry and identity rotation; conformance suite across 3 OSes | safe target versions |
| 4 | Background updater: lock, install, smoke-test, swap, roll back, Windows trash GC, policy-denial reporting; `schedule --install` | self-updating on user machines |
| 5 | Scheduled bump workflow and auto-merge, release workflow publishing the manifest and six offline bundles, Homebrew, winget and Scoop manifests | self-sustaining releases |

### Phase 2 spike (about one day)

- A `pixi.toml` with one environment per language, solved for linux-aarch64, osx-arm64, win-64
  and win-arm64, plus the Tier 2 entries for those targets.
- A throwaway installer that installs each environment with `py-rattler` from the lock (and each
  vendor archive with the downloader), then runs the language's hello-world.
- A GitHub Actions run on ubuntu-24.04-arm, macos-latest, windows-latest and windows-11-arm that
  records pass/fail, install time and disk use per language, records the Authenticode status of
  every Windows executable, and diffs the filesystem to confirm nothing was written outside the
  data directory.
- Extra cells: a Windows user profile whose name contains a space; a deep-path install (.NET, the
  JDK, R) on Windows; a Windows 11 image with Smart App Control enforced; two updaters started at
  once against the same data directory.

Decision gate: at least 20 of 22 sampled language/OS cells pass on linux, macOS and win-64; every
extra cell behaves as designed (installs, or fails with a reported reason and no corruption); no
writes outside the data directory. win-arm64 is scored and published separately so its thin
coverage cannot hide inside the threshold. A failed gate sends this design back for revision
before any provisioner code is written. The spike's output resolves every "(verify)" cell in
section 2.

## Risks and open questions

- **Windows code-integrity policy (highest risk).** Smart App Control checks every executable and
  library the OS loads, whatever its origin. It allows unsigned code only if Microsoft's reputation
  service knows it, and it has blocked unsigned developer tools after updates. App Control for
  Business and AppLocker can block execution from `%LOCALAPPDATA%` outright. Nobody has checked
  whether conda-forge's Windows builds are Authenticode-signed; if they are not and SAC blocks
  them, Windows cells move to signed vendor builds. Phase 2 measures this.
- **py-rattler churn.** It is pre-1.0, with 42 releases since 2023 and breaking changes in almost
  every minor release, some silent (build strings became `BuildString` objects, so
  `record.build == "py312_0"` is now always False). A 0.27.5 patch broke Windows extraction of deep
  .NET paths. Mitigation: pin it exactly, call it only through one adapter, give the adapter
  contract tests for silent semantic changes, make a Windows deep-tree install (.NET, JDK, R) a
  required check for any bump, and keep micromamba as the fallback engine behind the adapter.
- **Windows coverage is uneven, and win-arm64 is thin** (about 15 of 31). Phase 2 publishes the
  measured table before anything is promised.
- **conda-forge lag.** Most packages track upstream within days; some do not (openjdk, typescript,
  bash, tcl). The bump workflow surfaces lag, and a language that falls too far behind moves to
  Tier 2.
- **conda-forge availability.** Repodata downloads have stalled since late 2025. The client never
  downloads repodata and has a second mirror.
- **Package trust.** conda-forge publishes no package attestations, so trust rests on the signed
  manifest and sha256 checks. Turn on rattler's `attestation_policy` for channels that publish
  attestations, and for conda-forge if it starts.
- **Disk use.** Each runtime bundles its own libraries, so the full set can reach several
  gigabytes. Mitigated by installing on first use, hardlinking from the shared cache and keeping
  one rollback version.
- **Network-restricted environments.** The updater must fail quietly and keep working offline;
  `CODECALC_AUTO_UPDATE=0` and the offline bundles must be documented for air-gapped installs.
- **Sigstore on clients.** Verifying in Python adds a dependency. If it is too heavy, the fallback
  is checksum files signed by the release key, verified with a vendored public key; serial and
  expiry still apply.
- **Who confirms MCP-initiated applies?** Problem 4 (elicitation cancelled in some clients) still
  affects the MCP `update_runtimes` tool. The CLI path does not depend on it; the MCP tool may
  become report-only once the background updater exists.
