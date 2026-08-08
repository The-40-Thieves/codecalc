//! Build the `--no-net` shim as part of `cargo build`.
//!
//! It used to be built by hand, from a comment in blocknet.c. That is a silent
//! staleness trap and it fired during the 2026-08-08 sweep: blocknet.c was
//! edited to stop blocking AF_UNIX, the source read correctly, the static
//! symbol checks passed — and the executor went on blocking AF_UNIX, because
//! the .so sitting next to the binary was the previous build.
//!
//! A MISSING shim is already reported honestly (the executor emits
//! `no_net_requested_but_no_shim_available` in `unenforced`). A STALE one
//! cannot be: the file is there, so every check that asks "is the shim
//! present?" says yes while the policy being enforced is the old one. Building
//! it here removes the gap rather than adding another check for it.
//!
//! Windows has no LD_PRELOAD equivalent, so there is nothing to build; the
//! executor reports `--no-net` as unenforced there instead of pretending.

use std::{env, fs, path::PathBuf, process::Command};

fn main() {
    println!("cargo:rerun-if-changed=blocknet.c");
    println!("cargo:rerun-if-changed=build.rs");

    let target_os = env::var("CARGO_CFG_TARGET_OS").unwrap_or_default();
    if target_os == "windows" {
        return;
    }
    let lib = if target_os == "macos" { "blocknet.dylib" } else { "blocknet.so" };

    // OUT_DIR is the only location cargo actually promises a build script may
    // write to, so the shim is BUILT there.
    let out_dir = PathBuf::from(env::var("OUT_DIR").expect("OUT_DIR"));
    let built = out_dir.join(lib);

    // Same flags as the CI gate, -Werror included: a shim that only compiles
    // with warnings is a shim nobody has looked at.
    let cc = env::var("CC").unwrap_or_else(|_| "cc".to_string());
    let status = Command::new(&cc)
        .args(["-shared", "-fPIC", "-O2", "-Wall", "-Wextra", "-Werror", "-o"])
        .arg(&built)
        .arg("blocknet.c")
        .status();

    match status {
        Ok(s) if s.success() => {}
        // Do NOT fail the build: the executor runs fine without the shim and
        // says so (`no_net_requested_but_no_shim_available` in `unenforced`),
        // whereas a hard failure would make a missing C compiler block every
        // other language. Warn loudly instead.
        Ok(s) => {
            println!("cargo:warning=blocknet shim failed to build ({s}); --no-net will report itself unenforced");
            return;
        }
        Err(e) => {
            println!("cargo:warning=could not run {cc} to build the blocknet shim ({e}); --no-net will report itself unenforced");
            return;
        }
    }

    // Convenience only: put a copy beside the built executable so `cargo build`
    // followed by `target/<profile>/codecalc-exec --no-net` works, since the
    // executor looks for the shim next to `current_exe()`.
    //
    // Located by walking up to the ancestor NAMED after the profile rather than
    // by counting levels — OUT_DIR's depth is an implementation detail, and a
    // hard-coded `nth(3)` silently lands somewhere else the day it changes.
    //
    // This does NOT make installation safe on its own. Copying only the
    // executable into `bin/` leaves whatever shim was there before, which is
    // exactly the staleness this file exists to prevent — so anything that
    // installs the binary must move the matching shim WITH it, and
    // tests/test_executor_sweep.py asserts the installed shim is not older
    // than blocknet.c.
    let profile = env::var("PROFILE").unwrap_or_default();
    if let Some(exe_dir) = out_dir
        .ancestors()
        .find(|a| a.file_name().map(|n| n == profile.as_str()).unwrap_or(false))
    {
        if let Err(e) = fs::copy(&built, exe_dir.join(lib)) {
            println!("cargo:warning=built the blocknet shim but could not place it beside the executable ({e})");
        }
    }
}
