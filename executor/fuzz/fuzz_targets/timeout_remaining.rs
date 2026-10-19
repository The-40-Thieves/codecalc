#![no_main]

use codecalc_exec::bounds::remaining_run_timeout_secs;
use libfuzzer_sys::fuzz_target;

// Input layout: budget_secs (8 bytes, little-endian u64), then elapsed_ms (8 bytes, little-endian u64).
fuzz_target!(|data: &[u8]| {
    if data.len() < 16 {
        return;
    }
    let budget_secs = u64::from_le_bytes(data[..8].try_into().unwrap());
    let elapsed_ms = u64::from_le_bytes(data[8..16].try_into().unwrap());

    // Oracle in u128 so nothing can overflow. The budget is clamped to u64::MAX
    // milliseconds, exactly as the function's saturating_mul does.
    let budget_ms = (budget_secs as u128 * 1000).min(u64::MAX as u128);
    let elapsed = elapsed_ms as u128;
    let expected = if elapsed >= budget_ms {
        None
    } else {
        Some((budget_ms - elapsed).div_ceil(1000) as u64)
    };

    assert_eq!(
        remaining_run_timeout_secs(budget_secs, elapsed_ms),
        expected
    );
});
