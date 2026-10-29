# Soloify: Spotify through Soloist, as a Q2 Pod app

Status: plan, approved 2026-10-26. Step 0 (server spikes) is done (2026-10-27, [docs/spikes.md](docs/spikes.md)). Step 1 (relay audio) is built and passed its M1 checks, including the battery hour (2026-10-27, [docs/step1.md](docs/step1.md)). Step 2 (bridge core) is done (2026-10-27, [docs/step2.md](docs/step2.md)). Step 3 (relay contract) is built and checked on the M1 (2026-10-28, [docs/step3.md](docs/step3.md)); scrub and the stop/Rockbox UI routes await matching firmware. Step 4 browsing is implemented and validated on the laptop, including live Spotify checks ([results](docs/step4.md)); the [home-network M1 runbook](docs/step4-runbook.md) was run on the M1 on 2026-10-28 (browsing, queue, settings, recovery and stop checks mostly pass; search fails and resume/Previous have audio glitches; [results and findings](docs/step4.md#m1-pass-results)). Fixes are planned separately; no firmware change was made. From 2026-10-27 the development network blocks peer-to-peer traffic, so the laptop can't reach the M1 (no q2remote, no LAN link to the bridge): work that needs no device goes first, and step 3's device check waits for home Wi-Fi, or the laptop and the M1 both on the phone's hotspot (the laptop and the M1 were back on the home network that evening, and the battery hour ran). Paths such as `patch/spotify.c`, `tools/build.py` or `docs/soloist.md` are in the q2-pod repo (`jtliang24/q2-pod`, branch `feature/m1plus-port`); the others are in this repo.

## Context

Spotify won't give librespot audio keys for the user's account (`error audio key 0 1`, librespot-org/librespot#1649), so Q2 Pod's Streaming → Spotify can't play. Spotify's official headless client, Soloist, does play the account (tested 2026-10-25), but it can't run on the X2000. q2-pod's `docs/soloist.md` sets out the design and its tests: Soloist runs on a machine at home; a **bridge** there sends its audio as Opus 160 kbit/s; a **relay** on the card dials out over TLS from any network and keeps librespot's on-device contract.

This plan turns that design into **Soloify**, an app installed from System settings → Apps like Spotify and Soulseek. It has its own Streaming row and the same controls and pages as Q2 Pod's Spotify. The server side runs on Linux, macOS and Windows, with Android evaluated.

### Decisions (user, 2026-10-26)

- **Its own app, on the shared Spotify page.** Downloads can't match librespot: Soloist has no offline API, and recording its stream would be ripping. So Soloify isn't a drop-in for `.spotify/`. It gets its own card folder `.soloify/`, its own Streaming row and its own Apps row, and coexists with Spotify. Only one backend runs at a time. Both rows open `patch/spotify.c`'s page, with the backend picked by the row.
- **Apps row:** on the M1 behind an M1 seam in q2-pod's fork now, then an upstream PR for the Q2 (Q2 users unzip by hand until it merges).
- **Pairing:** for the PoC, copy a file (`.soloify/remote`). A LAN pairing code comes later.
- **TLS:** pin the bridge's self-signed certificate by SHA-256 by default; check against the device's CA bundle (`/etc/scrobble-ca.pem`) when the bridge has a real name (Let's Encrypt, Tailscale Funnel).
- **Code lives in this repo, `q2-soloify`**: relay, bridge, container, card ZIP. Only the firmware seams and docs go in q2-pod.
- **Android:** written up below; an optional, time-boxed spike after the PoC works.
- **Hosts:** the NixOS laptop for development; test on an M1 MacBook Air and on Windows 11 (x86 and ARM); a Pi or the MacBook Air long term.

### What's new since `docs/soloist.md` (checked 2026-10-26 on developer.spotify.com/documentation/soloist)

- Soloist now ships for Linux x86_64, aarch64 and armv7, **Apple Silicon macOS** and **x86_64 Windows**, at fixed URLs that need no login: `https://soloist-builds.spotifycdn.com/soloist_release_{x86_64,arm64,arm32,osx_arm64}.tar.gz` and `soloist_release_windows_x86_64.zip`. Builds expire 90 days after their build date (exit code 10), and must not be redistributed, so the bridge downloads them on the user's machine. (`.gitignore` keeps `soloist_release_*` and `*.key` out of this repo.)
- macOS (Core Audio) and Windows (WASAPI) play only to the **system default output**: there is no device option. Capturing there would need a loopback driver set as the default, taking over the computer's sound. So **macOS and Windows run the Linux build in a container**. On Linux, `-d/--pipewire-device NODE` sends audio straight to the bridge's sink, replacing the doc's `pactl move-sink-input`.
- Pairing: the phone's Spotify app must be **on the same LAN** as Soloist (local discovery). This is the main risk for containers on Docker Desktop for Mac and Windows (step 0).
- The API key is taken only as `-k` (no environment variable or file option), so `ps` shows it. Document this. In a container it's visible only to whoever can list the host's processes.

## Architecture

```
server (Linux native, or a container on Linux/macOS/Windows)         device (any network)
┌─────────────────────────────────────────────────────┐    TLS     ┌──────────────────────────────┐
│ soloist -n Q2 -k KEY -D data --ws 127.0.0.1:P [-d]  │  (pinned   │ Q2 Pod: Streaming → Soloify   │
│   └─audio─► sink q2_soloify ─monitor─► ffmpeg Opus  │   or CA)   │   = spotify.c page, backend   │
│ soloify bridge (Python asyncio)                     │◄───────────┤     .soloify/                 │
│   WS client · Web API (OAuth PKCE) · Q2SPOT/1 ·     │  one TCP   │ .soloify/q2-soloify (relay)   │
│   covers · state · framed wire · self-signed cert   │   port     │   ffmpeg (stock) ► aplay.sh   │
└─────────────────────────────────────────────────────┘            └──────────────────────────────┘
```

The relay keeps the doc's contract unchanged: `/tmp/q2-librespot.state` (+`.jpg`), `/tmp/q2-librespot.sock`, `/tmp/q2-librespot.browse.sock`, the pid file `/tmp/q2-librespot`, and `aplay.sh` honouring `SPOT_OUT`. Only one backend runs at a time, so the `/tmp` names can be shared and spotify.c's state and browser code need no change. The wire, buffer and stall rules, state mapping, controls and Q2SPOT/1 table are as in `docs/soloist.md` (sections Design, The relay, The bridge, Q2SPOT/1 over the bridge, Relay ↔ bridge wire).

## 1. Repo layout

```
relay/        C: main.c (event loop), tls.c (OpenSSL 1.1, linked to the rootfs's libssl/libcrypto),
              wire.c, ring.c, sink.c (stock ffmpeg → aplay.sh), state.c; build.py (link recipe below)
card/         run (restart loop, pid in /tmp/q2-librespot, 5 s → 60 s backoff), aplay.sh,
              remote.sample, README for the card (no q2app: it isn't an addon.c card app)
bridge/       soloify/ (Python 3.12, asyncio): soloist.py (download, verify arch, expiry refresh,
              supervise), audio.py (sink + per-connection ffmpeg), ws.py (Soloist WebSocket),
              state.py (events → status lines), player.py (state, covers, controls, stall pause),
              webapi.py (OAuth PKCE, refresh token in data dir), q2spot.py (ops table), wire.py,
              server.py (TLS listener, token, cert generation), cli.py (`soloify serve|login|remote`)
container/    Containerfile (debian-slim: pulseaudio, ffmpeg, python), entrypoint, compose.yaml
nix/          flake.nix: package, devShell, NixOS module + systemd user unit
docs/         setup per OS, wire spec, troubleshooting
```

- **Relay build.** Same ABI as q2-pod's `q2video`: clang + `ld.lld -m elf32ltsmip --dynamic-linker /lib/ld-linux-mipsn8.so.1`, o32 `nan2008` `fp64`, linked against the stock rootfs's `libc-2.28.so`, `libssl.so.1.1` and `libcrypto.so.1.1` (`tools/build.py`'s `compile_helper`, about line 1001). The relay's `build.py` takes the stock firmware path, as q2-pod's builds do. Releases are built locally (no vendor ZIP in CI), and one binary serves the Q2 and the M1.
- **Release assets.** Apps takes the *first* `.zip` asset of `releases/latest` (`shelf_release` in `patch/netupdate.c`), so the card ZIP is the only `.zip`. It holds `.soloify/q2-soloify`, `run`, `aplay.sh`, `remote.sample`. Server artifacts go elsewhere: the image on GHCR, plus a `.tar.gz` sdist/wheel.

## 2. Relay (card): changes from the doc

- Folder `.soloify/`, binary `q2-soloify`; the same `/tmp` contract as librespot.
- `.soloify/remote`: one endpoint per line, `HOST PORT TOKEN [sha256=HEX] [buffer=MS]`, tried in order (LAN address first, then WAN), each with a short connect timeout. With `sha256=`, the relay pins the server's leaf certificate. Without it, the relay checks the name and the CA bundle.
- **Delay and bitrate are the user's settings** (decided 2026-10-27, after step 1): the buffer target defaults to 1 s on the LAN and 4 s beyond it, and the bitrate to 160 kbit/s. A longer target means fewer dropouts on spotty Wi-Fi for more delay (step 1 measured the delay at the target or up to half a second more). Until the settings UI (section 5), `buffer=` and `soloify serve --bitrate` set them.
- No `remote` file: the browse socket answers `error` with the message "Copy remote to .soloify (see the Soloify guide)", and Retry re-reads the file.
- Everything else as in `docs/soloist.md` "The relay": local pause first, kill and reap the sink before writing a non-`playing` state, one buffer on the device, and pause Soloist after a stall longer than the buffer.

## 3. Bridge (server)

- **Soloist manager.** Picks the build by `uname -m`, downloads it to `data/soloist/`, reads `--version` for the build date, and replaces it from the same URL when under 14 days are left (checked daily; Soloist restarts on the new build once nothing plays), or at once after exit 10. It restarts Soloist on exit with a backoff. It runs `-n Q2` (configurable), `-D data/soloist-state`, `-C data/cache`, `-z 100`, `-i 100` and `--ws 127.0.0.1:0`, then reads `ws.port` from the data directory.
- **Audio.** Native Linux with PipeWire: `pactl load-module module-null-sink sink_name=q2_soloify …` and Soloist with `-d q2_soloify`. Container: a private PulseAudio daemon (`--exit-idle-time=-1`, null sink as default, no host audio). Soloist falls back to PulseAudio when PipeWire can't start. On PipeWire only `-d` routes it (`PULSE_SINK` is ignored); on a PulseAudio host, `PULSE_SINK=q2_soloify` does (step 0). The encoder is the doc's ffmpeg line, started per relay audio request and stopped on pause or disconnect, so nothing queues. Unset `LD_LIBRARY_PATH` for nixpkgs ffmpeg.
- **TLS.** On first run it generates an ECDSA P-256 self-signed certificate. `soloify remote [--lan IP] [--wan HOST]` prints the `remote` file with the token and `sha256=`. An optional `--cert/--key` (Let's Encrypt or Funnel) drops the pin.
- **Web API.** `soloify login` runs PKCE through a loopback redirect, and keeps the refresh token in the data directory. Before relying on each endpoint in the Q2SPOT/1 table, check it on a development-mode app: Spotify has restricted some for new apps, and editorial or algorithmic playlists may answer `unavailable`.
- **Secrets.** The API key and token come from `data/config.toml` (mode 0600) or the container's secret or env file, and never go into logs.

## 4. Cross-platform hosting

| Host | How | Soloist build | Pairing |
| --- | --- | --- | --- |
| Linux (NixOS dev box, Pi, any distro) | native: Nix flake / NixOS module, or pipx + systemd user unit; or the container with `network_mode: host` | x86_64 / arm64 / arm32 | LAN, native or the container on host networking once, with the firewall open to Soloist's random ZeroConf port for that run |
| macOS (Apple Silicon) | Docker Desktop, OrbStack or `podman machine`; compose file | Linux arm64 in the VM | native macOS Soloist paired once, its session copied into the container (host networking can't bind UDP 5353) |
| Windows 11 x86_64 | Docker Desktop (WSL2) or Podman Desktop | Linux x86_64 | native Windows Soloist paired once, its session copied in (host networking not listed) |
| Windows 11 ARM | Docker Desktop for ARM, arm64 Linux containers | Linux arm64 (Windows' own build is x86_64 only) | the same, with the x86_64 build under Windows' emulation |

- One multi-arch image (`linux/amd64`, `linux/arm64`, `linux/arm/v7`) published to GHCR. Soloist is never in the image: it is downloaded into the volume at start.
- `compose.yaml` works with `docker compose` and `podman compose`. It has one published TLS port, a named volume for `data/`, and a `.env` for the key.
- **Pairing fallbacks to test in step 0, in order:** (1) host networking (Docker Desktop 4.34+ for Mac and Windows; Linux always); (2) OrbStack on the Mac; (3) pair once with the *native* macOS or Windows Soloist (`soloist -p -n Q2 -D <dir>`), then mount that data directory into the container, if the stored session carries over between platforms; (4) pair with the container on a Linux box and copy the data directory. Whichever works becomes the documented path for that OS. **Result** ([docs/spikes.md](docs/spikes.md)):
  - (1) failed on every Docker Desktop host.
  - (2) wasn't tried.
  - (3) works on macOS, Windows x86 and Windows ARM: a session moves between platforms.
  - (4) works from Linux to Linux.

  Once paired, the container runs on the default network, controlled through Spotify's cloud.
- Mac or Windows as a server: it has to stay awake (no sleep on AC), and macOS's firewall prompt must allow the port. Document both.

## 5. Firmware (q2-pod): Soloify row on the shared page

Every change sits behind an M1 seam (`#if M1PLUS` or a `patch/m1plus_*` include, per the port's pattern), so the Q2's `update.tar` stays byte-identical to `UPSTREAM_BASE`'s (`test/q2compare.py`).

- **Backend descriptor** (`patch/spotify.c`, the table in a new `patch/m1plus_spotify_backend.inc`): `{label, dir, bin, run, downloads, pair_note, pair_sub}` for Spotify (`.spotify`, `librespot`) and Soloify (`.soloify`, `q2-soloify`). `SPOT_DIR`, `SPOT_BIN` and `SPOT_RUN` (spotify.c lines 69–70, `peq.h:166`) read the current backend. `spot_open` takes the backend from its row argument. Opening the other backend runs `spot_stop()` first, so only one runs.
- **Texts follow the backend:** the page title (`page_list(…, "Spotify", …)` at about lines 711, 714, 721), "X isn't running", "X didn't answer", "X isn't on the card", and the Now Playing notes (about lines 390–396). For Soloify, the pairing texts become "Pair Soloify on its server" / "Open Spotify near the server and choose Q2".
- **Soloify's settings:** two rows for Soloify, beside Streaming Configuration's: Delay (the buffer target, 1 s by default on the LAN, 4 s beyond it; longer means fewer dropouts) and Quality (96, 128 or 160 kbit/s, 160 by default). The relay reads them when it connects; it applies the delay itself, and passes the bitrate to the bridge with its audio request, so the bridge's `--bitrate` becomes only the default. The wire change goes into `docs/wire.md` with step 3.
- **Rows hidden for Soloify:** Downloaded Tracks, Download Playlist (about line 750), Sign Out, and Streaming Configuration's cache cap (`sc_dirs`, `navigation.c:5314`, stays Spotify's). For the upstream PR, generalise this to a `caps` reply, as the doc proposes.
- **`SPOT_KILL`** (`peq.h:108`) also kills `q2-soloify`; the M1 copy goes in `patch/m1plus_peq.h`. Stop Streaming Services, Home's Rockbox row and `spot_stop` then cover both.
- **Streaming row** (`ringnav_stream`, `navigation.c:6182`): "Soloify", after Spotify, only while `.soloify/q2-soloify` exists, opening `spot_open` with the Soloify backend. For now its icon reuses `stream_spotify.png`; a distinct icon waits for the aesthetics pass (all M1 functionality first, visual polish last).
- **Apps row** (`patch/netupdate.c`, the `apps[]` table at line 34, `sh_rows` at 671): `SH_SOLOIFY` `{ "Soloify", "soloify", "jtliang24/q2-soloify", ".soloify", ".soloify/remote", ".soloify/q2-soloify" }`, M1 only. Wherever Spotify jobs call `SPOT_KILL`/`spot_stop` (lines 506, 608, 622), Soloify does too. The check loop (line 524) covers the new row. `remote` isn't in the ZIP (only `remote.sample` is), so it's never on `soloify.files`: updates and Remove both leave it, and the folder with it. The guide says to delete it by hand to unpair.
- **Tests:** `test/m1plus/patch.py`: the Soloify row and its presence check; the page with the Soloify backend against the existing fake Q2SPOT/1 backend; the hidden rows; the one-backend-at-a-time stop; Apps' Soloify row (install, update, remove, keep `remote`). Q2 gates: `test/q2compare.py` stays byte-identical, and `test/m1plus/ports.py` passes.
- **Docs:** rewrite `docs/soloist.md` as Soloify (these decisions, the platform table, the folder), and update `docs/internals.md` (Spotify, Q2 Shelf), `docs/m1plus-changelog.md`, and the user guide's Apps/Streaming section.
- **Upstream PR** (after the M1 device pass): the same change without seams, for the Q2, with the `caps`-based hiding.

## 6. Android: feasibility

- **No native path:** Soloist has no Android build (bionic), and Docker isn't possible without root or a custom kernel.
- **Plausible path:** Termux + `proot-distro` Debian arm64 running the Linux aarch64 Soloist (glibc under proot's ptrace translation), PulseAudio in Termux (or in the proot) with a null sink, ffmpeg and Python from Termux, and a `termux-wake-lock`.
- **Risks:** Android 12+'s phantom-process killer and battery optimisation kill long-running children. The workaround is an `adb` setting or Android 14's developer option, plus a battery exemption. proot overhead on Soloist's ~2% of one core should be fine. Unknown: whether Connect discovery works from proot on the phone's own hotspot LAN, where the device would be the only client.
- **Why it's interesting:** a phone that is both hotspot and server needs no home server, port forward or DNS, and the device's link is local Wi-Fi. But Soloist's own stream then also uses the phone's mobile data.
- **Verdict:** possible, fragile, unsupported by Spotify. The spike (step 10) is two days at most: pair, play 30 min with the screen off, survive a Doze cycle. Go or no-go is written into `docs/soloist.md`.

## Steps

Test and commit after each item. One device flash with a checklist at the end: set Screen Off to Off for the pass and restore it after, drive the device through q2remote, and plan any fixes only after the pass is finished.

0. **Spikes (server only).** Done 2026-10-27; results in [docs/spikes.md](docs/spikes.md). On NixOS: Soloist with `-d` into a null sink, and Soloist in the container with only PulseAudio (the local `soloist_release_x86_64.tar.gz` and `soloist_api.key`, both git-ignored). On the MacBook Air and on Windows x86 and ARM: the container plays, and the pairing fallbacks in order. Check: one pairing path per OS written down.
1. **Relay audio + minimal bridge** (doc step 1): TLS with the pin, token, stock-ffmpeg decode, buffer and stall rule. Check, on the M1: home Wi-Fi and hotspot delay at target; a forced 15 s block pauses and resumes without growing delay; a network switch reconnects; an hour on battery. This sets the buffer targets and the bitrate A/B. Done (2026-10-27, [docs/step1.md](docs/step1.md)): delay about 1–1.5 s at the 1 s LAN target and 4 s on a hotspot; blocks and network switches recover by themselves; 96 and 160 kbit/s sound alike to the user; streaming with the screen dark drains about 0.16% a minute (roughly 10 hours from full), the relay and decoder about 1% of the CPU, and the gauge held 100% for the first hour, so the figure comes from a 30-minute second leg. Defaults stay 1 s and 160 kbit/s, as settings (section 2).
2. **Bridge core** (doc step 2) against a fake relay: a scripted cast, pause, seek, skip, track change and deactivate give the expected state lines and covers. Done 2026-10-27 ([docs/step2.md](docs/step2.md)), off the device's network: the fake Soloist replays a recorded session, and a live run against the real Soloist checked the covers, the controls and a stall. A stall's pause reaches the relay as `playing`, so Q2 Pod's idle stop doesn't end it.
3. **Relay contract** (doc step 3). Built and checked before firmware changes, temporarily in place of `.spotify/librespot` with the originals backed up and restored (2026-10-28, [docs/step3.md](docs/step3.md)). Now Playing, keys, direct seek, `spot_yield`/`spot_take`, idle stop, the shared stop command, a crashed relay and both stall lengths passed. The installed older hub did not expose the planned scrub and stop/Rockbox UI routes; repeat those with step 5. The pass found a short HTTP read truncating covers; fixed with regression coverage afterward.
4. **Browsing** (doc step 4): OAuth, every op, 429/401 against a fake Web API, and endpoint availability for development-mode apps.
5. **Firmware seams** (section 5) on a branch off q2-pod's `feature/m1plus-port`: build with `nix develop -c python3 tools/build.py …`, the M1 suites, `test/q2compare.py`, then `graphify update .`.
6. **Packaging:** card ZIP, GHCR multi-arch image, compose, Nix flake/module, `soloify remote`, `soloify pair` (step 0's pairing path per OS), per-OS setup docs.
7. **Cross-platform runs:** NixOS native and Podman, macOS Docker, Windows x86 and ARM Docker, each playing to the M1 for 10 min with skip, seek and pause.
8. **Device pass on the M1** (one dev flash): the Apps row installs, updates and removes; the Streaming row; the full Spotify-page checklist with the Soloify backend; switching Spotify ⇄ Soloify; hidden rows; Bluetooth not applicable (Wi-Fi). Restore settings, then plan any fixes separately.
9. **Upstream PR** (Q2): the seams without guards, plus `caps`, offered to DiamondBond/q2-pod. A Q2 device check if one is available.
10. **Later:** the LAN pairing code (relay broadcast, code shown through the page's error message, confirmed on the server); the Android spike; a Pi deployment.

## Verification (end to end)

- Server: `soloify serve` on NixOS, then `soloify remote --lan … --wan …` → `.soloify/remote` on the card.
- Device: Apps → Soloify → Install; Streaming → Soloify → menu loads (`account` ok); a playlist track plays in context; Now Playing shows art and position; pause is instant; skip and seek take about one buffer; hold Play/Pause adds to the queue; Stop Streaming Services stops it; opening Spotify stops Soloify and the other way round.
- Off home Wi-Fi: on the phone's hotspot through the WAN endpoint, with stall and recovery as in step 1.
- Gates: q2-pod's M1 suites, `test/q2compare.py` (Q2 byte-identical), `test/m1plus/ports.py`, and this repo's unit tests for the relay and bridge (a fake relay, a fake Web API, a fake Soloist WebSocket).
