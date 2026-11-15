# Matching release artifacts

A maintainer builds the MIPS relay using the compatible stock rootfs (never bundled),
then prepares the only release ZIP: `.soloify/q2-soloify`, `run`, `aplay.sh`, `VERSION`.
It contains no `remote`, Soloist, user settings, session or credentials. Runtime card
export adds a private `remote` for each installation.

```sh
nix develop
python relay/build.py --rootfs ../q2-pod/build-m1/stock.squashfs
python tools/release.py --write-bootstrap
python -m build --wheel --no-isolation --outdir build/wheel
python tools/release.py --wheel build/wheel/soloify-0.0.1-py3-none-any.whl --relay build/relay/q2-soloify
```

Versions in `pyproject.toml`, `soloify/__init__.py`, `setup_bootstrap.py`, card VERSION,
wheel and image tag must match. `soloify-setup.py` is generated verbatim from the
standard-library bootstrap module. Run the release tests after regeneration. The
builder rejects host binaries, wrong MIPS ABI, mismatched versions and extra files.
`build/release/SHA256SUMS` covers the script, wheel, card ZIP and release metadata.
The bootstrap fetches the version-matched wheel/card over HTTPS and checks SHA-256
before using them. System prerequisites are never installed by the launcher.

Create a **draft** release tagged `vVERSION` and upload the maintainer-built card ZIP.
Run the “Publish matching Soloify artifacts” workflow with that tag. Use purpose
`walkthrough` for the first testing release: it publishes a prerelease so fresh users
can test the real downloads, container and setup wizard before physical walkthroughs
are complete. Keep `walkthroughs_verified` false until the matrix below actually passes.
Use purpose `validated` and confirm `walkthroughs_verified` for a validated release;
that path remains gated by the confirmation. The workflow checks the
draft/version, builds the wheel and launcher, verifies the card, builds/pushes the
Linux amd64/arm64/armv7 image tagged `VERSION`, uploads matching files and checksums,
then publishes the release after an anonymous image pull succeeds. Treat version
tags as immutable; do not reuse them for changed images. Source archives generated
by GitHub are separate from release assets.

### Public container access

The GHCR package must be public independently of the repository's visibility. In
[package settings](https://github.com/users/jtliang24/packages/container/q2-soloify/settings),
choose **Change visibility → Public**. If the package does not exist yet, run the
workflow once to push it, change visibility, then rerun the failed job on the same
unpublished draft. If the package already exists, ensure this repository has write
access under **Manage Actions access**. The image's source label links it to the
repository; it does not make the package public.

The workflow's pull uses an empty Docker credential directory so maintainer login
cannot hide private-package failures. Check from a tester machine without logging in:

```sh
docker pull ghcr.io/jtliang24/q2-soloify:0.0.1
```

See [GitHub's package visibility instructions](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility).

### First walkthrough release

Label the release notes **Testing release: fresh-user walkthroughs pending**, name
the required Soloify-enabled protocol-v2 firmware, and list the known M1 scrub issue.
Testers download `soloify-setup.py` directly from this prerelease; the launcher fetches
its version-matched artifacts by tag, so it does not require a latest release.
Use the wizard's full card export and copy `.soloify` to the card for this pass.
GitHub's `releases/latest` discovery used by Apps excludes prereleases, so Apps
install/update/remove validation waits for a validated release (or a separate
firmware change to select a testing release).

After publishing the walkthrough prerelease, record actual fresh-user results below.
Complete this matrix before publishing a validated release. An unchanged prerelease
can later be promoted without rebuilding; fixes require a new version and matching
artifacts. Never claim the matrix passed just to enable testing downloads.

| Environment | Required evidence | Current validation |
| --- | --- | --- |
| NixOS native | pairing, OAuth, service restart without pairing, LAN and hotspot playback | Pending |
| NixOS Docker | temporary host pairing/probe, normal network, persistent identity/session | Pending |
| Apple Silicon macOS | native pairing, session transfer, Desktop restart, LAN and hotspot playback | Pending |
| Windows x86 | native pairing, ACLs, session transfer, restart without pairing | Pending |
| Windows ARM | x86 emulation pairing, ACLs, session transfer, restart, hotspot playback | Pending |

Automated tests simulate detection/import/error paths and exercise real local TLS pin
checks and supervisor staging. They do not replace the physical walkthroughs above.
No artifacts have been published merely by preparing a release directory.

## Local verification, 2026-11-14

- Full regression suite: 325 passed, 1 live test skipped.
- Final focused setup, release, renewal, CLI, OAuth and server suite: 107 passed.
- Real Docker smoke test: passed using a local amd64 image, random disposable project
  and volume, fake credentials, and no Spotify requests. It checked host-network TCP
  probing, dotfile session import, private OAuth transfer, read-only resource checks,
  normal-network recreation, persistent identity and pinned TLS readiness. Test storage
  was removed afterward.
- MIPS relay and wheel built; release manifest checksums passed for all four artifacts.
- Standalone launcher check passed without site packages or a source checkout, with no
  installation files created. The wheel's CLI version check passed outside the checkout.
- Ruff, container lint and whitespace checks passed.

These checks do not establish real Spotify pairing, physical device playback, off-LAN
playback, Windows ACL behavior, or native service startup across the physical matrix.
Those walkthroughs and actual release publication remain pending.
