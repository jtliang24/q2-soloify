# Step 2: bridge core

PLAN.md step 2: Soloist's supervision, its WebSocket, state, covers, controls and the wire's `c`, `s` and `j` frames, against a fake relay. Built and checked on 2026-10-27 on the NixOS laptop, without the device: that network blocked peer-to-peer traffic, so q2remote couldn't reach the M1. The paired Soloist needs no LAN: it logs in from its stored session, and `soloist ctl activate` stands in for casting from the phone.

## What's new

| Module | What |
| --- | --- |
| `bridge/soloify/ws.py` | Soloist's WebSocket: reads `ws.port` and reconnects after each restart; events to the player, commands from it |
| `bridge/soloify/state.py` | events → the status file's lines (docs/wire.md, "State and covers") |
| `bridge/soloify/player.py` | what every relay shares: state and covers sent as they change, Q2 Pod's controls, the volume held at 100, and the stall's pause and resume, now over the WebSocket instead of `soloist ctl` |
| `bridge/soloify/soloist.py` | the build for this machine fetched into `data/soloist/`, refreshed under 14 days from expiry (daily check, restarted when nothing plays) or at once on exit 10 |
| `bridge/soloify/cli.py` | `serve` runs all of it; `--name` sets the Connect name; SIGTERM and SIGINT stop Soloist with the bridge |

## Checks

| Check | Result |
| --- | --- |
| Recorded session (`tests/test_state.py`): Soloist 1.3.10.16's own events for connect, activate, an album, pause, play, seek, next, previous, volume, shuffle, repeat, queue, pause and deactivate | one state line per change Q2 Pod shows, each cover ahead of the state that says `cover=1`, and `set_volume 100` after the volume moved |
| Scripted session over TLS (`tests/test_bridge.py`): the fake Soloist replays the recording, a fake relay sends Q2 Pod's controls | cast, `s`, `P`, `S60000`, `n`, `b`, the phone's volume, deactivate and a Soloist restart give the expected states, covers and commands |
| Live, `soloify serve` with the real Soloist and a loopback relay taking audio | covers are JPEGs (15.9 KB, about 300 px); pause, play and seek reach the relay as a state 0.1 s after the control; a forced stall (3 s without acks) paused Soloist and resumed it with the relay seeing `playing` throughout; SIGTERM stops Soloist too |

`tests/fixtures/soloist_ws.jsonl` is the recording, with the account's playlist replaced by a placeholder. Re-record it when Soloist's events change: start Soloist with `--ws 127.0.0.1:0`, log every frame as `{t, dir, msg}` lines while sending the commands the recording holds (its `out` lines, in order), and check the tests' expectations by hand.

## Findings

- **The WebSocket reference matches what Soloist sends**, with two details it leaves out. A play with a new context sends `playing` and a `playback_state` with an empty item about 75 ms before `track_changed`. And every seek, skip and new context passes through `buffering` for about 0.1 s. The bridge waits 100 ms for each burst to settle and keeps the state across `buffering`, so the relay never sees the old track playing or a sink-stopping blip.
- **A new track's position comes after the track.** `track_changed` carries no position, and the old anchor would put the new track at the old one's position for a moment. The state restarts the position at 0 on a new track until its `position_sync`.
- **The duration moves by a few hundred ms** between `track_changed` (233000) and the next `playback_state` (232666). Q2 Pod gets a second state line, which is harmless.
- **A stall's pause must not reach Q2 Pod as `paused`.** In the first live run the relay was told `paused` during the stall. For Q2 Pod that frees the DAC (`spot_yield`'s signal) and starts the 30 s idle stop, which would kill the relay during a long outage. The bridge now says `playing` while the pause is its own. Once Soloist resumes, the position jumps back by the stall's length: Soloist was paused while Q2 Pod's clock ran on.
- **Soloist counts expiry differently.** It says "client expires in 88 days" where the build date plus 90 days gives 89. With a 14-day margin the difference doesn't matter.
- **A background `soloify serve` ignores SIGINT** when started from a non-interactive shell (the shell sets SIGINT to ignored). systemd and Docker send SIGTERM, which now stops cleanly.
- The Nix launcher prints `ld-linux…: unrecognized option '--type=crashpad-handler'` at each start: Soloist re-executes itself for its crash handler, through the loader the launcher used. Playback is unaffected; crash reports aren't collected.

## For later steps

- Step 3: the relay writes `s` frames to `/tmp/q2-librespot.state` with `at=`, delayed and shifted by the buffer it plays behind Soloist, and `j` to `.state.jpg`. It forwards control datagrams as `c`. The relay's own pause (`A 0 pause`) and Q2 Pod's `s` will both pause Soloist: decide which one the relay sends, so the bridge's `paused_by_us` stays right.
- Step 4: `get_queue` and `add_to_queue` already work over the WebSocket; the `q`/`r` frames are next.
- Step 6: the container's entrypoint still fetches Soloist with `tools/soloist-fetch`; the bridge can do it now.
