# Step 4 follow-up: reliability results

**Policy update (2026-10-30):** the drain-on-pause implementation/results below are historical. [Immediate pause and rewind](pause-rewind.md) supersedes that policy; its M1 validation is pending.

## Completed 2026-10-30

The user confirmed clean hardware and Spotify Connect resumes at both the
**1-second LAN target** and the **4-second target on LAN**. Double Previous
preserves the opening, rejected playback actions leave the sink uninterrupted,
and reopening Spotify preserves explicit pause. The final observation completed
**1,800.58 seconds of actual rendered playback/browsing** over **1,859.49 seconds
wall time**, and the user reported **no gaps**. Its 186 statistics samples show
zero underruns, trims, ring drops, reconnects or stall shutdowns; device decoder/
player logs contain no faults. Buffer level remained steady through ordinary
browsing/control changes.

Original device files are restored and verified. ALSA and all temporary device/
host audio services are stopped. No firmware was flashed. The agent changed no
device configuration or volume; volume started at 20 and was left at 18 under
the user's instruction not to restore it. The user handles firewall rules; the
bridge no longer listens on TCP 7443.

Latest relay SHA-256:

```text
a53c0f275d7a2d502f63662eb41ffc7a620df15d7035aa646cc970061d0acf79
```

## Confirmed cause and chosen pause behavior

A continuous raw-PCM capture compared the same musical passage with and without
a pause. Soloist fades up from zero to unity over roughly **250 ms** after Play;
after the ramp, samples match the uninterrupted reference (correlation 1.0).
Pause also tapers before silence. Retaining old full-level audio on instant pause
exposed that source transition later, at the old/new capture join.

The user explicitly chose **drain on pause** on 2026-10-30, superseding the earlier
instant-pause/retained-buffer requirement. Both hardware and Connect pause now
pause Soloist first, capture its fade, drain the ring and PCM/hardware tail, reap
the sink, and only then publish paused. Resume starts fresh. Play interrupting a
drain discards its old tail and waits for the new capture generation. Stop and
deactivation still stop immediately. Observed LAN pause delay was about
**1.7–1.8 seconds**, including source/output tails; the 4-second setting takes its
longer buffer plus those tails. Drainage and stuck-player cleanup are bounded.

Earlier candidates also exposed independent codec/capture faults: about 90 ms of
paused capture prefix where only 20 ms was trimmed; fully skipped warmup packets
being discarded before decoding; independent Opus encoders sharing predictive
decoder state; ignored final granule padding; and ffmpeg probe packets dropped by
`-fflags nobuffer`. These corrections remain, alongside the new pause policy.
Timestamp-based trimming skips known pre-dispatch samples without detecting or
trimming quiet musical content. Long priming retains at least 80 ms of decoder
history, including zero-duration warmup packets.

## Verification and device evidence

- Full Python suite: **150 passed, 1 opt-in live test skipped**, **135.52 s**.
  Includes stock MIPS/QEMU decoding, exact PCM joins/tails, partial startup drains,
  missing completion, rapid Play during drainage, exactly-once external/local
  pause boundaries, browse cancellation/failures, generation races, ACK bursts,
  and real 3-/15-second outages. Ruff lint/format and whitespace checks pass.
- Firmware sources were not changed in this resumed session. Their preceding
  gates remain applicable: 1,355 MIPS scenarios; M1 package/manifest and host PEQ;
  all 615 ports classified; Q2 Legacy/iPod byte identity against upstream 1.5.0.
