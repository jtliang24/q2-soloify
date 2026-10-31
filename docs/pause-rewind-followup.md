# M1 pause/bookmark follow-up — 2026-10-31

The implementation and regression tests were completed on top of `6ea488e`.
Physical validation is **partial**, not a completed M1 certification. Work stopped
at the user's request after restoring the device and the laptop's temporary network
setting; the user then authorized committing the verified work.

Keep [the original runbook and October 30 results](pause-rewind.md) as historical
evidence. This report records the additional work; it does not replace those results.

## Implementation progress

- **Hardware clock lifecycle:** owned ALSA `OPEN`, `SETUP` and `PREPARED` observations
  before the first `RUNNING` clock no longer permanently invalidate it. Those states
  after an established epoch still invalidate the clock. Tracking resets when a new
  player starts and survives decoder changes that retain the player. XRUNs, resets,
  rate/owner changes and missing hardware remain unsafe for bookmarking.
- **Diagnostics:** relay logs distinguish startup, unavailable hardware, ownership,
  reset/XRUN, invalid rate, discarded presentation samples, pointers beyond capture,
  and missing timeline anchors. Logs include capture/decoder generations, hardware
  owner/group, pointer/rate, selected token and presentation position without credentials.
- **Latest command:** intent revisions are recorded when controls arrive. Obsolete
  restoration and Play work checks its revision after asynchronous operations;
  superseded controls still finish their protocol boundaries. Navigation keeps a
  bookmark until it is ready to dispatch its replacement mutation.
- **Capture ownership:** each capture owns its readiness, release and shutdown events.
  Old pumps cannot signal a newer capture. A newer Pause wakes a pending readiness
  wait; the frame reader continues processing ACKs during restoration and shutdown.
- **Source events:** queried/reconnect snapshots are not explicit Play commands.
  `buffering` notifications are not Pause commands. Explicit redundant phone Pause
  notifications can still supersede a pending Play.
- **Browse-play:** mutations suppress their own intermediate source notifications,
  confirm fresh playing state before the boundary, and reopen the capture gate.
- **Paused seek:** an encoder primed for a seek that stays paused is stopped. The next
  Play starts a new capture, so it cannot reuse a header the relay discarded while gated.
- **Recovery:** source `none`/`stopped` observations retain an active local pause's
  bookmark and frozen metadata. Explicit Play can retry after source reconnect.
- **Repeat safety:** a repeat-track lap that did not advance the queue cannot select
  an older duplicate from Previous. It reports `Repeat-track occurrence is ambiguous`
  and stays paused; an explicit seek/selection can replace that bookmark.
- **Last change, offline only:** explicit Play of a retained bookmark now primes
  capture even when a new connection has not sent its first audio request. The new
  cold-reconnect regression failed before this condition was added and passes afterward.
  This last condition was added **after the final M1 listening pass**.

Firmware controls, Q2SPOT/1 and protocol v2 wire formats are unchanged. Defaults remain
1000/4000 ms and 160 kbit/s. No elapsed-time or configured-buffer estimate was added
to bookmark selection.

The main implementation is in `relay/sink.c`, `relay/audio.c`, `relay/q2pod.c`, and
`bridge/soloify/{audio,player,rewind}.py`.

## Offline verification

The exact stopping-point code passes the full suite: **215 passed, 1 skipped** in
186.84 seconds. The final cold-reconnect condition also passes the focused
control/rewind/relay set: **93 tests**. Full output is saved as
`build/pause-rewind-m1-venkqu0c/pytest-stopping-point.log`; earlier completed runs
remain separate.

Ruff lint and formatting pass (51 Python files). Host and stock-rootfs MIPS builds
pass with compiler warnings treated as errors. The full suite executes the stock
MIPS relay/decoder checks under QEMU; the single usual skip is the opt-in standalone
live Spotify test. The physical pass used real Soloist separately.

MIPS relay SHA-256:

```text
068d96d09d858e35b6a97f1257d6b517a539f3de684be19c40b60023d382edd4
```

The stopping-point artifacts are in `build/pause-rewind-m1-venkqu0c/{host,mips}/`.
The Python-only final condition does not change the relay binary.

Regressions now cover:

- Startup ALSA transitions, including `PREPARED → SETUP → PREPARED → RUNNING`, both
  process-group owners, multiple cards and 44100/48000/96000 Hz hardware rates.
