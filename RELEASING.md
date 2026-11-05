# Releasing codecalc

The org convention (see the organization's CONTRIBUTING): semver tags `vX.Y.Z`, one GitHub Release per tag, and the Release body is the changelog excerpt for that version. The product page at <https://the40thieves.dev/codecalc/> reads the latest GitHub Release, so a tag without a Release does not appear there.

## Steps

1. Bump `version` in `pyproject.toml` and add the `## X.Y.Z - YYYY-MM-DD` section to `CHANGELOG.md`. Land that through a pull request like any other change.
2. Tag the merge commit on `main` and push the tag:

   ```bash
   git tag -s vX.Y.Z -m "codecalc vX.Y.Z"
   git push origin vX.Y.Z
   ```

3. `release.yml` builds the wheels and binaries for the tag, verifies each wheel in a clean environment, and creates the GitHub Release with the artifacts and checksums attached.
4. Edit the Release body so it is the `CHANGELOG.md` section for this version, nothing else. The hub shows the first paragraph of it.
5. Confirm the PyPI publish finished and `uvx 'codecalc[full]' --version` reports the new version.

## What the hub reads

`hub.json` at the repository root (tagline, one measured claim with its source and date, install snippets per client, docs path). Update the claim's `measured_on` date whenever the number behind it is re-measured; the hub build rejects placeholder copy.
