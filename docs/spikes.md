# Step 0: server spikes

PLAN.md step 0, before any bridge or relay code. For each host: does Soloist's audio land in the `q2_soloify` null sink, and only there, and which pairing path works? Done on all five hosts by 2026-10-27: the NixOS laptop, then the MacBook Air and the two Windows machines by hand from [step0-runbook.md](step0-runbook.md).

Soloist 1.3.10.16 (build 20261026, "client expires in 89 days") on NixOS; the other hosts fetched 1.3.10.18 (build 20261027) a day later. The account the user plays from, a phone on the same Wi-Fi. `soloist ctl -D <data>/soloist-state activate` then `play URI` drives a paired Soloist without the phone; `tools/soloify-sink level` reads the sink's monitor (silence is -91 dB).

## Summary

| Host | Audio | Pairing path |
| --- | --- | --- |
| NixOS native (PipeWire 1.6.6) | `-d q2_soloify` | LAN, with Soloist's ZeroConf port open in the firewall while pairing |
| NixOS, Docker 29.9.1 | the container's PulseAudio, default sink | host networking (`compose.linux.yaml`) once, then the default NAT network; or a session copied in from another Linux machine |
| macOS 26.5.2 (Apple Silicon, Docker Desktop 29.9.2) | the container's PulseAudio, default sink | a native macOS Soloist paired once from the phone, its session copied into the container; host networking can't bind port 5353 |
| Windows 11 x86_64 (Docker Desktop 29.9.2) | the container's PulseAudio, default sink | a native Windows Soloist paired once, its session copied into the container; neither NAT nor host networking was listed |
| Windows 11 ARM (Docker Desktop 29.9.2) | the container's PulseAudio, default sink | a native Windows Soloist (x86_64 emulated) paired once, its session copied into the container; host networking was not listed |

**One path works on every host.** On Linux, pair natively or in the container on host networking, with the firewall open to Soloist's ZeroConf port for that run. On macOS and Windows, pair a native Soloist once (from the phone or the Spotify desktop app on the same machine) and copy its `soloist-state/` into the container's volume. Docker Desktop's host networking never listed Q2: on the Mac it can't bind UDP 5353, and on Windows the container's mDNS doesn't reach the LAN. A session moves between Linux, macOS and Windows, and once paired the container runs on the default NAT network, controlled through Spotify's cloud.

## Findings

- **Pairing needs the LAN once, and only once.** A paired Soloist on Docker's NAT network, where its advert can't reach the LAN, logs in from its stored session, and the phone lists it, controls it and starts tracks on it through Spotify's cloud. So the default compose network is fine for daily use; host networking (or another path) is needed only for the first pairing.
- **A stored session moves between Linux systems.** A session paired natively on NixOS, copied into a fresh container volume (a different hostname, Debian's userland), logged in and played. The device id goes with it (`.device_id` in the data directory), so two copies of one data directory must never run at once.
- **Discovery is mDNS plus an HTTP request to a random port.** Soloist advertises `_spotify-connect._tcp` under its device id (not "Q2"), TXT `CPath=/zc`, on a ZeroConf port chosen afresh at each start, with no option to fix it. The phone lists the device only once its `getInfo` request to that port succeeds.
- **A host firewall blocks pairing.** On NixOS the phone saw Q2 only after `sudo nixos-firewall-tool open tcp PORT` for that run's port (then `reset`); the laptop's own Spotify app saw it before, through the local path the firewall allows. The setup docs need this step for native Linux and for a container on host networking, or the copy-a-session path instead.
- **Under NAT, no pairing.** In the container on Docker's default network, Soloist sees only its bridge address (172.18.0.2); its advert never reaches the LAN (`avahi-browse` on the host doesn't list it) and the phone doesn't show Q2. This is the expected case for Docker Desktop on macOS and Windows.
- **PipeWire: only `-d` routes.** With PipeWire reachable, Soloist uses its native API and the default device; `PULSE_SINK` is ignored and the audio goes to the speakers. `-d q2_soloify` puts the stream on the sink (`target.object = "q2_soloify"`) and leaves the speakers' sink suspended.
- **PulseAudio: `PULSE_SINK` routes.** When PipeWire is unreachable ("PipeWire daemon not reachable; skipping PipeWire", "Using PulseAudio"), Soloist honours `PULSE_SINK`. The container needs neither: its private PulseAudio's default sink is `q2_soloify`, and the host's audio gets nothing.
- **Volume.** A run starts at the session's last volume (56% just after pairing) or the default 40% (`-i`), and the sink's level follows it (-52 dB mean at 40%, -21.5 to -31.5 dB near full). The bridge passes `-i 100` and sends `set_volume 100` on activation, as planned; `tools/soloist-run` passes neither.
- **The encoder line is fine.** 30 s of the sink through `docs/soloist.md`'s ffmpeg line (`tools/soloify-sink encode`): Ogg Opus, 48 kHz stereo, 166 kbit/s, and it sounded right.
- **Footprint.** Natively, 69 MB RSS and 2.1% of one core a minute into playback, plus 12 MB for the crash reporter.
- **`docker exec` runs with umask 0000.** A Soloist started through `exec` wrote its session world-writable; `tools/soloist-run` and the entrypoint now set `umask 077`.

