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
[Linux](https://docs.docker.com/engine/install/). Native setup requires
[Nix](https://nixos.org/download/) and PulseAudio compatibility. The wizard provisions
its Python dependencies after you select a usable method.

Have Spotify Premium and your own [Soloist API key](https://developer.spotify.com/documentation/soloist)
ready. The wizard accepts a key file (including quoted or dragged-in paths) or hidden
entry, and offers imports for an existing paired session, `oauth.json`, and a matching
Soloist archive. Each computer downloads Soloist directly from Spotify; the release
never includes Soloist or anyone's credentials.

The wizard guides you through:

1. Pairing **soloify** in Spotify's device picker on the same LAN. Linux Docker pairs
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
4. Starting the bridge, with automatic startup selected by default.
5. [Pairing the device by code](#pairing-the-device-by-code), so no file is copied.
   Declining, or failing, exports a complete `.soloify` folder with the matching MIPS
   relay, `run`, `aplay.sh`, `q2app` and a private `remote` instead. Copy that hidden
   folder to the root of the device's card. Existing installations can export only
   `remote`.
6. Separate results for bridge readiness, device pairing or file export, LAN playback,
   WAN verification, and automatic Soloist renewal. Successful checks at home leave
   external verification pending until you confirm playback over a phone hotspot with
   home Wi-Fi disconnected.

The bridge computer must stay powered, awake, and connected. Docker needs to start
at login/boot; native Linux uses a systemd user service. Setup explains lingering and
user audio availability for operation after logout. Rerun setup with the same `--root`
(default `~/.soloify-bridge`) to update endpoints and re-export without pairing again.
Keep that directory private and backed up: it preserves the bridge identity, paired
session, OAuth credentials and API key. Progress is stored separately without secrets.

Which firmware runs it decides how Soloify gets onto the card:

- **Upstream Q2 Pod 1.9.1 or later, on the Q2:** [install it from the App Store](#install-from-the-app-store).
  Its Soloify app pairs, puts the relay in Spotify's place and takes it out again.
- **The M1 Plus's firmware** (the q2-pod fork with the `step5-soloify` integration, protocol v2):
  **Streaming → Soloify** is its own row. The one thing it takes from this page is
  [pairing by code](#pairing-the-device-by-code); Delay and Quality stay in Streaming
  Configuration.
- **Any other Q2 Pod release:** the relay can [stand in for Spotify's librespot](#in-place-of-spotifys-librespot)
  by hand.

Firmware installation is separate; this wizard does not flash firmware.
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

### Pairing the device by code

The device gets the bridge's addresses, token and certificate pin without a file
copied by hand. The wizard offers it after the bridge starts, or later, on the computer
running the bridge:

```sh
soloify pair-device            # native; python -m soloify.cli pair-device from a checkout
docker compose -f ~/.soloify-bridge/compose.json exec -T -u soloify soloify soloify pair-device
```

(The Docker form is for a default `--root`; setup's own compose file is at
`ROOT/compose.json`.) While it waits:

1. On the Q2 open **Library → Apps → Soloify → Pair with server**. The Q2 finds the
   bridge by a UDP broadcast on port 7444 and shows a six-digit code, such as `123 456`.
2. Type the code on the computer. A match proves the Q2 reached this bridge, and the
   bridge sends its `remote` lines through that connection.

The wizard and `soloify pair-device` on the host answer the broadcast. A bridge in a
container can't hear it, so with the Docker command above the Q2 doesn't find it: enter
the computer's LAN address in the Q2's **Server address** row, or run the wizard, which
answers for it. The same applies on guest Wi-Fi or with client isolation. Windows may
show a Windows Firewall prompt for Python the first time: allow it on private networks,
or the Q2 won't find the computer. **Pairing is LAN-only**: the bridge refuses it from
any address outside the home network, and only while a `pair-device` is waiting.

The paired device gets the LAN address and, when setup's away step is done, the WAN
address too, with one token and pin. Without a WAN address `pair-device` ends with "Paired
for home only"; run [setup's away step](docs/setup.md#away-listening), then pair again to
listen away. With a raw IP address as the WAN address it warns that the address changes
when your ISP changes it, so prefer a DDNS name. After pairing at home, the Q2's
Soloify app shows *Away: not yet verified* until the first connection through the WAN
address works, then *verified* with a date. It reads *not reached* after three WAN
failures in a row, and then the address in setup needs fixing before pairing again.

**On the M1 Plus** (and in the Spotify slot by hand, below) there is no Soloify app, but a
relay with no `remote` pairs by itself: its Soloify (or Spotify) page shows the code
as its error message, "Pair: enter 123 456 on your Soloify server". Run `soloify pair-device`,
type the code, and press Retry. It has no address row, so the computer must answer the
broadcast (the wizard or a native `pair-device`); otherwise copy `remote` as before. To pair
again, delete `remote`.

Pairing reuses the bridge's token, so every device paired earlier keeps working;
`soloify remote --rotate` replaces it, which revokes them all. The protocol is in
[docs/wire.md](docs/wire.md#pairing-pair-version-1).

## Install from the App Store

For a Q2 on **upstream Q2 Pod 1.9.1 or later**, where the App Store reads custom sources.
It needs a release newer than `v0.0.1` published as the repository's Latest release
([docs/releasing.md](docs/releasing.md#latest-and-prerelease)), because the App Store
installs only that. Set up the bridge first ([guided setup](#guided-setup)).

1. Add one line to `/mnt/mmc/.q2/custom-sources.list` (the card's `.q2` folder; make the
   file if it isn't there):

   ```text
   jtliang24/q2-soloify Soloify
   ```

   Then open the App Store on the Q2 and **Install** Soloify.
2. Open **Library → Apps → Soloify**. Choose **Pair with server**, run
   [`soloify pair-device`](#pairing-the-device-by-code) on the bridge's computer (or let the
   wizard do it) and type the code the Q2 shows.
3. Choose **Use Soloify for Spotify**, then pick **soloify** in the phone's Spotify device
   picker. **Streaming → Spotify** now plays through the bridge, with Now Playing,
   browsing, queue and controls.

The app's Status shows what the Spotify slot holds, whether the Q2 is paired, whether the
bridge answers and the Away line. **Delay** (Automatic, 1, 2, 4 or 6 s) and **Quality**
(96, 128 or 160 kbit/s) are its rows too: Streaming has none for Spotify. The swap is the
[manual copy below](#in-place-of-spotifys-librespot), done for you: it backs up Spotify's
`librespot`, `run` and `aplay.sh` to `.soloify/backup/`, leaves `.spotify/cache` alone and
puts the relay in `.spotify`.

- **Restore Spotify** (shown while the relay is in the slot) puts the backup back and
  deletes `remote`, `.delay` and `.quality` from `.spotify`. Without a backup it removes
  only Soloify's files; install Spotify from the App Store then.
- **Repair** appears after an App Store **Update of Spotify**, which writes the real
  librespot over the relay. It runs the swap again and keeps that librespot as the new backup.
- **Update Spotify's copy** appears after a Soloify update, when the slot's relay is older
  than `.soloify/q2-soloify`.
- A Soloify **Update** replaces only the release's files: `remote`, `.delay`, `.quality` and
  `backup/` stay. A Soloify **Remove** deletes the whole `.soloify`, `backup/` included,
  but not `.spotify`, where the relay keeps working. **Restore Spotify before you Remove
  Soloify**, or install Spotify from the App Store afterwards.
- Spotify's own **Remove** deletes `.spotify`, `remote` and the relay with it. **Erase
  Spotify Data** is safe: it deletes only the cache and the logs.

Only the Q2 on upstream Q2 Pod has the app. The **M1 Plus** runs the same files but is
unchanged apart from [pairing by code](#pairing-the-device-by-code): `run` tells the devices
apart (the M1 Plus's `/sys/class/graphics/fb0/virtual_size` and its Goodix touchscreen; a
device matching neither is taken for a Q2), and on the M1 Plus it deletes `.soloify/q2app`,
so no second Soloify row appears next to the firmware's own.

## In place of Spotify's librespot

The manual way, for a Q2 Pod without the App Store's [Soloify app](#install-from-the-app-store)
(or to do by hand what its swap does). Q2 Pod's releases have no Soloify row, but the relay can
take librespot's place in the card's `.spotify` folder: **Streaming → Spotify** then plays through the bridge, with
Now Playing, browsing, queue and controls. The firmware starts `.spotify/run`
and talks to `/tmp/q2-librespot.*`, the relay's own defaults, and the bridge tells the
page what it can't do, so the download rows are hidden. Spotify's librespot is gone
until you put it back.

1. Set up the bridge with the [guided setup](#guided-setup) (or the [manual setup](docs/manual-setup.md)).
   Its exported `.soloify` folder holds every file below; use the files from one
   release together, as the relay and the bridge must speak the same protocol.
2. With the card in a computer, copy `.spotify`'s `librespot`, `run` and `aplay.sh`
   somewhere safe, such as a `.spotify-backup` folder at the card's root. Leave
   `.spotify/cache` where it is.
3. Copy the exported files into `.spotify`:

   | From `.soloify` | To `.spotify` |
   | --- | --- |
   | `q2-soloify` | `librespot` (the relay, renamed) |
   | `run` | `run` |
   | `aplay.sh` | `aplay.sh` |
   | `remote` | `remote` (or leave it out and [pair by code](#pairing-the-device-by-code)) |

   The relay must be named `librespot`, with no `q2-soloify` in `.spotify`: `run`
   starts `q2-soloify` first when there is one, and the firmware stops the service
   with `killall librespot`, which would miss it. Make the first three executable
   (`chmod 755`) and `remote` private (`chmod 600`). A `.soloify` folder left on the
   card does nothing: without a `q2app` file the firmware doesn't list it.
4. Optionally, set the delay and quality, which the firmware has no rows for here:
   `.spotify/.delay` holds the buffer target in milliseconds and `.spotify/.quality`
   96, 128 or 160 (kbit/s), one integer each. Without them the relay buffers 1 s on
   the LAN and 4 s beyond it, at the bridge's bitrate (160 by default).
5. Put the card back, open **Streaming → Spotify**, and choose **soloify** in the
   phone's Spotify device picker.

While it's installed:

- **Don't press Update or Remove on Spotify in the App Store.** Update writes the real
  librespot over the relay; Remove deletes `.spotify` but its cache, `remote` with it.
  Erase Spotify Data is safe: it deletes only the cache and the logs.
- The row, the page and its messages still say Spotify. The Spotify Cache setting
  doesn't apply, and Sign Out answers that it's done on the server. With no
  `.spotify/remote` the page shows the pairing code as its error message; the relay writes
  `.spotify/remote`, and `.soloify/remote` too.
- To update, copy a newer release's relay over `.spotify/librespot` and its `run`
  and `aplay.sh` beside it. When the bridge's addresses change, export only `remote`
  and copy it to `.spotify/remote`.
- To go back to librespot, copy the backed-up files into `.spotify` and delete
  `remote`, `.delay` and `.quality`.

This is how the M1 ran in [step 3](docs/step3.md) and [step 4](docs/step4-runbook.md),
on older firmware. On a Q2, so far only the relay's build has been checked, under
qemu against the Q2's stock V1.32 libraries and ffmpeg. Search, Bluetooth output and
scrub haven't been tried with the current firmware.

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
