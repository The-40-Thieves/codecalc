"""Published-image digest lock + fail-closed execution path (THE-828).

The production strict EXECUTION path is digest-required: it pins the executor
image `ghcr.io/the-40-thieves/codecalc-exec` by an immutable `@sha256:` digest,
written into `docker/executor-image.lock` by the publish-executor-image
workflow. Until that workflow is dispatched the lock holds only a placeholder,
and the execution path must FAIL CLOSED rather than fall back to the mutable
local diagnostic tag (`codecalc-exec:strict`). The diagnostic path — `doctor`,
the conformance canary — keeps using the local tag and is unaffected.

Pure logic over a temp lock file and injected environment mappings: no Docker,
no sandbox, no native executor, so it runs on the full OS matrix.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from codecalc.strict_runtime import (
    DEFAULT_STRICT_IMAGE,
    STRICT_IMAGE_ENV,
    STRICT_IMAGE_LOCK_ENV,
    GVisorConfig,
    StrictImageUnavailable,
    published_strict_image,
    strict_execution_config,
    strict_image,
)

DIGEST = "ghcr.io/the-40-thieves/codecalc-exec@sha256:" + "a" * 64
MUTABLE = "ghcr.io/the-40-thieves/codecalc-exec:strict"
FAILS: list[str] = []


def check(name: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL':4} {name}")
    if not condition:
        FAILS.append(name)


def expect_raises(name: str, error: type[Exception], match: str, operation) -> None:
    try:
        operation()
    except error as exc:
        if match not in str(exc):
            FAILS.append(f"{name}: wrong message: {exc}")
    else:
        FAILS.append(f"{name}: did not raise {error.__name__}")


def _env_pointing_at(text: str | None) -> tuple[dict[str, str], object]:
    """An environment mapping whose lock override points at a temp file (or a
    path that does not exist when `text` is None), returned with the tempdir so
    the caller keeps it alive for the duration of the assertion."""
    tmp = tempfile.TemporaryDirectory()
    lock = Path(tmp.name) / "executor-image.lock"
    if text is not None:
        lock.write_text(text, encoding="utf-8")
    return {STRICT_IMAGE_LOCK_ENV: str(lock)}, tmp


def test_lock_with_a_real_digest_resolves_to_it() -> None:
    env, tmp = _env_pointing_at(
        "# header comment, ignored\n\n" + DIGEST + "\n"
    )
    with tmp:
        check("a digest in the lock file is resolved",
              published_strict_image(env) == DIGEST)


def test_placeholder_lock_resolves_to_no_published_image() -> None:
    env, tmp = _env_pointing_at(
        "# not published yet — the workflow rewrites the line below\nunpublished\n"
    )
    with tmp:
        check("a placeholder lock yields no published image",
              published_strict_image(env) is None)


def test_absent_lock_resolves_to_no_published_image() -> None:
    env, tmp = _env_pointing_at(None)
    with tmp:
        check("an absent lock yields no published image",
              published_strict_image(env) is None)


def test_env_digest_is_used_when_the_lock_has_none() -> None:
    env, tmp = _env_pointing_at(None)
    env[STRICT_IMAGE_ENV] = DIGEST
    with tmp:
        check("a digest-pinned CODECALC_STRICT_IMAGE is honoured as a fallback",
              published_strict_image(env) == DIGEST)


def test_env_mutable_tag_is_never_used_for_the_execution_path() -> None:
    env, tmp = _env_pointing_at(None)
    env[STRICT_IMAGE_ENV] = MUTABLE
    with tmp:
        check("a MUTABLE CODECALC_STRICT_IMAGE is refused as a published image",
              published_strict_image(env) is None)


def test_lock_digest_wins_over_a_mutable_env_tag() -> None:
    env, tmp = _env_pointing_at(DIGEST + "\n")
    env[STRICT_IMAGE_ENV] = MUTABLE
    with tmp:
        check("the committed digest is preferred over a mutable env override",
              published_strict_image(env) == DIGEST)


def test_execution_config_uses_the_published_digest() -> None:
    env, tmp = _env_pointing_at(DIGEST + "\n")
    with tmp:
        config = strict_execution_config(env, tmpfs_mb=32)
        check("the execution config pins the published digest", config.image == DIGEST)
        check("execution-config overrides are applied", config.tmpfs_mb == 32)


def test_execution_config_fails_closed_when_nothing_is_published() -> None:
    env, tmp = _env_pointing_at("unpublished\n")
    with tmp:
        expect_raises(
            "no published image fails closed", StrictImageUnavailable,
            "publish-executor-image",
            lambda: strict_execution_config(env),
        )


def test_execution_config_never_falls_back_to_the_mutable_local_tag() -> None:
    env, tmp = _env_pointing_at(None)
    with tmp:
        # The local diagnostic tag exists and is mutable; the execution path must
        # refuse it rather than silently pin it.
        try:
            config = strict_execution_config(env)
        except StrictImageUnavailable:
            check("execution path does not pin the mutable local tag", True)
        else:
            check("execution path does not pin the mutable local tag",
                  config.image != DEFAULT_STRICT_IMAGE and False)


def test_gvisorconfig_accepts_a_ghcr_digest_and_rejects_a_ghcr_mutable_tag() -> None:
    check("GVisorConfig accepts a well-formed ghcr digest ref",
          GVisorConfig(image=DIGEST).image == DIGEST)
    expect_raises(
        "ghcr mutable tag", ValueError, "digest-pinned",
        lambda: GVisorConfig(image=MUTABLE),
    )


def test_diagnostic_tag_path_is_unchanged() -> None:
    # strict_image() still resolves the LOCAL diagnostic tag with no digest
    # requirement — doctor and the conformance canary depend on this.
    check("diagnostic strict_image defaults to the local tag",
          strict_image({}) == DEFAULT_STRICT_IMAGE)
    check("diagnostic strict_image honours a mutable env override",
          strict_image({STRICT_IMAGE_ENV: MUTABLE}) == MUTABLE)


def test_committed_lock_is_a_placeholder_so_the_repo_ships_fail_closed() -> None:
    # The shipped state: no digest is pinned yet, so a caller reading the real
    # committed lock (empty environment → default lock path) gets None and the
    # execution path fails closed. The first `workflow_dispatch` replaces this.
    check("the committed lock ships unpublished (execution path fails closed today)",
          published_strict_image({}) is None)


if __name__ == "__main__":
    test_lock_with_a_real_digest_resolves_to_it()
    test_placeholder_lock_resolves_to_no_published_image()
    test_absent_lock_resolves_to_no_published_image()
    test_env_digest_is_used_when_the_lock_has_none()
    test_env_mutable_tag_is_never_used_for_the_execution_path()
    test_lock_digest_wins_over_a_mutable_env_tag()
    test_execution_config_uses_the_published_digest()
    test_execution_config_fails_closed_when_nothing_is_published()
    test_execution_config_never_falls_back_to_the_mutable_local_tag()
    test_gvisorconfig_accepts_a_ghcr_digest_and_rejects_a_ghcr_mutable_tag()
    test_diagnostic_tag_path_is_unchanged()
    test_committed_lock_is_a_placeholder_so_the_repo_ships_fail_closed()
    for failure in FAILS:
        print(f"FAIL {failure}")
    raise SystemExit(1 if FAILS else 0)
