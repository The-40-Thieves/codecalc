//! Library face of the executor crate.
//!
//! The binary (`main.rs`) owns process spawning and the platform backends. Only the
//! pure helpers are exported here, so the cargo-fuzz targets in `fuzz/` can exercise
//! them without restructuring the binary.

pub mod bounds;