- One hardware epoch spanning a trimmed/pre-skipped decoder generation and a track
  change, repeated resume epochs, reset/XRUN/unplug, invalid rate and ownership rejection.
- The real C relay with delayed source replies and shutdown: ten rapid cycles at
  both buffer targets, ending in Pause, Play, browse-play, and paused seek then Play.
- Fresh refill, closed/reaped output, frozen metadata, responsive ACKs, source loss
  and reconnect, retained failure/retry, stale responses, and navigation supersession.
- The captured real Soloist `buffering → playing` sequence and repeat-lap ambiguity.

Several new tests were run against preserved earlier code and failed before their
corresponding fixes. Do not drop those tests because an earlier physical baseline passed.

## Physical evidence and its limits

Tests used the existing Spotify compatibility route at `/mnt/mmc/.spotify`, real
Soloist **1.3.10.16**, and user listening through headphones and USB. No firmware was
flashed. Hardware rates observed in this session were 44100 Hz. Other rates are
covered offline, not by a second physical USB device.

| Configuration | Evidence obtained | Status at stopping point |
| --- | --- | --- |
| Headphones / 1000 ms | On relay `068d…`, hardware and phone Pause/Play, ten rapid cycles ending in Pause, listening beyond ten seconds, and menus remaining silent/frozen all passed. Direct control probes also passed startup cancellation, seek/skip/browse precedence, natural-boundary restoration with a uniquely identified queue UID, controlled source outage/reconnect/explicit retry, and safe repeat ambiguity plus explicit-seek recovery. Resumes required advancing hardware pointers. | Passed before the final cold-reconnect-only condition; retest that new condition on hardware. |
| Headphones / 4000 ms | Earlier in the session, hardware/phone controls and ten rapid cycles ending in audible Play passed, with listening beyond ten seconds. | Earlier bridge/relay revision; repeat after the later control/recovery fixes. |
| USB / 4000 ms | Earlier hardware/phone controls and ten rapid cycles ending in silent, frozen Pause passed. | Earlier revision; repeat with the stopping-point builds. |
| USB / 1000 ms | Earlier hardware/phone controls after a track change, plus ten rapid cycles ending in audible Play, passed beyond ten seconds. | Earlier revision; repeat with the stopping-point builds. |

The three earlier buffer/output baselines used relay hash
`d7f91452cec7997f8a7a22e5b567fa81da896829dda85693ccd3b1c42cde7d82`.
Later deferred tests exposed additional races, so these listening results must not
be presented as verification of the final implementation.

On the later headphone/1000 ms natural-boundary probe, the source had advanced to
another track, then restored the heard occurrence paused at **208584 ms**. Queue
UID comparison identified the heard entry and confirmed that it became current
again. This is source/protocol evidence, not a synchronized audible-alignment measurement.

### Setup problems that must not be mistaken for passes

- An open ALSA stream was sometimes `RUNNING` with its pointer stuck at the opening
  value. Source playback and process existence alone were false positives. Reopening
  the actual Spotify route through the M1 UI restored audible playback. Later probes
  required two advancing pointer observations; earlier automatic results on the
  inactive route are not successful audio verification.
- The laptop's Wi-Fi power saving was initially **on**. The link missed the 1000 ms
  ACK deadline, caused source stalls and actual ALSA XRUNs, and correctly left invalid
  clocks un-restorable. After the user temporarily disabled host power saving, a
  short ping check improved from roughly 250 ms to **4–9 ms**, and the subsequent
  headphone diagnostic group completed without those interruptions. This is a short
  link observation, not a long-duration reliability certificate.
- The wired bridge address `192.168.1.200:7443` timed out. The working bridge address
  was `192.168.1.156:7443`; the M1 was `192.168.1.210`. Verify current addresses and
  routing before reusing them. No firewall rules were changed.
- One diagnostic tried a Liked Songs context without the required track offset and
  received the expected `bad` reply. Its corrected playlist-based rerun passed.
  Validate the fixture arguments before interpreting a probe failure as a playback bug.

No synchronized audio recording was made. **The 100 ms silence and resume-alignment
targets remain unmeasured.** Do not infer them from status files or user reports of
prompt silence.

## Restored state and retained artifacts

Restoration was verified before writing this report:

- Original `librespot`, `run`, and `aplay.sh` hashes/modes match. Originally absent
  `remote` and `q2-soloify` are absent again. The original backend authenticated during
  an intermediate restoration check; the final service state is stopped.
