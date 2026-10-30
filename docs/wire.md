# The relay ↔ bridge wire

Version 2: audio, presentation anchors, immediate pause/rewind, state, covers, controls, ordered boundaries and Q2SPOT/1 browsing (PLAN.md). One TLS connection, which the relay on the card opens to the bridge, carries everything. Update bridge and relay together; version 1 peers are rejected.

## Connection

1. The relay connects to an endpoint from `.soloify/remote` (`HOST PORT TOKEN [sha256=HEX] [buffer=MS]`, one a line, tried in order) and does TLS 1.2 or later. With `sha256=`, the relay accepts only a leaf certificate whose DER SHA-256 is `HEX` and checks nothing else (so the device's clock doesn't matter). Without it, the relay checks the chain against `/etc/scrobble-ca.pem` and the name against `HOST`.
2. The relay sends `hello 2 TOKEN\n`. The bridge compares the token in constant time and answers `ok 2\n`, or closes the connection. A second relay that says hello replaces the first, whose link may be dead without either side knowing yet.
3. After that both sides send frames: a type byte, a big-endian u32 payload length (at most 1 MiB), then the payload.

## Frames

| Type | Direction | Payload |
| --- | --- | --- |
| `A` | relay → bridge | `1 MS`: send audio, MS being the relay's buffer target. `0 pause`: stop the encoder, without issuing another pause or marking it as a stall. The explicit `R` bookmark carries the device pause. |
| `k` | relay → bridge | u32: audio packets received since the hello, sent about every 200 ms while audio flows and at least every 5 s otherwise; the bridge drops a relay it hears nothing from for 20 s |
| `t` | relay → bridge | stats as text (`key=value` words), logged by the bridge |
| `c` | relay → bridge | `P` play, `n` next, `b` previous, `S<ms>` seek. Device `p` is resolved locally; Pause uses `R`. Bare `c s` remains a source-only control for diagnostic clients. |
| `g` | bridge → relay | u32 generation, u16 Opus pre-skip, optional warmup Opus packet (at most 1275 bytes), before the first audio packet of the run. A legacy four-byte generation implies pre-skip 312 and no warmup. |
| `e` | bridge → relay | u32 sequence, u16 valid samples (0–960) for the next, final `a` packet. Preserves the encoder's final Ogg granule trim. |
| `B` | bridge → relay | u32 browse correlation ID: read-only preparation succeeded; begin a playback reset. Paired with exactly one `x Q`. |
| `a` | bridge → relay | u32 sequence number, then one Opus packet (20 ms, 48 kHz stereo) |
| `s` | bridge → relay | the state: librespot's status file lines (`key=value`, each ending `\n`), less `at=` |
| `j` | bridge → relay | a track id, `\n`, then that track's cover as a JPEG |
| `x` | bridge → relay | completed `c` control bytes, echoed. For skip/seek the previous encoder has ended before this frame. The current cover and a forced state snapshot follow local controls. Pause bookmarks complete with `h`, not `x s`. |
| `l` | bridge → relay | state lines plus `token=`, immediately before an audio packet's first presented sample. Position comes from its capture timestamp, excluding pre-skip. Token 0 means no reliable source anchor. |
| `v` | bridge → relay | empty: external Pause event, request immediate local bookmark/drop. `P`: external Play event, request the same ordered local Play gate. |
| `R` | relay → bridge | u32 pause correlation ID, then `token=...\nposition=...\n`. Empty lines payload means no reliable bookmark. IDs survive reconnect; repeated pending Pause retains the bookmark. |
| `h` | bridge → relay | u32 pause ID, success byte (0 or 1), optional UTF-8 diagnostic (at most 512 bytes). Success confirms a paused source restore; only the current ID may open the restore gate. |

`q` carries a correlated Q2SPOT/1 request (relay → bridge); `r` carries its response (bridge → relay). See Browsing below. The relay ignores frame types it does not know.

## State and covers

