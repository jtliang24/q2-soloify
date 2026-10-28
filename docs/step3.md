# Step 3: the relay contract

The relay now implements librespot's state, controls and sink lifecycle behind `-q`. The card's `run` enables it; without `-q`, the audio-only test source still works. Browsing and OAuth remain step 4, and the firmware's Soloify row remains step 5.

## Implementation

- `/tmp/q2-librespot.state` is written whole through `.new` and renamed. `at=` uses the device's monotonic clock. Metadata waits on the audio packet clock, so a natural track change and its cover follow the buffered audio, including through a pause or refill. At startup and after a seek, the initial position is shifted by the target and anchored again when the first buffered audio starts. The decoder/ALSA delay is not measured by this clock; step 1 measured up to about half a second more than the target.
- Covers are cached by track ID (four bounded entries), installed atomically before `cover=1`, and also update while paused. The receiver accepts the wire's full 1 MiB frame limit, including fragmented frames; a larger frame closes the link and reconnects.
- The control datagram socket accepts `p`, `P`, `s`, `n`, `b` and `S<ms>` (also `S <ms>`). Toggles use the relay's local state. Pause kills and reaps the decoder/player group before publishing `paused`, preserves the queued packets, forwards one explicit `s`, and stops audio requests. The bridge treats this as a user choice, separate from its stall pause.
- Skip and seek clear buffered audio and pending metadata. The bridge drains the previous encoder and echoes the completed command in an ordered `x` boundary before its new snapshot. The relay acknowledges but discards old audio until the final outstanding reset boundary. Explicit pause/play boundaries also prevent stale state from undoing rapid toggles.
- The launcher backgrounds itself, owns the pid file, respects an existing backend and restarts the relay with 5–60 second backoff. The relay has a kernel lock for its sockets, cleans up on TERM/INT, and exits if its launcher dies. Sink children have parent-death signals as well as the normal process-group shutdown, so a crash releases the PCM.
- A bounded, nonblocking Q2SPOT/1 socket serves only the minimum page access: `account` succeeds with no account rows when the bridge reports an active session, `caps` advertises no browsing operations, and other operations answer `unsupported`. Missing configuration answers `error` with the copy-file guide; a disconnected bridge answers `offline`. Retry wakes the reconnect backoff and reloads `remote`.

The launcher alone writes `/tmp/q2-librespot`. The relay never writes a download-progress file or changes `aplay.sh` or its output-device contract. Paths can be isolated with `SOLOIFY_Q2POD` for tests.

## Automated checks

The host integration tests exercise the actual C relay over TLS, alongside the bridge's fake Soloist tests. Coverage includes a fragmented maximum-size cover, rejection of an oversized frame, packet-clock metadata, initial position anchoring, late covers while paused, immediate sink shutdown and buffered resume, rapid controls with stale replies, discarded pre-seek audio, malformed browse requests, missing configuration and Retry, singleton ownership, crashes, launcher restart/backoff and an existing backend's pid. The MIPS test uses the stock rootfs's glibc/OpenSSL and ffmpeg under QEMU.

The final full run passed **65 tests** in 58.70 s, including the MIPS/QEMU check. `ruff check .`, `ruff format --check .` and `git diff --check` passed. The device pass exposed the truncated-cover bug below; the complete-body, late-cover, initial-position and ownership regressions are included in that final run.

## M1 pass, 2026-10-28

The M1 was reachable at home over q2remote. The bridge ran on the NixOS laptop's Ethernet address, with TCP 7443 opened using `nixos-firewall-tool` by the user. No firmware was flashed. This installed firmware opens Spotify from the older **Q2 Pod hub**, rather than the newer Streaming layout in the current q2-pod checkout.

Original `.spotify/librespot`, `run` and `aplay.sh` were copied to `.soloify-step3-backup-20261028/` and checksummed before the relay replaced the binary and launcher temporarily. Cache and saved login were preserved. The compatibility installation had its own temporary `.spotify/remote`; the normal `.soloify/` files were left alone. Screen Off was set to Off for the checklist.

| Check | Result |
| --- | --- |
| Launch through the existing Spotify row | The launcher starts once and the relay connects with the pinned certificate; Retry after activating Soloist opens the menu |
| Now Playing | Correct title, artist, album and advancing position; the first pass exposed incomplete artwork (below) |
| Play/Pause | `paused` with ALSA closed and no decoder/aplay processes; play resumes; the user confirmed audio and controls sound normal |
| Side buttons and phone controls | Track changes reach the relay and playback continues |
| Seek | A temporary helper sent `S60000` to the device's actual control socket; the resulting state was `position=60102` and playback resumed |
| Scrub gesture | Deferred: the installed build's centre gesture toggles the screen instead of entering scrub; the touch bar did not send a seek. No firmware changes were made |
| Local music handover | Starting Bucket pauses the relay; ALSA then belongs to hciplayer (pid 1106). Recasting returns the PCM to the relay's aplay |
| Idle stop | With the Spotify browser closed, the paused relay/launcher and state disappear after the idle interval; local playback continues |
| Crash | SIGKILL to the temporary relay closes ALSA; its launcher restarts it after five seconds and it plays again |
| Shared stop command | The exact `SPOT_KILL` shell command ends the launcher, relay, ffmpeg and aplay, and ALSA closes |
| Stop Streaming Services / Rockbox rows | Their UI routes were not exercised: those rows were not found in this installed hub. The shared command was checked directly; UI coverage waits for the firmware step |
| User-run 3 s port block | The bridge pauses at 1020 ms in flight, resumes automatically, and the ring returns to about 0.7 s. The user heard about three seconds of interruption |
| User-run 15 s port block | The relay detects six seconds without traffic, reconnects once the port opens and resumes by itself. The ring returns to about 0.7 s with no growing backlog |

All three original Spotify files were restored and their SHA-256 checksums matched. The temporary `.spotify/remote` and RAM helper were removed; Screen Off returned to 1 min (`LIGHTTIME=3`), standby remained Off, and volume was restored to 20. The on-card backup remains available.

### Finding after the pass: gray artwork

The source JPEG on the device was only 15,880 bytes and lacked its JPEG end marker; the thumbnail and screen both showed a gray lower half. The bridge had used a single `aiohttp` `read(n)`, which can return the first network chunk before the body ends. The fix consumes the body to EOF within the cover-size limit and rejects incomplete JPEGs. A regression test serves an HTTPS image in separate chunks and checks the full image, caching, incomplete images and the size limit.

A focused M1 recheck with the final relay (`04d448dedf92babe3e6fb2d9020998b15048ffab18110d32e4b3aa75caddf394`) confirmed the corrected Discovery JPEG: 21,737 bytes, a complete end marker, and the whole cover visible on Now Playing. The initial state anchored at `position=47` as the buffered output started. Afterward the original Spotify files were restored and checksummed again, all test playback processes stopped, the temporary pairing file removed, and settings verified as `LIGHTTIME=3`, `STANDBYMODE=0`, `VOLUME=20`.

## Remaining firmware coverage

Step 5 should repeat scrub through the actual UI, Stop Streaming Services and the Rockbox row on a matching firmware build. Full browsing, queue operations and account details are step 4; settings, Apps installation and the separate Soloify backend are later steps. The battery and hotspot measurements from step 1 were not repeated.
