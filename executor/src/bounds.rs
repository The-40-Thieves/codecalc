//! Pure bounding helpers for the executor: output capping and the run-timeout remainder.
//!
//! These live in the library target (not main.rs) so the cargo-fuzz targets under
//! `executor/fuzz/` can reach them. They take and return plain values and touch no
//! process, file, or clock state.

/// Truncate `buf` to `cap` bytes and append the marker when it is over the cap.
///
/// Returns `true` when output was cut. `buf` is left untouched when it fits.
pub fn cap_output(buf: &mut Vec<u8>, cap: u64) -> bool {
    let truncated = buf.len() as u64 > cap;
    if truncated {
        buf.truncate(cap as usize);
        buf.extend_from_slice(b"\n...[truncated]");
    }
    truncated
}

/// How many whole seconds remain in the wall-clock budget for the run step,
/// given how long compiling already took. `elapsed_ms` is `started.elapsed()`
/// captured right after the compile step returns, at millisecond precision —
/// not a floored `compile_ms / 1000`, which is what let a 9,900ms compile get
/// charged as spending only 9 whole seconds. Returns None when nothing is
/// left — compiling alone met or exceeded the budget — meaning the run must
/// not be attempted at all: the old code's unconditional `.max(1)` handed the
/// run step a guaranteed extra second regardless of the deficit, so
/// `--timeout 10` could take up to ~11.5s end to end.
pub fn remaining_run_timeout_secs(budget_secs: u64, elapsed_ms: u64) -> Option<u64> {
    let budget_ms = budget_secs.saturating_mul(1000);
    if elapsed_ms >= budget_ms {
        return None;
    }
    // spawn_and_wait's kill timer only understands whole seconds
    // (Duration::from_secs in platform::spawn_and_wait), so any genuinely
    // positive remainder is rounded UP to the smallest unit it supports
    // rather than floored to zero — a compile that "just barely" fits inside
    // the budget still gets to attempt the run. The guard above is what
    // confines that rounding-up to cases where time truly remains.
    Some((budget_ms - elapsed_ms).div_ceil(1000))
}
