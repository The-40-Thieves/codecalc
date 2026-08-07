#!/bin/sh
# Zig-as-C linker wrapper for x86_64 musl cross-builds (host is ARM64, no
# x86_64 GCC present). cargo passes raw linker flags; zig needs `cc` plus an
# explicit target since it would otherwise default to the host arch.
exec /data/tools/mise/shims/zig cc -target x86_64-linux-musl "$@"
