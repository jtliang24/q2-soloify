# Immediate pause and rewind

Implemented 2026-10-30; partial physical M1 validation found **resume and rapid-toggle
failures** (see results below). The full matrix remains incomplete. This replaces the
explicit drain-on-pause policy in [step4-followup.md](step4-followup.md). Those
older listening results remain evidence for that implementation, not this one.

The [2026-10-31 follow-up](pause-rewind-followup.md) records subsequent fixes,
regression results, partial M1 retesting, verified restoration and the remaining work.
The historical results below are preserved.

Pause snapshots the hardware playback cursor, kills/reaps the decoder and output
process group, discards the ring and pending metadata, and publishes a frozen
paused position. It does not wait for the buffer or Soloist's pause fade. The
bridge pauses Soloist and seeks back to that heard position. If the source has
already crossed a natural track boundary, verified Previous navigation restores
the heard occurrence without replacing its playlist/album context. If Previous
passes a manually queued item, verified upcoming entries and Next reach that item.
Resume primes
fresh capture before Play and refills the normal buffer. There is no retained
PCM/Opus splice; Soloist's normal resume ramp belongs at the beginning.

Before output starts, Pause cancels buffering without starting the sink. It saves
the first pending presentation sample. With no packets yet, it uses a known source
starting anchor. Missing timestamps, an unowned/unreadable hardware cursor, pointer
reset/XRUN, changed context, unavailable history or an ambiguous queue leave output
paused. Errors are logged by bridge and relay and written to
`/tmp/q2-librespot.pause-error` (or the `SOLOIFY_Q2POD` prefix). Play retries the
retained bookmark; selecting a track, skipping or explicitly seeking replaces it.
Opening menus and automatic stall recovery cannot resume it.

The relay discovers playback substreams from `/proc/asound/pcm`, checks that the
hardware owner belongs to the sink's process group, and reads `status` and
`hw_params`. Hardware frames divided by the actual hardware rate map to the
presentation timeline, including decoder pre-skip and end trim. USB card numbers
and rates are not fixed. Capture timestamp anchors are sent per presented Opus
packet (20 ms); a source event burst may settle for up to 100 ms before its packet
is sent. This does not promise sample-exact Spotify seeking. A missing or invalid
clock fails safely instead of subtracting the configured buffer target.

The internal bridge/relay protocol is **version 2**; deploy both together. Firmware
control datagrams and Q2SPOT/1 are unchanged. Keep the 1-second LAN / 4-second remote
buffer and 160 kbit/s defaults. Headphone DAC and USB outputs are in scope;
Bluetooth is excluded because it cannot run alongside Wi-Fi on this device.

## Checks without the device

Run from this checkout; preserve the command output with the revision and date:

```sh
nix develop
python -m pytest -q
ruff check .
ruff format --check .
python relay/build.py --host
python relay/build.py --rootfs ../q2-pod/build-m1/stock.squashfs
sha256sum build/relay/q2-soloify
```

`test_contract.py` checks immediate shutdown, frozen metadata, discarded tails,
fresh refill, startup cancellation, phone controls, failures and rapid toggles.
`test_pause_clock.py` exercises real C parsing against simulated headphone/USB
proc trees, different cards/rates, ownership, resets, XRUN and unplug.
`test_rewind.py` checks asynchronous source confirmation, queue occurrence identity,
Previous passing a queued item and the verified Next path back to it,
context/options, failure/retry, cancellation and phone ordering. The codec tests
retain independent-generation, pre-skip, predictive-history and exact end-trim
checks on host ffmpeg and the stock MIPS decoder under QEMU. Software success
cannot establish actual DAC release latency or listening quality.

## M1 validation runbook (deferred)

Use manual device navigation and the phone's Spotify controls for the functional
checks. Do not flash firmware or attempt Bluetooth playback. Run host commands
from this checkout in `nix develop`.

