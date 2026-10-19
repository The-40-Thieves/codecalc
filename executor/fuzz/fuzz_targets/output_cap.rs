#![no_main]

use codecalc_exec::bounds::cap_output;
use libfuzzer_sys::fuzz_target;

const MARKER: &[u8] = b"\n...[truncated]";

// Input layout: the first 8 bytes are the cap (little-endian u64), the rest is the output.
fuzz_target!(|data: &[u8]| {
    if data.len() < 8 {
        return;
    }
    let (head, body) = data.split_at(8);
    let cap = u64::from_le_bytes(head.try_into().unwrap());

    let mut buf = body.to_vec();
    let truncated = cap_output(&mut buf, cap);

    if truncated {
        let keep = cap as usize;
        assert!(
            body.len() as u64 > cap,
            "truncated although output fits the cap"
        );
        assert_eq!(
            buf.len(),
            keep + MARKER.len(),
            "cut length is not cap + marker"
        );
        assert_eq!(
            &buf[..keep],
            &body[..keep],
            "kept prefix differs from the output"
        );
        assert!(buf.ends_with(MARKER), "marker missing after truncation");
    } else {
        assert!(
            body.len() as u64 <= cap,
            "output over the cap was not truncated"
        );
        assert_eq!(buf, body, "output that fits the cap was modified");
    }
});
