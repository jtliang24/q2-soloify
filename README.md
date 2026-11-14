# q2-soloify

Soloify plays Spotify on Q2 Pod devices (Shanling Q2, M1 Plus) through Spotify's own [Soloist](https://developer.spotify.com/documentation/soloist), for accounts librespot can't play. Soloist can't run on the device, so it runs on a computer at home. The **bridge** (this repo's Python server) runs Soloist, encodes its audio as Opus and serves it over TLS. The **relay** on the device's card dials the bridge, decodes the audio and plays it, keeping the Spotify page's controls, browsing and Now Playing working. The design is in [PLAN.md](PLAN.md) and the protocol in [docs/wire.md](docs/wire.md).

```
home computer                                     M1 Plus
phone's Spotify app ──cloud──► Soloist ─► bridge ══ TLS, port 7443 ══► relay (.soloify/q2-soloify) ─► headphones / USB
                                           ▲                                   │
                                           └── browsing, queue, controls ◄─────┘
```

## Guided setup

Install **Python 3.11 or later**, then download `soloify-setup.py` from the matching
[release](https://github.com/jtliang24/q2-soloify/releases). From this checkout:

```sh
python3 soloify-setup.py --check   # read-only readiness report; no downloads or containers
python3 soloify-setup.py
```

On Windows use `py -3 soloify-setup.py`. An installed bridge also provides
`soloify setup` and `soloify setup --check`. Setup never installs Docker, Nix, Python,
firmware, or other system prerequisites. Docker is recommended when usable; native
Nix is available on Linux with a running PulseAudio/PipeWire PulseAudio server.
The launcher checks daemon access, the active Docker context, Linux-container support,
architecture, native audio, and LAN addresses before asking for secrets or provisioning
the wizard. Remote Docker engines are unsupported.

Install Docker using its official instructions for
[macOS](https://docs.docker.com/desktop/setup/install/mac-install/),
[Windows](https://docs.docker.com/desktop/setup/install/windows-install/), or
[Linux](https://docs.docker.com/engine/install/).
On NixOS, enable `virtualisation.docker.enable`, add your user to the Docker group,
rebuild your configuration, and log in again. Native setup requires
[Nix](https://nixos.org/download/) and PulseAudio compatibility. The wizard provisions
its Python dependencies after you select a usable method.

Have Spotify Premium and your own [Soloist API key](https://developer.spotify.com/documentation/soloist)
ready. The wizard accepts a key file (including quoted or dragged-in paths) or hidden
entry, and offers imports for an existing paired session, `oauth.json`, and a matching
Soloist archive. Each computer downloads Soloist directly from Spotify; the release
never includes Soloist or anyone's credentials.

The wizard guides you through:

1. Pairing **Q2** in Spotify's device picker on the same LAN. Linux Docker pairs
   temporarily with host networking after a connectivity probe. Native Linux Docker
   Engine needs no Desktop toggle. Where Linux Docker Desktop requires the setting,
   setup guides **Settings → Resources → Network → Enable host networking → Apply
   and restart**, then rechecks. See [Docker's host networking instructions](https://docs.docker.com/engine/network/drivers/host/).
   macOS and Windows pair a native Soloist, stop it, then transfer the session into
   persistent container storage. Windows ARM uses the x86 build's emulation support;
   Intel Macs need an imported session. Firewall guidance and retry/import fallbacks
   are included. Never run two copies of one paired session.
2. Optional Spotify browsing authorization. Create a developer app and register
   exactly `http://127.0.0.1:8888/callback`. Authorization happens in the host browser;
   the wizard transfers its token into bridge storage. Without it, Connect playback
   works, but device browsing, search and queue are unavailable.
3. Choosing **at home** or **at home and away**, configuring a stable LAN address,
   and optionally forwarding the bridge's TCP port. See [away listening](docs/setup.md#away-listening).
4. Starting the bridge, with automatic startup selected by default, and exporting a
   complete `.soloify` folder with the matching MIPS relay, `run`, `aplay.sh`, and
   private `remote`. Copy that hidden folder to the root of the device's card. Existing
   installations can export only `remote`.
5. Separate results for bridge readiness, file export, LAN playback, WAN verification,
   and automatic Soloist renewal. Successful checks at home leave external verification
   pending until you confirm playback over a phone hotspot with home Wi-Fi disconnected.

The bridge computer must stay powered, awake, and connected. Docker needs to start
at login/boot; native Linux uses a systemd user service. Setup explains lingering and
user audio availability for operation after logout. Rerun setup with the same `--root`
(default `~/.soloify-bridge`) to update endpoints and re-export without pairing again.
Keep that directory private and backed up: it preserves the bridge identity, paired
session, OAuth credentials and API key. Progress is stored separately without secrets.

Q2-Pod firmware must include **Streaming → Soloify**, from the `step5-soloify`
integration, using protocol v2. Firmware installation and upstream Q2-Pod integration
are separate; this wizard does not flash firmware. Future Apps installation can use
configuration-only export. Delay and Quality settings remain in Streaming Configuration.
[Card details](card/README.md), [manual development setup](docs/manual-setup.md), and
[troubleshooting/startup guidance](docs/setup.md) cover the remaining details.

Soloist builds expire 90 days after their **build date**. The bridge checks at startup
and every 24 hours, renewing once the installed build reaches **60 days old**. It stages
and validates a newer unexpired build, then activates it when playback is idle. An
identical download does not extend validity. Failed renewals retain the usable build
and retry daily. Inspect build date, estimated expiry and renewal eligibility with:

```sh
soloify --data ~/.soloify-bridge/data diagnostics     # native
# Docker: use the installation-specific compose file, never a different data directory
docker compose -f ~/.soloify-bridge/compose.json exec -T -u soloify soloify soloify diagnostics
```

## Status

The bridge and relay carry audio, state, covers and controls, and browsing, search, queue and settings work through the existing Spotify page. Pause stops output at once and rewinds Soloist to the last heard position; resume refills a fresh buffer from that bookmark. Deploy the bridge and relay together (protocol version 2). The results so far:

- [Step 4](docs/step4.md): laptop validation, live Spotify checks and the M1 pass, with the [reliability follow-up](docs/step4-followup.md) and the repeatable [M1 runbook](docs/step4-runbook.md).
- [Step 5](docs/step5.md): the firmware's Soloify row, Delay and Quality, and the M1 pass. Scrub through the UI still fails on the M1, as in [step 3](docs/step3.md).
- [Pause and rewind](docs/pause-rewind.md): implementation and M1 runbook, with [follow-up progress](docs/pause-rewind-followup.md). Physical verification is partial.

Spotify's terms forbid redistributing Soloist, so this repo never holds it. Each machine fetches its own build, and each user makes their own API key (with Spotify Premium). `.gitignore` keeps `soloist_release_*` and `*.key` out of git.

## Development

Building, testing and running from a checkout (the Nix shell, the relay build, `serve`, `login`, the Soloist launchers) is in [docs/development.md](docs/development.md).

## Manual container development (Docker or Podman)

For macOS and Windows, whose Soloist plays only to the system's default output (the [manual setup guide](docs/manual-setup.md#macos-and-windows-docker-desktop) has step-by-step instructions), and for Linux hosts that prefer it. The image runs Spotify's Linux build (fetched into the data volume at start) into a private PulseAudio null sink, `q2_soloify`; nothing reaches the host's audio. The key goes in as the compose secret `soloist_api_key`, read from `./soloist_api.key`.

```sh
docker compose up -d                                      # the bridge (soloify serve), port 7443
docker compose -f compose.yaml -f compose.linux.yaml up -d  # Linux: host network, for pairing
docker compose -f compose.yaml -f compose.dev.yaml up -d    # dev: ./ mounted at /import, kept idle
docker compose -f compose.yaml -f compose.dev.yaml exec -u soloify soloify sh
```

`podman compose` takes the same files. The dev override installs the repo's `soloist_release_<arch>.tar.gz` instead of downloading, and runs `sleep infinity` instead of the bridge, so Soloist or `soloify serve` can be run by hand through `exec`. Shells go in as `-u soloify`: the entrypoint starts as root only to hand the secret and the volume to that user. `hadolint container/Containerfile` lints the image.

Pairing needs the phone's Spotify app on Soloist's LAN, but only once: a paired container plays on the default network, controlled through Spotify's cloud. Under Docker Desktop on macOS and Windows, pair a native Soloist and copy its state into the container as described in the [manual setup guide](docs/manual-setup.md#macos-and-windows-docker-desktop). The first pairing may need a fallback; [docs/step0-runbook.md](docs/step0-runbook.md) tries them, and [docs/spikes.md](docs/spikes.md) has the results.

Release preparation is documented in [docs/releasing.md](docs/releasing.md).

## Layout

| Path | What |
| --- | --- |
| `bridge/soloify/` | the bridge (Python): TLS listener, Soloist and its WebSocket, state and covers, audio |
| `relay/` | the card's relay (C) and its build |
| `card/` | the launcher `run`, `aplay.sh`, `remote.sample` and measurement helper `log.sh` |
| `tests/` | the bridge's and the relay's tests |
| `tools/soloist-fetch`, `tools/soloist-run`, `tools/soloify-sink` | fetch and run Soloist; the null sink and its level (POSIX sh, also used in the image) |
| `tools/soloify-stall` | step 1's forced stall: blocks the bridge's port for a while (root) |
| `container/` | `Containerfile`, `entrypoint`, `pulse.pa` |
| `compose*.yaml` | the service; Linux host networking; development |
| `flake.nix` | the optional Nix dev shell and the `soloist` launcher, on q2-pod's nixpkgs revision |
