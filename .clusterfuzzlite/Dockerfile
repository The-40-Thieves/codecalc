# ClusterFuzzLite build image (THE-899 follow-up: coverage-guided discovery
# to complement scripts/fuzz.py's deterministic seeded-mutation gate — see
# fuzz/README.md for the split between the two).
#
# Standard Python OSS-Fuzz/ClusterFuzzLite base image (context7
# /google/clusterfuzzlite docs, "Python Dockerfile for ClusterFuzzLite"):
# atheris and pyinstaller ship pre-installed in this image.
#
# No tag/digest pin (hadolint DL3006), deliberately: this base image is
# OSS-Fuzz's own rolling build environment — the whole point of using it is
# that its sanitizer runtime, libFuzzer and toolchain stay in lockstep with
# what ClusterFuzzLite's actions expect THIS WEEK. Pinning it to a digest
# would trade "matches upstream" for "silently drifts from upstream", the
# opposite of what a pin buys anywhere else in this repo (contrast
# docker/executor-image.lock, which digest-pins an image THIS repo builds
# and controls).
# hadolint ignore=DL3006
FROM gcr.io/oss-fuzz-base/base-builder-python

COPY . $SRC/codecalc
WORKDIR $SRC/codecalc

# scripts/fuzz.py (NOT part of the installed `codecalc` package — it is a
# repo-local script) holds the seed corpus and the pass/fail contract every
# harness in fuzz/ reuses. Putting it on PYTHONPATH, rather than copying its
# corpus into fuzz/, is what lets `import fuzz` resolve both for pyinstaller's
# OWN build-time import analysis (it runs as this same interpreter, so it
# honors PYTHONPATH like any other import) and for the frozen fuzzer binary
# at runtime — see fuzz/README.md for why this matters (a copy of the corpus
# that can drift from scripts/fuzz.py's is a defect, not a convenience).
ENV PYTHONPATH="$SRC/codecalc:$SRC/codecalc/scripts"

COPY .clusterfuzzlite/build.sh $SRC/