The bridge follows Soloist through its WebSocket and sends `s` whenever what Q2 Pod shows changes: the state, the track, its duration, a seek or a pause (not the position's steady advance). Each burst of Soloist's events settles for 100 ms first, so a play that names the new track a moment after saying `playing` gives one state, not two. A newly connected relay gets the current cover, then the current state.

- `state=` is `none` (Soloist isn't running, isn't logged in, or another device is active), `stopped`, `paused` or `playing`. Soloist's `buffering` (a seek, a skip, a new context) keeps the state before it, so the relay's sink isn't stopped for a tenth of a second each time. A pause the bridge made for a stall stays `playing`: the device plays on from its buffer, and Q2 Pod would take `paused` as a reason to free the DAC and start its idle stop.
- With a track: `track=` (the Spotify id), `title=`, `artist=` (joined by `, `), `album=`, `duration=` and `position=` in ms, each value one line of at most 255 bytes. `position=` is Soloist's when the bridge sends the frame. The relay queues the snapshot at the corresponding received-audio packet clock and stamps `at=` with its own `CLOCK_MONOTONIC` when that audio reaches playback. Pending metadata waits through pause/refill, and follows trims. At initial buffering or after a reset, the position is shifted by the target and re-anchored when the sink starts. These arrival-time snapshots remain a display fallback for sources without presentation anchors. Version 2 uses `l` and the hardware cursor for audible bookmarks; `s` can carry a `token` for a known starting point before any packet exists. Internal tokens are stripped from the firmware status file.
- `cover=1` once the relay has this track's JPEG: the bridge sends `j` (Soloist's `default` cover, about 300 px, fetched over HTTPS) before the first state that says so. The id in `j` lets the relay keep a cover with its track if it delays the state.

