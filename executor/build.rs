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

use std::{env, path::PathBuf, process::Command};

fn main() {
    println!("cargo:rerun-if-changed=blocknet.c");
    println!("cargo:rerun-if-changed=build.rs");

    let target_os = env::var("CARGO_CFG_TARGET_OS").unwrap_or_default();
    if target_os == "windows" {
        return;
    }
    let lib = if target_os == "macos" { "blocknet.dylib" } else { "blocknet.so" };

    // OUT_DIR is target/<profile>/build/<pkg>-<hash>/out; the executable lands
    // in target/<profile>, which is where it looks for the shim at run time
    // (`current_exe().parent()`). Three levels up is that directory.
    let out_dir = PathBuf::from(env::var("OUT_DIR").expect("OUT_DIR"));
    let exe_dir = out_dir
        .ancestors()
        .nth(3)
        .expect("OUT_DIR has the documented depth")
        .to_path_buf();

    // Same flags as the CI gate, -Werror included: a shim that only compiles
    // with warnings is a shim nobody has looked at.
    let cc = env::var("CC").unwrap_or_else(|_| "cc".to_string());
    let status = Command::new(&cc)
        .args(["-shared", "-fPIC", "-O2", "-Wall", "-Wextra", "-Werror", "-o"])
        .arg(exe_dir.join(lib))
        .arg("blocknet.c")
        .status();

    match status {
        Ok(s) if s.success() => {}
        // Do NOT fail the build: the executor runs fine without the shim and
        // says so, whereas a hard failure would make a missing C compiler block
        // every other language. Warn loudly instead.
        Ok(s) => println!("cargo:warning=blocknet shim failed to build ({s}); --no-net will report itself unenforced"),
        Err(e) => println!("cargo:warning=could not run {cc} to build the blocknet shim ({e}); --no-net will report itself unenforced"),
    }
}