- Final `config.ini` matches the session's starting file byte for byte. Volume is
  **18**, headphone output and the original screen/standby and USB settings are preserved.
- ALSA is closed. No test relay, decoder, player or control helper remains. The
  pre-existing AirPlay daemon was left alone.
- Bridge/Soloist are stopped and TCP 7443 has no listener. Generated credential-bearing
  local `remote` copies were removed; original pairing/cache and unrelated files were preserved.
- Source shuffle/repeat were restored to the original **false/off** values. The user
  restored laptop Wi-Fi power saving to **on**, and this was verified with `iw`.

Fresh backup manifests were made before each deployment. The card filesystem reports
mode 0777 and cannot enforce private permissions. Verified compressed originals are
also retained under private mode-0700 directories in `/mnt/data/`, with mode-0600
archives. Later checksum-identical snapshots use hard links to that immutable archive
to avoid filling internal storage; their fresh manifests were compared with the restored
original files. All earlier backups remain.

The latest backup is `/mnt/mmc/.soloify-pause-backup-m1-venkqu0c`, with its private
archive at `/mnt/data/.soloify-pause-backup-m1-venkqu0c/original.tar.gz`.

Private evidence directories, all mode 0700, are retained separately:

| Directory under `build/` | Purpose |
| --- | --- |
| `pause-rewind-fix-4cobpcr8/` | Initial fixes; four output/buffer listening baselines; additional failures and first verified restoration. |
| `pause-rewind-fix-final-uiaftaqu/` | Snapshot/browse fixes; source-loss recovery failure and restoration. |
| `pause-rewind-verified-v1yr182z/` | Recovery fixes; inactive-clock and unstable-link evidence; captured buffering-event race. |
| `pause-rewind-stable-9lbqulut/` | Stabilized-link natural-boundary/recovery passes; paused-seek generation failure. |
| `pause-rewind-m1-venkqu0c/` | Paused-seek fix, five successful headphone/1000 ms diagnostic groups, final manual listening/Pause/menu pass, builds, tests and restoration. |

Do not overwrite these directories. They contain account/track identifiers and raw
logs and must not be committed. Temporary diagnostic programs in these directories
use existing relay protocols; they are not firmware/UI certification. Their fixture
URIs and assumptions must be revalidated before reuse.

## What remains

1. Review the implementation commits and this handoff. The last cold-reconnect priming
   condition has only offline verification; begin hardware follow-up there. Pause,
   reconnect without an automatic Play, then explicitly Play. Confirm capture readiness
   while the source is still paused, a fresh capture header, the verified bookmark,
   advancing output, and bounded failure/retry.
2. With a stable verified link and fresh private backup, repeat headphones/4000 ms
   and USB/1000 and 4000 ms using the current matching builds. Include track changes,
   hardware/phone controls, ten rapid cycles and both final Pause and final Play;
   listen beyond each configured buffer plus several seconds. Recheck headphones/1000
   ms after the final condition as part of the complete matrix.
3. Continue the deferred matrix on those configurations: natural boundaries and
   duplicate occurrence identity, shuffle/repeat, startup/refill cancellation,
   source outage/reconnect/retry, menus, and seek/skip/browse during restoration.
   A real just-before-output threshold race, physical USB unplug, and controlled
   hardware-pointer reset have not been completed. Offline fault injection is separate evidence.
4. Make a synchronized recording if measuring the 100 ms targets. A second USB device
   at another hardware rate is still needed for physical rate diversity.
5. Restore and verify files, absences, modes, volume/output/settings, source options,
   services and any temporary network changes before publishing new results. Preserve
   current settings at the next session; do not blindly force this session's volume 18.

## Commit series

Clock and control changes are independently reviewable:

- `e2084cf` — `fix(relay): preserve reliable hardware playback epochs` — clock lifecycle,
  diagnostics and hardware/presentation regressions; include the diagnostic hunk in `q2pod.c`.
- `ca3b681` — `fix(streaming): serialize pause restoration and capture generations` — bridge
  controls/events, capture ownership, recovery/repeat handling, the source-loss hunk
  in `q2pod.c`, and control/contract regressions.
- `docs(streaming): record M1 pause follow-up and remaining validation` — this report
  and the README/runbook links.

The user authorized this series after verification. The two `q2pod.c` hunks belong
to separate implementation commits. Private evidence and backups are excluded.