- Current q2-soloify base is `a7436b3` (the user's envrc ignore commit). M1 search
  is already committed separately in q2-pod as `2f8c003`; its tree is clean.
  Root playback/test/doc changes form one coordinated reliability change; bridge
  and relay must be deployed together.

| Check | Result |
| --- | --- |
| M1 button pause/resume, 1 s | User confirmed clean, at least three repeats |
| Spotify Connect pause/resume, 1 s | User confirmed three clean repeats |
| Double Previous | User confirmed both openings intact, twice |
| Invalid play / stale queue | `bad` / `unavailable`; same ALSA owner and advancing hardware pointer |
| Pause, leave, reopen without Play | User confirmed stayed paused; ALSA closed |
| M1 and Connect pause/resume, 4 s on LAN | User confirmed both clean; left paused |
| 30 minutes of actual playback/browsing, 1 s LAN | Passed: 1,800.58 rendered seconds; user reported no gaps; zero fault counters |

M1 address: `192.168.1.210`; bridge: `192.168.1.200:7443`. ALSA ownership and
advancing `hw_ptr` were verified before listening conclusions. The old hotspot's
1-second failures are not proof of a universal cause; this pass validates a stable
LAN independently. Defaults remain **1 second LAN / 4 seconds remote / 160 kbit/s**.

The `ab03ae68…` and `60478853…` candidates failed listening before the source fade
was isolated; each was restored before further implementation. The current
candidate has the user-approved drain policy. A deliberate relay restart for the
4-second setting required starting the temporary service while the user had
Spotify open; its playback clock was then verified. Soloist's known first-login
restoration failure occurred once, then cleared on a single restart without
changing credentials.

## Evidence, restoration and next step

Private evidence is under `build/step4-final-wz9TwqSw/` (mode 0700):
`source-comparison/analysis.json` contains measured gain windows;
`take3-drain/` contains the successful control traces and bridge log;
`take4-remote/` contains the 4-second comparison; `take5-lan30/ordinary.jsonl`
counts advancing ALSA samples at 44.1 kHz and excludes paused/stopped intervals.
Each run has its own file; earlier aborted observations are not combined.
Bounded encoder packets and raw PCM are private audio, not publication data.

Fresh backup retained: `/mnt/mmc/.soloify-step4-final-wz9TwqSw`, with five
originals, `original.sha256`, `original.modes`, and `absent`. Final cleanup copied
back `librespot`, `run`, `aplay.sh`, `spot.log` and `aplay.log` with `cp -p`:
all five SHA-256 checks pass, and numeric mode/owner/size fields match each backup.
Originally absent `remote`/`q2-soloify` remain absent; `/tmp/soloify-qctl` and all
five local generated `remote` files were removed. ALSA is closed, no device
librespot/ffmpeg/aplay remains, the native bridge/Soloist/encoder and collectors
are stopped, and no TCP 7443 listener remains. Volume is left at 18. The older
backup, OAuth/Soloist credentials, API key, certificate and unrelated user files
are preserved. Original-backend playback was not relaunched after the requested
pause; its earlier account audio-key refusal is recorded in step 4.

Final evidence: `take5-lan30/result.json`, `relay-observation.log` and the empty
`aplay-observation.log`; root `restoration-final.log` and `final-state.txt`;
`functional-results.json`; and the copied software gate logs. The observation ran
from `2026-10-30 04:49:10 UTC` to `05:20:09 UTC`, including initial waiting/refills;
only advancing ALSA samples count toward its 30-minute result. Earlier failed or
aborted observations do not count toward this pass.

Deploy bridge and relay together: `B`, extended `g`, `e`, and external-pause `x s`
semantics are documented in [wire.md](wire.md); Q2SPOT/1 text is unchanged. The
search fix is prepared for the step 5 firmware build; installed dev 13 still needs
that build for firmware UI search. Do not flash it as part of this follow-up.
Enqueue feedback, cramped messages, typography and volume investigation stay in
step 5; editorial-playlist availability remains an upstream limitation.

The earlier handoff below is historical. Its instant-pause behavior and next-session
checklist are superseded by the current policy and results above.

## Earlier handoff

Paused at the user's request on 2026-10-29. **The latest codec-boundary correction
passes software checks but has not been deployed or listened to on the M1.** A
clean resume listening pass and a complete 30-minute playback/browsing pass remain
outstanding. Do not mark step 4 reliability complete or proceed as though those
checks passed.

The temporary device installation has been restored. Playback is stopped; the
bridge and Soloist are stopped. No firmware was flashed. The user asked to leave
volume restoration alone and will handle the firewall.

## Working trees and artifacts

Changes are uncommitted in two existing checkouts:

- `/home/jtliang/Projects/q2-soloify`, branch `main`, base
  `ee3e3137679cf542b1fd04b1ac525b68d3e1c969`.
- `/home/jtliang/Projects/q2-pod`, branch `staging`, base
  `b504ee0d006dab3dc3dc30bb51dbbf08d333dbf0`.

Preserve the current working trees. The untracked `.envrc` and `.direnv/` in
q2-soloify appeared during this session and were not edited by this work. New
files belonging to this work include this report, `tests/test_reliability.py` and
`tests/test_codec_join.py`.

Latest MIPS relay: `build/relay/q2-soloify`, SHA-256:

```text
ab03ae68e199f34639cc4743b8c9a055259a10ccd7d3d7fafb83cb9762c7c5b8
```

Deploy the current bridge and this relay **together**. The internal wire now uses
`B`, extended `g`, and `e`; Q2SPOT/1 request/response text is unchanged. See
[wire.md](wire.md). Earlier test binaries do not contain the final codec fix.

## Implemented changes

**Search firmware seam.** In q2-pod's `patch/spotify.c`, the category level owns
the accepted query in its title. Inactive-editor and repeated keyboard-close
callbacks cannot overwrite it. All changes are behind `M1PLUS`. The regression in
`test/m1plus/patch.py` models callbacks nested in widget destruction, plus all
categories, Unicode/escaping, Back and continuation. Against the original
firmware it reproduces `('search', 'tracks', '')`; the fixed build passes. This is
prepared for the step 5 firmware build. Installed dev 13 still needs that build
for firmware UI search; no flash was authorized for this follow-up.

**Preparation before playback reset.** `browse.py` prepares playback under the
ordinary-control lock while audio continues. It checks arguments, the exact
Connect device, retained Liked Songs selection and initial queue identity. Only
successful preparation sends correlated `B` reset-begin. The relay no longer
clears audio merely for forwarding `play`/`skip`. Once begun, every reset completes
with `x Q`, including mutation failure, timeout/cancellation and partially executed
queue skips. Queue identity remains checked before every skip. Pre-dispatch
rejection/cancellation sends no reset; requests are not replayed.

**Capture and controls.** Encoder generations bind delayed stops and stall work;
only one stall shutdown is scheduled per run. External playback transitions share
the control/capture lock. Disconnect disarms external capture callbacks before
awaiting cancelled controls. Both rapid Previous presses are retained. Capture
primes before the final skip/seek mutation and holds output until its boundary:
Soloist can auto-play a changed track even when previously paused. Local capture
stops before Pause, local Play primes capture first, and Connect resume starts
capture from its event instead of waiting for the periodic relay probe. Explicit
pause is separate from bridge-owned recovery pause. Browser opening sends no Play.

**Buffer and burst handling.** Pause reaps the sink and releases ALSA immediately,
retaining unplayed audio. Resume waits for ordered boundaries, a fresh capture
marker/packets and the target ring level. The retained queue is exempt from the
initial backlog trim on resume. At the in-flight limit, the pump yields to ACKs
until the oldest packet's target-duration deadline. Shutdown tails and recovery
work are bounded. Diagnostics include generation, burst size, ACK/oldest-packet
age, reset/decoder boundaries and sink transitions. Search logs include category
and term byte length, not private query text.

**Latest codec-boundary correction — software-verified only.** Independent Opus
encoders were being treated as one continuous decoder stream. A steady-tone
reproduction showed a large short transient and extra padded samples at the join.
The bridge now preserves the final Ogg granule trim (`e`) and sends decoder
warmup/pre-skip in `g`. The relay queues that information with the corresponding
audio. At a generation boundary it drains/restarts only the decoder, retaining the
same PCM player and ALSA ownership. Resuming within a retained generation supplies
up to 80 ms of decoder history as pre-roll. The exact PCM test checks two seconds
of output, stable join energy and unchanged player PID, with and without warmup.
Both host and stock MIPS ffmpeg pass. Removing `-fflags nobuffer` also prevents
ffmpeg's initial probe from discarding audio packets.

The codec handling follows [RFC 7845 pre-skip/end trimming](https://www.rfc-editor.org/rfc/rfc7845.html#section-4.2).
[Section 7.2](https://www.rfc-editor.org/rfc/rfc7845.html#section-7.2) describes the
discontinuity from independent encodings. Ogg chaining alone did not remove the
measured transient; a fresh decoder plus correct trimming did.

Defaults remain **1 second LAN, 4 seconds remote, 160 kbit/s**.

## Verification completed

- Final full Python suite: **139 passed, 1 opt-in live test skipped**, **114.29 s**.
  Includes host relay tests, four MIPS/QEMU checks, real 3- and 15-second outages,
  codec joins, final encoder granule trimming, cancellation/drainage, preflight
  rejection, queue races/partial execution, stale generations, bursts and toggles.
- Ruff lint/format and both repositories' `git diff --check` pass.
- One preceding full run hit the existing launcher shutdown/socket timing
  assertion: 138 passed, 1 failed, 1 skipped. The launcher suite rerun passed all
  three checks; the subsequent full run passed 139. Launcher production code was
  not changed. This timing issue is also recorded in the original step 4 report.
- M1 package/manifest checks pass. Callback harness: **1,355 MIPS scenarios
  passed**, zero known gaps and 21 existing checks not ported.
- Firmware host contract suite passes with `TMPDIR=/tmp`; the first run's long
  Nix temporary path triggered an unrelated Tidal format-truncation warning.
- M1 port report classifies all **615** Q2 hooks, call sites and names.
- Q2 Legacy and iPod outputs are byte-identical to upstream **1.5.0**:
  `d9ece51c3547…` and `5936f7554a5e…`. Firmware source did not change after these
  gates. `graphify update .` completed in q2-pod.

Relevant final logs are `/tmp/soloify-clean-final-full.log`,
`/tmp/soloify-codec-mips2.log`, `/tmp/soloify-complete-relay.log`,
`/tmp/soloify-firmware-fixed.log`, `/tmp/soloify-q2compare.log` and
`/tmp/soloify-firmware-peq2.log`. `/tmp` artifacts may disappear; the counts and
checksums above are the durable summary.

## What the device established

M1 firmware: **1.5.0 dev 13**. Last addresses: laptop hotspot `192.168.12.1`, M1
`192.168.12.130`. These are historical, not guaranteed next-session addresses.
TCP 7443 reachability was verified. The M1 has neither `nc` nor shell `timeout`;
use telnet with q2remote's host-side deadline. The [runbook](step4-runbook.md) now
uses `python -m soloify.cli` and records first-login/restart troubleshooting.

- Rejected invalid play and stale queue returned `bad`/`unavailable` with identical
  decoder/player PIDs before and after; audio stayed playing and no reset began.
- Direct relay/bridge search in all four categories, continuation and Unicode/
  percent escaping succeeded. This is not a firmware UI search pass.
- Pause released ALSA. Closing Now Playing, leaving the browser and reopening
  Spotify preserved `paused` with ALSA closed; explicit Play was required.
- Previous initially lost the song's opening. The trace showed Soloist auto-played
  before capture was ready (one attempt reached 566 ms). Priming before the final
  command produced capture-ready at paused position 0. The user confirmed on a
  repeat: **the opening was intact**.
- At a **1-second target on this hotspot**, repeated playback stalls persisted.
  TCP traces show retransmissions and packets outstanding longer than a second.
  This supports a transport contribution but does not isolate every cause or
  constitute a stable-LAN pass. Do not change the LAN default to conceal this.
- At the existing **4-second remote target**, ordinary streaming was substantially
  more stable. The user still heard a short resume blip on both the device button
  and Connect, a few seconds after resume. Capture-order/retained-buffer changes
  improved it, but the last listening verdict was **a smaller blip remains**.
- The final decoder/warmup/trimming correction above was developed afterward.
  **It has not had an M1 listening check.**

One shell-launched leg was invalid: Spotify had not been reactivated through the
firmware UI, hardware Play/Pause went to the local player, and ALSA stopped
advancing while the queue grew. Exclude its trims/drops from product conclusions.
Reopening Spotify through the UI and resetting the test queue restored normal
ALSA progress (44,320 samples in one second). Always verify ownership before tests.

**No complete, clean 30-minute pass was obtained.** Observation files are partial
and were stopped when the audible blip was reported. Do not treat them as passes.

## Restoration at this handoff

- Original `librespot`, `run`, `aplay.sh`, `spot.log` and `aplay.log` restored with
  `cp -p`; all five original SHA-256 checks pass. Numeric `ls -ln` mode, ownership and
  size fields also match each backup (the M1 has no `stat`).
- `remote` and `q2-soloify` were absent originally and are absent again.
- Temporary `/tmp/soloify-qctl` and local generated `build/step4-followup/remote`
  removed. Regenerate the latter before using the reinstall helper.
- Backup retained on card: `/mnt/mmc/.soloify-step4-followup-20261029`, including
  `original.sha256` and `absent`. Do not overwrite it.
- ALSA is closed; no relay, device ffmpeg or aplay remains. Native bridge listener,
  Soloist and capture ffmpeg are stopped. Playback was not restarted.
- `OUTPUTWAY=0`, `STANDBYMODE=0`, `LIGHTTIME=0` match starting values. Volume was
  20 initially and now reads 11; the user explicitly said not to restore volume.
- The user will handle the temporary TCP 7443 firewall opening. Noninteractive
  sudo could not close it because a password was required. No listener remains.
- Existing Spotify/Soloist/OAuth credentials, cache and unrelated card files remain.
  Original-backend launch was not repeated after the user requested this pause.

Private evidence remains under `build/step4-followup/` (mode 0700). Logs/screenshots
contain private identifiers; do not publish raw files. The final installed relay
before restoration was the earlier `aff237d0…` build, not the final `ab03ae68…` build.
`relay-final-before-restoration.log` preserves that trace.

## Next session

1. Read this report and [wire.md](wire.md), inspect both dirty trees, and preserve
   unrelated `.envrc`/`.direnv/`. No commits were made. Re-run appropriate software
   checks if source or environment changed.
2. Confirm connectivity and the current M1 IP. Ask the user to perform simple
   device navigation; they explicitly prefer that to remote UI automation.
3. Regenerate a private pinned `remote` from the preserved `data` directory. For
   the hotspot comparison, explicitly use `buffer=4000`; keep defaults unchanged.
   The old generated `remote` was deleted. Do not print its token.
4. Deploy the **current bridge and final relay together**, without flashing
   firmware. Reuse/verify the intact backup or create a fresh uniquely named one;
   never overwrite the original backup. Helpers in `build/step4-followup/` have
   hardcoded old addresses and paths—review/update them before reuse.
5. Ask the user to open **Q2 Pod → Spotify → Now Playing** and explicitly Play.
   Verify ALSA `hw_ptr` advances before relying on any listening check. A shell
   launch alone does not establish the firmware's Spotify/DAC ownership.
6. Repeat hardware and Connect pause/resume; listen past the full buffer duration.
   Verify no artifact where retained audio meets the fresh encoder. Repeat double
   Previous and preserve the confirmed intro behavior. Recheck rejection without
   sink interruption and paused state on reopening.
7. Only after clean listening, run **30 minutes of actual rendered playback and
   browsing**. The diagnostic `monitor.py` counts advancing ALSA samples at
   44.1 kHz and excludes paused/non-advancing intervals. Rotate existing
   `ordinary-playback.jsonl` and `tcp-timing.jsonl` before reuse; they are partial.
   Collect generation/decoder/ACK traces and user observations. A stable network
   is still needed to validate the 1-second LAN target independently of hotspot
   delays. Restore the device and stop test services afterward.
8. Do not flash the search fix in this follow-up; prepare it for step 5. Enqueue
   feedback, cramped messages, typography and volume investigation remain deferred.
   Editorial-playlist availability remains an upstream limitation.