The bridge keeps Soloist at volume 100, setting it back whenever the phone changes it (librespot's `--volume-ctrl fixed`): the device's volume is its own.

## Local controls and boundaries

The relay resolves `p` using the latest requested state, including rapid toggles during
buffering/restore. Pause snapshots the hardware cursor, kills/reaps the sink group,
discards audio and pending metadata, publishes a frozen paused state and sends `R`.
It never drains a partial buffer or waits for a source fade. If output has not
started, the bookmark is the first pending sample, or a known source starting
anchor before the first packet. Without a reliable anchor the bookmark is invalid;
output still stops immediately.

Hardware discovery uses `/proc/asound/pcm`, the sink process-group owner, substream
`status` (`hw_ptr`) and actual `hw_params` rate. Card/device numbers are not assumed.
Pointer resets, XRUN, changed ownership/rate and unavailable/ambiguous clocks fail
safely. The source timeline comes from capture sample timestamps, not packet arrival
or subtracting the configured target. The relay retains 2048 presentation anchors,
covering the ring and hardware lead. Ring loss or mid-playback trims invalidate the
current mapping until a fresh hardware epoch.

Under the session control lock the bridge pauses Soloist, stops capture without
waiting for the pause fade, restores the heard occurrence through verified queue
history/Previous when necessary, seeks, then queries until the source confirms the
paused track, context and position (within 100 ms). A dispatch ACK is insufficient.
Previous first seeks the current track to its opening to avoid restarting it.
Each visited occurrence is confirmed by its URI and removal from the prior queue
snapshot using UID/source identity. Soloist may pass a manually queued track on
Previous; checked upcoming entries and Next then reach the heard occurrence.
List direction is inferred only when duplicate occurrences identify it uniquely;
ambiguous duplicates fail paused. The bookmarked entry must remain reachable
throughout navigation.
Shuffle/repeat/context are preserved; changed or ambiguous history fails paused.
State confirmation has a three-second deadline per step and restoration has a
12-second overall deadline. A final best-effort pause may use the WebSocket's
three-second timeout. Failed bookmarks remain for the next explicit Play, with
bridge/relay logs and a local `.pause-error` sidecar; automatic probes cannot retry
or resume them. Seek/skip/prepared browse-play supersede the bookmark under the
same control lock. Reconnect retains the bookmark and verified navigation progress.

Play sends `c P`. Any failed/pending restore is completed first, capture is primed
before source Play, actual playing state is confirmed, and only fresh capture is
released after `x P` and the forced state. The relay requires restore success, all
outstanding boundaries, a new `g`, fresh packets and the normal target before
starting its sink. Discarded packets are still ACKed. Rapid Pause/Play/Pause orders
a final source Pause against the same bookmark; stale state cannot start output.
Phone Pause sends `v` immediately on its event, before acquiring the session lock;
phone Play uses `v P` and goes through the same restore/prime gate. Soloist's normal
resume ramp begins fresh playback with no retained-audio join. Headphone and USB
validation is in [pause-rewind.md](pause-rewind.md); M1 measurements are pending.

Skip and seek stop/discard the local sink, ring and pending metadata at once. The bridge serializes the command with encoder shutdown, drains old audio, sends the command to Soloist, settles its state, then sends `x` and its snapshot. Until all outstanding reset boundaries arrive, the relay discards old audio and state while still acknowledging the audio. Both rapid Previous presses are retained, and capture cannot restart between their outstanding boundaries. Skip/seek keeps Soloist paused by the bridge across those boundaries. For the final mutation, the bridge primes the new encoder to its first packet before dispatch, discards that warmup packet, and holds output until the final boundary. Soloist can auto-play on skip even when paused; capture already records the intro. Stall recovery likewise primes capture before issuing Play. Priming has a three-second readiness deadline. Pulse capture preserves its first sample timestamp, then uses a steady sample clock. Immediately before dispatch, the bridge marks the presentation start and derives pre-skip from that timestamp, excluding samples captured while paused. For long priming it omits earlier packets while keeping at least 80 ms of decoder history. A warmup packet can travel in `g`; the remaining fully skipped packets travel as `a` with zero presented duration. They must still reach the decoder. Sources without capture timestamps retain the one-packet warmup fallback (pre-skip 1272). A readiness failure leaves recovery paused for a later probe. Multiple pause/play commands likewise keep the latest local choice until their echoed boundaries have drained; stale snapshots cannot undo rapid toggles. A control's state is deferred until its boundary, so the relay can distinguish the new snapshot from old state already in flight.

External pause uses the same immediate-drop bookmark exchange as above. Deactivation and stop still stop/reap the sink immediately before publishing their nonplaying state, and send `A 0 pause` to end capture. A pause the bridge made for a stall remains `playing` and follows the existing stall recovery rule. Explicit pause is tracked separately from recovery ownership: opening the browser sends no Play, and automatic recovery resumes only a bridge-owned pause. Capture shutdown and stall work are bound to an encoder generation; late work cannot stop a replacement encoder.

## Audio

An `A 1` while capture is stopped starts a new encoder on the bridge, and each stop ends it, so nothing queues on the server while the relay isn't taking audio. The packets carry no Ogg framing. Each encoder run becomes an independent Ogg Opus stream on the relay. The queue retains its generation, warmup and exact final-sample count. When playback reaches the next generation, only the decoder drains/restarts; the same PCM player keeps ALSA open. This prevents a predictive decoder from interpreting a fresh encoder as continuation of the old one. Encoder padding and warmup are decoded then trimmed. Restarting a decoder partway through a queued generation supplies up to 80 ms of history as pre-roll, excluded from playback. Explicit pause discards that generation instead of retaining it. The stock ffmpeg line preserves its probe packets (no `-fflags nobuffer`, which discarded initial audio in exact-length tests).

Flow control is in the application, not left to TCP. The bridge records each packet's
send time and counts sent minus acknowledged packets. At the in-flight limit it
yields to the independent ACK reader, allowing batches of encoded packets to flow
without declaring a failed link. It waits only until the oldest unacknowledged
packet's send time plus the buffer target. Without sufficient ACK progress by that
deadline, it schedules one generation-bound stall shutdown. The limit is the relay's
buffer target from `A 1` (at least 300 ms), else `serve --inflight` (2 s). Shutdown
sends at most 300 ms of encoder tail beyond that limit; encoder termination and
drainage have a four-second deadline. Recovery work cannot accumulate per packet.
Diagnostics report generations, bursts (rate limited), ACK age, in-flight time,
reset boundaries and sink transitions. Search logs contain category and term byte
length, never query text. Remaining device stalls require trace evidence before
attributing them to Wi-Fi, capture or relay scheduling.

The relay holds the only buffer. It starts its sink once it holds its target (`buffer=`, or `-b`). It then feeds the decoder in real time, at most 300 ms ahead, rather than as fast as the pipe takes it: a pipe bounds bytes, and silence's packets are small enough that seconds of them would wait there uncounted. On an underrun it rebuffers to the target while audio keeps coming, without stopping Soloist, so nothing is missed. The relay never declares a stall itself: a 1 s hiccup on a 1 s LAN target would stop Soloist, when the bridge's in-flight limit rides it out. While it wants audio it repeats `A 1` every second, which the bridge ignores while it streams. After a stall the bridge restarts on the first `A 1` that comes once the acks have brought in-flight back under half the limit. So audio resumes only when the link carries data both ways, and the audio from before the stall, still in flight, plays on without a gap. With no bytes for 6 s while it wants audio, the relay takes the link for dead and reconnects; TCP keepalive (5 s idle, three 2 s probes) catches a dead link otherwise.

## Browsing (step 4)

`q` (relay → bridge) and `r` (bridge → relay) are active. Each payload starts with a
four-byte, big-endian correlation ID, followed by the unchanged Q2SPOT/1 text.
The ID belongs to the relay transaction, independently of the firmware's decimal
request ID inside the text. IDs increase across reconnects; a reply is accepted
only for a currently waiting slot. Update bridge and relay together.

The relay has four client slots, accepts at most 8 KiB of request text and returns
at most 64 KiB of response text. It forwards after the client shuts down its write
side. Local malformed/configuration/offline errors remain local. A transaction
has an 11-second deadline from accept, with up to 500 ms to deliver its timeout
reply (inside the firmware's 15-second deadline). Disconnect fails waiting clients;
late replies are discarded. Requests are never retried or replayed automatically.

The bridge permits four browse handlers per relay and gives each 10 seconds.
Handlers run separately from the frame reader, so Web API waits do not delay audio
acks or stats. Playback changes take the same lock as ordinary controls. The relay
forwards `play` and `skip` without changing audio. Under that lock the bridge first
prepares the action read-only: arguments, exact Connect device availability,
retained Liked Songs selection, and initial queue identity are checked while capture
continues. Rejection, timeout or cancellation before dispatch sends no reset frame.

After preparation the bridge sends `B` with the correlation ID, drains the encoder
and dispatches the prepared mutation. `B` clears the relay's sink/ring/metadata and
holds output until `x Q`, cover and forced state, followed by `r`. Once begun, every
reset completes even on mutation failure, timeout or cancellation. A reset outlives
its browse client slot if needed; the relay must process its completion even after
that client times out. Queue URI/UID checks remain before every skip, including
after capture drainage; a queue change after earlier skips is partial execution and
still completes the reset. Disconnect clears reset accounting, cancels handlers
and pending controls, and fails clients. Requests are never replayed.

Q2SPOT/1 text follows q2-pod's [browser contract](../../q2-pod/docs/librespot.md):
percent-escaped fields, six fields per row, at most 50 rows, 255-byte display
fields, 120-byte messages, and opaque cursors up to 1 KiB. This bridge binds cursors
to operation and arguments and reconstructs API paths locally; it never follows a
client-supplied URL. Artist pages permit an optional continuation cursor, although
older firmware sends only the artist URI. Search and artist-album pages use the
upstream maximum of 10. Artist pages intentionally show albums because development
mode removes top tracks. Missing profile fields remain blank.

`caps` advertises account/library/details/search/play/queue/settings operations.
Sign-out and downloads are unsupported. Without OAuth, account/library requests
say to run `soloify login --client-id ID` on the server; existing Connect playback
continues. Queue/settings use the local Soloist WebSocket, with one pending
command/query, a three-second deadline, and a new connection after a timeout to
exclude late responses. Queue skip re-fetches and checks URI and entry UID before
each skip; external changes can still race after the final check.