## Windows 11 x86_64

Windows 11 Pro 10.0.26300, Docker Desktop 29.9.2, container `x86_64` with Soloist 1.3.10.18 (build 20261027). The Spotify desktop app runs on the same machine; every "listed or not" check below was made in that app, and no phone was used for discovery. Run from [step0-runbook.md](step0-runbook.md).

- **Fallback 1, host networking: Q2 not listed.** Not on the default NAT network (ZeroConf started on port 35993), and not after turning on Docker Desktop's "Enable host networking" (port 58861). The engine restart that setting needs kills a running container.
- **Fallback 3, native Soloist: paired.** The x86_64 Windows build from `soloist-builds.spotifycdn.com` listed Q2 in the desktop app at once; `getInfo`, then `addUser`, then `pairing complete, credentials stored` and `logged in`, 24 s after `ready`. No firewall prompt was reported.
- **The copied session works.** `dc cp` of `soloist-state` into the volume, then `chown` and `chmod`, and the container's Soloist logged in from it (`logged in: yes`).
- **Play check passed on the default network.** `play` then `now`: `playing | 0:03 / 3:55 | vol 40%`; `level`: `mean_volume -21.5 dB`, `max_volume -3.8 dB` after `volume 100`; the PC's speakers stayed silent ("Using PulseAudio"). Q2 showed as the playing device in Spotify, and a skip (Crave to Ribs) and a pause from there reached Soloist (its log shows `Pause track`).
- **Restarting a stopped container can fail.** After the first container was killed (exit 137, as Docker Desktop restarted), `docker compose up -d` started it again but `pulseaudio` died with "Daemon startup failed" and the entrypoint exited 1. `up -d --force-recreate` fixed it. The cause, reproduced on NixOS: `/run/soloify` is in the writable layer, so a killed run leaves PulseAudio's `pulse/pid` and socket there. Fixed: the entrypoint removes `$XDG_RUNTIME_DIR/pulse` before starting it.
- **A race on first start.** `soloist` isn't on the PATH until the entrypoint finishes fetching it, so a `dc exec` straight after `dc up -d` can print `soloist: not found`. Fixed: the image's `HEALTHCHECK` passes once Soloist is installed and PulseAudio answers, and the runbook uses `up -d --wait`.
- **Not tested here:** fallback 4 (no `b3-state.tgz` on this machine) and whether a phone lists Q2 under host networking.

## Windows 11 ARM

Windows 11 (10.0.26200), ARM64, Docker Desktop 29.9.2, container `aarch64` with Soloist 1.3.10.18 (build 20261027, fetched by entrypoint). Pairing and discovery checks used the Spotify desktop app on this machine. Run from [step0-runbook.md](step0-runbook.md).