1. Follow the network, authorization and single-bridge prerequisites in
   [step4-runbook.md](step4-runbook.md#prerequisites-and-connections). Substitute the
   current M1 and bridge LAN addresses. Create a unique private evidence directory:

   ```sh
   export Q2REMOTE=../q2-pod/tools/q2remote.py
   export Q2_HOST=192.168.1.210  # replace with current address
   export BRIDGE_LAN=192.168.1.100  # replace with current address
   export EVIDENCE="$(mktemp -d build/pause-rewind-m1-XXXXXXXX)"
   chmod 700 "$EVIDENCE"
   git rev-parse HEAD > "$EVIDENCE/revision.txt"
   git diff > "$EVIDENCE/changes.patch"
   soloist --version > "$EVIDENCE/soloist-version.txt"
   python "$Q2REMOTE" state > "$EVIDENCE/device-before.json"
   sha256sum build/relay/q2-soloify > "$EVIDENCE/relay.sha256"
   ```

   Record installed firmware version, output mode, volume, standby/screen settings,
   selected backend, shuffle/repeat, and original service state. Preserve the user's
   current volume (the previous pass left it at 18); do not silently set it to an
   older recorded value. Keep credentials and generated `remote` private.

2. Stop playback and competing streaming backends through the UI. Use the backup,
   file manifest and checksum procedure in
   [Safe preparation](step4-runbook.md#safe-preparation-and-verified-relay), choosing
   a **new** backup directory such as `.soloify-pause-backup-YYYYMMDD-HHMMSS` everywhere
   in that procedure. Back up `librespot`, `q2-soloify`, `run`, `aplay.sh`, `remote`,
   their modes and recorded absences. Verify the backup checksums before replacing
   anything. Preserve cache, pairing and all unrelated configuration. Install the
   newly built **MIPS** relay and matching version-2 bridge. The existing launcher
   may prefer `q2-soloify` over `librespot`: verify the executable actually launched.
   Use the installed Spotify compatibility route or existing Soloify row. Never
   install new firmware to add a missing UI route.

3. Start the matching bridge with logs and select the device from Spotify:

   ```sh
   python -m soloify.cli --data data serve --listen 0.0.0.0 --port 7443 \
     > "$EVIDENCE/bridge.log" 2>&1 &
   export PAUSE_BRIDGE_PID=$!
   ```

   Open playback manually. Verify a successful TLS hello in the relay log and the
   installed binary's checksum. Before listening tests, capture all substreams:

   ```sh
   python "$Q2REMOTE" sh '
   cat /proc/asound/pcm
   for p in /proc/asound/card*/pcm*p/sub*; do
     echo "$p"
     cat "$p/status" "$p/hw_params"
   done
   ps
   cat /tmp/q2-librespot.state
   ' > "$EVIDENCE/playing-preflight.txt"
   ```

   Repeat that capture after about one second. Identify the `RUNNING` substream,
   actual hardware rate and owner PID/process group; `hw_ptr` must advance. Check
   USB independently, including a USB device at a different hardware rate if
   available. An absent hardware measurement is a failure to investigate, not
   permission to use the old wall-clock estimate.

4. Execute this matrix at **both 1000 ms and 4000 ms targets** on stable LAN. Use
   separate temporary `remote` configurations with `buffer=1000` / `buffer=4000`,
   reconnect between them, and verify the logged target. No default changes are
   necessary. For each row record pass/fail/deferred, timestamps and evidence:

   | Case | Expected |
   | --- | --- |
   | Headphones, hardware Pause/Play | Immediate silence, frozen heard position, ALSA released while paused, fresh refill resumes at bookmark. |
   | Headphones, phone Pause/Play | Same behavior after local receipt of the phone event; no premature audio from the source's ahead position. |
   | USB, hardware and phone | Same behavior; discovery follows actual card/substream/rate and preserves software volume/output transformations. |
   | Pause immediately after Play | Cancel zero/partial buffer; no delayed burst and no sink launched solely to drain it. Resume preserves the opening. |
   | Pause just before output begins | Either no output, or an immediately stopped hardware epoch with the corresponding bookmark. |
   | Pause near natural track end | When source is on the next track but listener still hears the old one, restore the heard occurrence and position; context continues correctly afterward. |
   | Duplicate tracks, shuffle/repeat | Restore the right occurrence where history identifies it. Unavailable/ambiguous history stays paused with a diagnostic. |
   | Pause/Play/Pause and rapid cycles | Final choice wins; stale state cannot start output. Repeat at least ten cycles. |
   | Reopen menus while paused | Position remains frozen and no source Play/capture restart occurs. |
   | Restore failure and retry | Disconnect/reconnect bridge or source while paused, then explicitly Play. Output stays off on failure, diagnostic is available, and a recoverable bookmark retries correctly. |
   | Seek/skip/browse-play during restore | New action wins; no later rewind jumps back over it. |
   | USB unplug or pointer reset | Immediate safe stop; invalid clock never silently becomes a guessed bookmark. |

5. Measure the timing with a continuous recording of headphone/USB output and a
   timestamped local pause marker. Relay diagnostics include `pause drop`, bookmark
   ID/validity/position and restore ready/failure. Use receipt at the relay, rather
   than the phone's tap time, to separate network latency from shutdown latency.
   Correlate recorded audio with a known track excerpt to compare the last audible
   position and resumed excerpt. Target silence within **100 ms of local receipt**
   and resumed position within **100 ms** for ordinary seekable tracks. Record raw
   measurements; seek granularity/source behavior may require investigation.
   A status file or process check alone cannot prove those timing targets.

6. After every pause, capture the state and all ALSA statuses again. The output
   owner must be gone/closed before the paused state is visible. After resume,
   listen beyond the entire configured buffer plus several seconds: no delayed
   fade, retained tail, repeat or skip at the old-buffer boundary. Inspect errors:

   ```sh
   python "$Q2REMOTE" sh '
   cat /tmp/q2-librespot.state
   if [ -f /tmp/q2-librespot.pause-error ]; then cat /tmp/q2-librespot.pause-error; fi
   tail -n 100 /mnt/mmc/.spotify/spot.log
   ' > "$EVIDENCE/pause-diagnostics.txt"
   ```

   Adapt the log path if the installed route uses `.soloify`. Compare paused-state
   snapshots a second apart and after reopening menus; `position` and `at` should
   stay frozen except for a late cover update. Record listening findings separately
   from protocol/process evidence. Deferred cases need a reason.

7. Stop the temporary device backend and the bridge (`kill "$PAUSE_BRIDGE_PID"`),
   restore the original manifest from the unique backup, remove only files recorded
   as originally absent, and verify every original checksum and mode. Follow
   [cleanup](step4-runbook.md#cleanup-and-restoration) using the same backup name . Restore output, volume,
   standby/screen settings, shuffle/repeat and service state. Verify the original
   backend starts, then leave it in the user's original state. Remove only firewall
   rules/services created for this pass. Keep the backup and evidence; do not
   overwrite earlier validation artifacts.

## Results

Validation on 2026-10-30:

- Live Soloist 1.3.10.16 restored a nonzero bookmark across a track change through
  actual queue navigation: requested **1534 ms**, confirmed **1533 ms**, paused on
  the heard track. The check verified the null-sink route, restored shuffle/repeat,
  and shut the temporary Soloist down. This measures source API confirmation;
  the bookmark was synthetic and no hardware cursor/listening measurement is claimed.
- The live check exposed Previous passing a manually queued item and a queue order
  that could not be treated as playback order. The implementation now confirms each
  visited occurrence and follows checked Next entries when necessary. Earlier failed
  attempts safely stayed paused. Private evidence is in
  `build/pause-rewind-live/queue-navigation-verified.json`; prior validation files
  were preserved.
- Final offline suite: **186 passed, 1 skipped** in 129.76 seconds. The skipped
  opt-in live Spotify test passed separately as described above. This suite includes
  the stock MIPS relay/ffmpeg tests under QEMU; these were executed, not skipped.
- `ruff check .`, `ruff format --check .` (50 files) and `git diff --check` pass.
  Host and stock-rootfs MIPS builds pass with compiler warnings treated as errors.
- MIPS artifact: `build/relay/q2-soloify`, SHA-256
  `967096297d2aaf4c6d7e6a5354d123ccd5030cdc9729a15882b0475c6310dc40`.
- The launcher test now waits for its documented one-second concurrent-start
  guard before testing Stop, so an unfinished startup request cannot race cleanup.
  The launcher itself was not changed.

To repeat the live check (paired Soloist and OAuth required; stop competing Soloist
instances first), keep evidence separate from earlier runs:

```sh
SOLOIFY_LIVE=1 SOLOIFY_LIVE_RESULTS="$EVIDENCE/live-results.json" \
  python -m pytest -q tests/test_live.py -s
```

The live test keeps audio in the null sink, exercises real queue navigation and
source confirmation, restores shuffle/repeat, and pauses/stops its temporary
Soloist. Its source track transition is driven by Next; natural boundaries and
hardware timing still need the M1 matrix above.

On-device latency and resume alignment measurements remain **pending**. The partial
physical pass below does not establish the timing targets or complete the matrix.

### Partial M1 pass, 2026-10-30 (EDT; evidence timestamps are UTC)

Tested checkout `47c5c66827e19f5710014001a1e25df25c14ad03`, without implementation
changes, on M1 firmware **1.5.0 dev 13**, through the installed Spotify compatibility
route. Soloist was **1.3.10.16**. The installed MIPS SHA-256 matched
`967096297d2aaf4c6d7e6a5354d123ccd5030cdc9729a15882b0475c6310dc40`.
Fresh offline verification passed: **186 passed, 1 skipped in 132.19 seconds**;
the opt-in live test was skipped. Ruff lint/format and host/MIPS builds passed.
An initial restricted test run was interrupted; these results are from the completed
run with local socket access.

Private evidence is in `build/pause-rewind-m1-qfz1yvxi/` (mode 0700).
`matrix.json` records all 50 output/buffer/case combinations separately, with
functional, process and listening observations: **2 pass, 5 fail, 43 deferred**.
These are case results, not a complete output certification. Deferred cases were
not executed; quick toggles failed before ten completed cycles could be confirmed.

| Output / target | Check | Functional and process result | Listening result |
| --- | --- | --- | --- |
| Headphones / 1000 ms | Hardware Pause/Play: **fail** | Paused position and `at` froze; ALSA closed and output children exited. Bookmark invalid; restoration failed. Explicit Play stayed silent. | User confirmed prompt silence and frozen position; Play did not resume. |
| Headphones / 1000 ms | Phone control: **fail** | Subsequent phone Play requests returned to paused with restore-failure diagnostics; fresh phone playback could not be established. | User reported Spotify Connect would not play. Phone Pause from audible playback was not independently established in this case. |
| Headphones / 4000 ms | Hardware Pause/Play: **fail** | Same safe shutdown, followed by invalid bookmark and failed restoration. | User confirmed prompt silence/frozen position; Play remained silent. |
| Headphones / 4000 ms | Reopen menus: **pass** | Playback remained paused, with ALSA closed and no output children. Subsequent explicit Play still failed. | User confirmed menus left playback paused. |
| USB / 4000 ms | Phone Pause/Play: **pass** | Bookmark valid at 29900 ms; restore ready. Paused snapshots froze and both ALSA streams closed. Resume used fresh capture and a new output owner. | User confirmed resume at the pause point and no problems heard beyond ten seconds, covering the buffer boundary. |
| USB / 4000 ms | Rapid hardware toggles: **fail** | Valid bookmarks preceded a bridge stall, timeout and reconnect. Relay remained paused with ALSA closed while Soloist advanced. Cycle count was not confirmed. | User reported silence and frozen M1 counter while the phone scrubber continued. |
| USB / 1000 ms | Hardware Pause/Play: **fail** | After a track change, bookmark invalid; paused snapshots froze and ALSA closed. Explicit Play failed. | User confirmed audio stopped and position froze; Play did not restart audio. |

The invalid-bookmark diagnostic was `No reliable audible bookmark; select a track
or seek`. Headphone metadata also lagged a source track change. These failures were
recorded before restoration; no fix was attempted during the pass. USB's one
successful restoration does not establish reliable restoration across generations
or rapid controls.

Both output paths had advancing hardware pointers at **44100 Hz**. Headphones used
card 0 with `aplay`; USB used card 1 with `q2video` through the existing `usb h16`
output route. Owner/decoder process-group membership was checked independently
for each output. No USB device at another hardware rate was tested.

The M1 was at `192.168.1.210`. Ethernet bridge `192.168.1.200:7443` timed out;
Wi-Fi `192.168.1.156:7443` connected and both targets were verified in relay logs.
Wi-Fi also suffered interruptions, including the rapid-toggle timeout, so the
stable-LAN prerequisite was not maintained throughout. No firewall rule was changed.
The old UI did not relaunch the stopped service after reconfiguration; the existing
card launcher was started directly after verifying it was stopped. This is setup
evidence, not a firmware UI launch pass.

Startup/pre-output cancellation, natural boundaries, duplicates/shuffle/repeat,
controlled recoverable restore failure/retry, action precedence during restoration,
USB unplug, and remaining control/output combinations are **deferred** after the
baseline failures and connection interruptions. No synchronized audio recording
was made: **100 ms silence and alignment targets are unmeasured**. Pointer-reset
injection remains deferred; automated reset/XRUN/unplug coverage is separate evidence.

Restoration completed before documenting these results:

- Fresh backup `.soloify-pause-backup-qfz1yvxi-verified` retains checksums, modes
  and recorded absences. An earlier partial backup is preserved: the first attempt
  stopped because this firmware lacks `stat`; no installed files had been replaced.
- Original `librespot`, `run` and `aplay.sh` checksums and modes match; originally
  absent `remote` and `q2-soloify` are absent again. The card filesystem reports
  mode 0777, including for the temporary configuration despite `chmod 600`.
- Original backend launched and authenticated, then was stopped to restore the
  initial service state. ALSA is closed; no test relay/decoder/player remains.
  The original AirPlay daemon remains present as it was at the start.
- Bridge/Soloist stopped and TCP 7443 listener closed. Generated local `remote`
  copies and verification sockets were removed; private evidence/backups remain.
- Final device `config.ini` matches the starting file byte for byte, including
  volume **18**, screen/standby, headphone output and USB mode. The user confirmed
  phone options restored; their initial values were not independently recorded.

Follow-up requires investigating invalid bookmarks on real headphone/USB epochs
and the rapid-toggle stall, then repeating the deferred matrix on a stable link.
