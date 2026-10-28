//! Pure bounding helpers for the executor: output capping and the run-timeout remainder.
//!
//! These live in the library target (not main.rs) so the cargo-fuzz targets under
//! `executor/fuzz/` can reach them. They take and return plain values and touch no
//! process, file, or clock state.

/// Appended, outside the budget, to each stream the cap cut. The Python backends
/// spell theirs "\n…[truncated]"; both spellings are documented in the README.
pub const TRUNCATION_MARKER: &str = "\n...[truncated]";

/// Truncate `buf` to `cap` bytes and append the marker when it is over the cap.
///
/// Returns `true` when output was cut. `buf` is left untouched when it fits.
pub fn cap_output(buf: &mut Vec<u8>, cap: u64) -> bool {
    let truncated = buf.len() as u64 > cap;
    if truncated {
        buf.truncate(cap as usize);
        buf.extend_from_slice(TRUNCATION_MARKER.as_bytes());
    }
    truncated
}

/// Apply ONE combined stdout+stderr budget of `cap` bytes to two streams.
///
/// Returns the byte budget each stream may keep and whether anything was cut.
/// Truncated iff `out_len + err_len > cap`; exactly `cap` is not. When it is,
/// each stream is guaranteed `min(its own length, cap / 2)` and whatever one
/// stream leaves unused goes to the other, so a stdout flood cannot push a short
/// stderr traceback out of the result. Mirrors `split_output_budget` in
/// codecalc/executor.py; the two must not drift.
///
/// The budgets are not final lengths: `keep_stream` cuts each kept slice back
/// to a UTF-8 character boundary, which can leave it up to three bytes short.
/// Pure: the answer depends only on the arguments.
pub fn split_output_budget(cap: u64, out_len: u64, err_len: u64) -> (u64, u64, bool) {
    if out_len.saturating_add(err_len) <= cap {
        return (out_len, err_len, false);
    }
    let share = cap / 2;
    let mut out_keep = out_len.min(share);
    let mut err_keep = err_len.min(share);
    let mut spare = cap - out_keep - err_keep;
    let grow = spare.min(out_len - out_keep);
    out_keep += grow;
    spare -= grow;
    err_keep += spare.min(err_len - err_keep);
    (out_keep, err_keep, true)
}

/// Length of the longest prefix of `data`, at most `keep` bytes, that does not
/// end inside a multibyte UTF-8 character. A character is at most four bytes,
/// so the back-off is at most three bytes. Bytes that are not UTF-8 are kept as
/// they are; the back-off only moves the cut, it never drops bytes.
pub fn cut_utf8(data: &[u8], keep: usize) -> usize {
    if keep >= data.len() {
        return data.len();
    }
    let mut cut = keep;
    while cut > 0 && keep - cut < 3 && (0x80..=0xBF).contains(&data[cut]) {
        cut -= 1;
    }
    cut
}

/// Render one captured stream under its `budget`. A stream within its budget is
/// returned whole; a cut one is cut on a character boundary and ends in
/// `TRUNCATION_MARKER`, outside the budget.
pub fn keep_stream(buf: &[u8], budget: usize) -> String {
    if buf.len() <= budget {
        return String::from_utf8_lossy(buf).into_owned();
    }
    let cut = cut_utf8(buf, budget);
    let mut text = String::from_utf8_lossy(&buf[..cut]).into_owned();
    text.push_str(TRUNCATION_MARKER);
    text
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

#[cfg(test)]
mod split_tests {
    use super::*;

    // Same cases as the Python split_output_budget tests (GH #354). The fair
    // split is the contract: a stream is guaranteed min(its length, cap/2) and
    // the other stream gets whatever is left.

    #[test]
    fn stdout_alone_over_the_cap_keeps_cap_bytes() {
        assert_eq!(split_output_budget(10, 15, 0), (10, 0, true));
    }

    #[test]
    fn stderr_alone_over_the_cap_keeps_cap_bytes() {
        assert_eq!(split_output_budget(10, 0, 15), (0, 10, true));
    }

    #[test]
    fn mixed_streams_under_the_cap_are_kept_whole() {
        assert_eq!(split_output_budget(10, 4, 3), (4, 3, false));
    }

    #[test]
    fn each_stream_under_the_cap_but_combined_over_is_truncated_fairly() {
        assert_eq!(split_output_budget(10, 6, 6), (5, 5, true));
    }

    #[test]
    fn exactly_the_cap_combined_is_not_truncated() {
        assert_eq!(split_output_budget(10, 5, 5), (5, 5, false));
        assert_eq!(split_output_budget(10, 10, 0), (10, 0, false));
    }

    #[test]
    fn one_byte_over_the_combined_cap_is_truncated() {
        assert!(split_output_budget(10, 6, 5).2);
    }

    #[test]
    fn fair_split_keeps_a_short_stderr_whole_after_a_stdout_flood() {
        let traceback =
            b"Traceback (most recent call last):\n  File \"main.py\", line 2\nValueError: boom\n";
        let (keep_out, keep_err, truncated) =
            split_output_budget(1024, 10_240, traceback.len() as u64);
        assert!(truncated);
        assert_eq!(keep_err, traceback.len() as u64);
        assert_eq!(keep_out, 1024 - traceback.len() as u64);
    }

    #[test]
    fn multibyte_character_straddling_the_cut_is_not_split() {
        // "éé" is 4 bytes; a 5-byte budget lands inside the third "é".
        let data = "é".repeat(10);
        let kept = keep_stream(data.as_bytes(), 5);
        assert_eq!(kept, format!("éé{TRUNCATION_MARKER}"));
    }

    #[test]
    fn a_stream_within_its_budget_gets_no_marker() {
        assert_eq!(keep_stream(b"abc", 3), "abc");
    }

    #[test]
    fn a_cut_stream_gets_exactly_one_marker() {
        assert_eq!(keep_stream(b"abcdef", 2), format!("ab{TRUNCATION_MARKER}"));
    }
}