- **Fallback 1, host networking: Q2 not listed.** The container's Soloist started on host networking (ZeroConf port 36839), but mDNS from WSL2 was not listed in the Spotify desktop app.
- **Fallback 3, native Soloist: paired.** The x86_64 Windows build (`soloist_release_windows_x86_64.zip`) runs smoothly under Windows 11 ARM64 emulation; listed Q2 in the Spotify desktop app at once (ZeroConf port 53636); `getInfo`, `addUser`, then `pairing complete, credentials stored` and `logged in`, 43 s after `ready`. No firewall prompt was reported.
- **The copied session works.** `dc cp` of `data/native/soloist-state` into the volume, `chown` and `chmod`, and the container's Soloist logged in from it (`logged in: yes`).
- **Play check passed on the default network.** `play` then `now`: `playing | 0:04 / 3:55 | vol 40% | shuffle:on`; `level`: `mean_volume -30.1 dB`, `max_volume -19.1 dB` after `volume 100`; the PC's speakers stayed silent ("Using PulseAudio"). Pause reached Soloist (`paused | 0:51 / 3:55 | vol 100%`).
- **Not tested here:** fallback 4 (no `b3-state.tgz` on this machine) and whether a phone lists Q2 under host networking.

## macOS 26.5.2 (Apple Silicon)

MacBook Air, Docker Desktop 4.97 (server 29.9.2), container `aarch64` with Soloist 1.3.10.18 (build 20261027), which the entrypoint fetched; the native build is the `macos/aarch64` one from `tools/soloist-fetch`. Pairing and the play check used Spotify on a phone on the same Wi-Fi. Run from [step0-runbook.md](step0-runbook.md).

- **Fallback 1, host networking: not usable.** Docker Desktop reported `Host Networking: failed to bind port 5353 on the host`. UDP 5353 is mDNS; on this Mac `lsof` showed a `synergy-s` process on it, and macOS's own responder normally holds it too. The container's Soloist started (ZeroConf port 65095) but the phone didn't list Q2. `dns-sd` wasn't run.
- **Fallback 2, OrbStack: skipped.** Not installed; fallback 3 was chosen instead.
- **Fallback 3, native Soloist: paired from the phone.** `getInfo` (several times), `addUser`, then `pairing complete, credentials stored` and `logged in`, 28 s after `ready`. Whether macOS showed a firewall prompt wasn't reported.
- **The copied session works.** `dc cp` of `data/native/soloist-state` into the volume, `chown` and `chmod`, and the container's Soloist logged in from it (`logged in: yes`).
- **Play check passed on the default network.** `now`: `playing | 0:03 / 3:55 | vol 40%`; `level`: `mean_volume -21.4 dB`, `max_volume -3.9 dB` after `volume 100`; the Mac's speakers stayed silent. Q2 showed as the playing device on the phone, and pause and skip from there worked.
- **Not tested here:** fallback 4, and the Spotify desktop app as a pairing client.
- **Setup notes:** macOS has no `timeout`, so a bounded pairing run needs `perl -e 'alarm N; exec @ARGV'`. An older Docker Desktop (4.12) had to be replaced first; the runbook's 4.34 minimum is real.

## For later steps

- Bridge audio (step 1): `-d q2_soloify` when PipeWire is running, `PULSE_SINK=q2_soloify` on a PulseAudio host, nothing in the container.
- Pairing tool (step 6): `soloify pair`. On macOS and Windows (sh and PowerShell), it fetches the native build, runs `--pair`, and copies `soloist-state/` into the volume. On Linux, it pairs natively or on host networking and prints the ZeroConf port to open.
- Setup docs (step 6): Docker Desktop 4.34 or later. Skip host networking on macOS and Windows. The Spotify desktop app on the same machine counts as a pairing client. Never run two copies of one session.
- Soloist manager (step 2): builds change often (1.3.10.16 to 1.3.10.18 within a day), each with its own 90-day expiry; the planned refresh from the fixed URL covers it. Done in step 2 ([step2.md](step2.md)).
- Podman wasn't tested (not installed); it moves to step 7.
