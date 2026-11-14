# Guided setup operations

Run `python3 soloify-setup.py --check` for a non-mutating report, or
`soloify setup --check --json` for automation. No temporary containers are created.
Detection can report Docker usable while Linux Desktop host-network confirmation is
still required before pairing. Host networking is checked again after Apply and restart.
macOS/Windows use native pairing and do not require that setting for normal setup.
The Linux probe confirms container-to-host TCP reachability; the phone's discovery
and firewall reachability still need the pairing test.

`--root PATH` selects an installation. `setup-state.json` records non-secret progress,
locations and an installation-specific Compose project/volume name. `secrets/api.key`
is private; Docker keeps sessions, OAuth and bridge identity in its named data volume.
Native Linux keeps them in `ROOT/data`. Private writes are atomic and mode 0600;
Windows setup applies a user-only ACL. Do not share this directory or unredacted
Soloist logs. Soloist's own API-key argument is visible to local process inspection.

Resume incomplete setup with the same root. Existing credentials and sessions are
kept by default, replacements require confirmation, and resources are rechecked.
Completed setups offer endpoint/export updates without credentials or pairing.
A lock prevents two wizards using one root. Do not delete the named volume or the
`data/bridge` identity directory: doing so invalidates the card's pin and token.
To migrate an existing manually managed installation, stop it first, import its
paired `soloist-state` and OAuth file, and keep using its original bridge identity if
you reuse its data directory. The wizard's default root creates a separate installation.

## Away listening

The route is:

```text
away device → public IPv4/DNS hostname:external TCP port
            → router forwarding → bridge LAN IPv4:internal TCP port
```

Choose “at home and away” in setup. Reserve the bridge's LAN address in the router's
DHCP settings. Supply a public IPv4 or DNS/DDNS hostname without `https://`, paths or
a port suffix. The default external TCP port is 7443, independently of the internal
bridge port. Setup prints the exact forwarding rule using your values.

Permit the internal TCP port in the host firewall. Docker publishes that internal port
on the host. Keep the OAuth callback (127.0.0.1:8888) and Spotify discovery local.
The card tries LAN first, WAN second; both endpoints retain the same authentication
token and SHA-256 certificate pin. You can also generate endpoints manually:

```sh
soloify --data data remote --lan 192.168.1.20 --port 7443 --wan music.example.com --wan-port 8443
```

Treat its output as a secret. `--wan-port` defaults to `--port`, preserving old behavior.
Use DDNS if your public address changes, and update its IPv4 record. DNS and TCP/TLS
checks verify the pinned certificate but do not establish external reachability from
inside the home. NAT loopback may make an otherwise correct endpoint fail locally.

Compare the router's WAN IPv4 with the public IPv4 reported by your ISP/router or an
external address service. A private WAN address or 100.64.0.0/10 can indicate another
router or carrier-grade NAT. For double NAT, forward the external TCP port through each
router to the next router, then to the bridge. With carrier-grade NAT, ask the ISP for
an inbound-capable public IPv4 address. Router changes are manual; this version does
not provision VPNs. A working LAN configuration remains usable if WAN is unresolved.

To verify, disconnect the device from home Wi-Fi, connect to a phone hotspot using
mobile data, and play. Until you confirm this succeeds, setup says
“WAN configured; external verification pending.” Rerun to change endpoints and export
again without re-pairing.

## Startup and troubleshooting

Docker uses a release-pinned image, installation-specific Compose file and persistent
volume. The normal service publishes only the bridge port. A separate temporary
pairing configuration uses host networking on Linux; cancellation stops it and keeps
the volume. The default restart policy is `unless-stopped`; enable Docker startup in
Desktop's settings or your Linux configuration. Stopping with Compose prevents restart
until the service is started again. Use the generated `compose.json` for logs and
service operations. Do not use `down -v` unless you intend to erase the installation.

Native setup enters a pinned Nix environment, which supplies Python dependencies,
FFmpeg, PulseAudio tools and Soloist's Nix loader. Automatic startup creates a systemd
user service named `soloify-INSTALLATION.service`. It starts at login; enable user
lingering for startup without login/after logout, and ensure the user audio server
also remains available. A native installation needs a usable user systemd session.
If automatic startup is declined, setup prints a manual command and offers to start
it now. Keep the source checkout in place when running a development installation.

If pairing fails, use the same LAN, disable guest/client isolation and allow the
reported ZeroConf TCP port while pairing. NixOS users can use
`sudo nixos-firewall-tool open tcp PORT`, then reset the temporary rule. Retry or import
a session from a stopped Soloist. macOS requires Apple Silicon for native pairing;
Windows ARM runs Spotify's x86 Windows build under emulation.

A TLS pin mismatch means the endpoint reaches a different bridge identity. Fix
forwarding or regenerate `remote` from the intended identity. Connection failures
usually mean an incorrect LAN address, firewall, sleeping bridge, or wrong port.
Playback and successful Spotify login are separate from bridge TLS readiness.

Use `soloify --data DATA diagnostics` for the installed version/build date, estimated
90-day expiry and eligibility from build age 60 days. Automatic renewal belongs to the
bridge supervisor; no cron job or image rebuild is needed. Downloading an identical,
older, malformed or expired build is rejected before replacement. While a usable build
remains, failure logs its remaining lifetime and retries daily. At expiry exit code 10,
the supervisor tries immediately and clearly fails if no usable replacement exists.
