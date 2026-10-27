# Step 0: server spikes

PLAN.md step 0, before any bridge or relay code. For each host: does Soloist's audio land in the `q2_soloify` null sink, and only there, and which pairing path works? Results so far are on the NixOS laptop. The MacBook Air and the Windows machines follow [step0-runbook.md](step0-runbook.md).

Soloist 1.3.10.16 (build 20261026, "client expires in 89 days"), the account the user plays from, a phone on the same Wi-Fi. `soloist ctl -D <data>/soloist-state activate` then `play URI` drives a paired Soloist without the phone; `tools/soloify-sink level` reads the sink's monitor (silence is -91 dB).

## Summary

| Host | Audio | Pairing path |
| --- | --- | --- |
| NixOS native (PipeWire 1.6.6) | `-d q2_soloify` | LAN, with Soloist's ZeroConf port open in the firewall while pairing |
| NixOS, Docker 29.9.1 | the container's PulseAudio, default sink | host networking (`compose.linux.yaml`) once, then the default NAT network; or a session copied in from another Linux machine |
| macOS (Apple Silicon) | pending | pending |
| Windows 11 x86_64 | pending | pending |
| Windows 11 ARM | pending | pending |

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

## For later steps

- Bridge audio (step 1): `-d q2_soloify` when PipeWire is running, `PULSE_SINK=q2_soloify` on a PulseAudio host, nothing in the container.
- Setup docs (step 6): pair once (host networking or a native run, the firewall open for that run's port), then run on any network; or pair anywhere and copy `soloist-state/`. `soloify pair` could print the port to open.
- Podman wasn't tested (not installed); it moves to step 7.
