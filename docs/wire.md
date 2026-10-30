# The relay ↔ bridge wire

Version 1, through step 4: audio, state, covers, controls, ordered boundaries and Q2SPOT/1 browsing (PLAN.md). One TLS connection, which the relay on the card opens to the bridge, carries everything. Update bridge and relay together for the control boundaries and browsing frames.

## Connection

1. The relay connects to an endpoint from `.soloify/remote` (`HOST PORT TOKEN [sha256=HEX] [buffer=MS]`, one a line, tried in order) and does TLS 1.2 or later. With `sha256=`, the relay accepts only a leaf certificate whose DER SHA-256 is `HEX` and checks nothing else (so the device's clock doesn't matter). Without it, the relay checks the chain against `/etc/scrobble-ca.pem` and the name against `HOST`.
2. The relay sends `hello 1 TOKEN\n`. The bridge compares the token in constant time and answers `ok 1\n`, or closes the connection. A second relay that says hello replaces the first, whose link may be dead without either side knowing yet.
3. After that both sides send frames: a type byte, a big-endian u32 payload length (at most 1 MiB), then the payload.

## Frames

| Type | Direction | Payload |
| --- | --- | --- |
| `A` | relay → bridge | `1 MS`: send audio, MS being the relay's buffer target. `0 pause`: stop the encoder, without issuing another pause or marking it as a stall. The explicit `c s` carries the user's pause. |
| `k` | relay → bridge | u32: audio packets received since the hello, sent about every 200 ms while audio flows and at least every 5 s otherwise; the bridge drops a relay it hears nothing from for 20 s |
| `t` | relay → bridge | stats as text (`key=value` words), logged by the bridge |
| `c` | relay → bridge | a control datagram as Q2 Pod sent it: `p` play/pause, `P` play, `s` pause, `n` next, `b` previous, `S<ms>` seek |
| `g` | bridge → relay | u32 generation, u16 Opus pre-skip, optional warmup Opus packet (at most 1275 bytes), before the first audio packet of the run. A legacy four-byte generation implies pre-skip 312 and no warmup. |
| `e` | bridge → relay | u32 sequence, u16 valid samples (0–960) for the next, final `a` packet. Preserves the encoder's final Ogg granule trim. |
| `B` | bridge → relay | u32 browse correlation ID: read-only preparation succeeded; begin a playback reset. Paired with exactly one `x Q`. |
| `a` | bridge → relay | u32 sequence number, then one Opus packet (20 ms, 48 kHz stereo) |
| `s` | bridge → relay | the state: librespot's status file lines (`key=value`, each ending `\n`), less `at=` |
| `j` | bridge → relay | a track id, `\n`, then that track's cover as a JPEG |
| `x` | bridge → relay | the completed control's bytes, echoed. `x s` also completes an external pause after capture drainage. For pause/skip/seek, the previous encoder has stopped and drained before this frame. The current cover and a forced state snapshot follow local controls. |

`q` carries a correlated Q2SPOT/1 request (relay → bridge); `r` carries its response (bridge → relay). See Browsing below. The relay ignores frame types it does not know.

## State and covers

The bridge follows Soloist through its WebSocket and sends `s` whenever what Q2 Pod shows changes: the state, the track, its duration, a seek or a pause (not the position's steady advance). Each burst of Soloist's events settles for 100 ms first, so a play that names the new track a moment after saying `playing` gives one state, not two. A newly connected relay gets the current cover, then the current state.

- `state=` is `none` (Soloist isn't running, isn't logged in, or another device is active), `stopped`, `paused` or `playing`. Soloist's `buffering` (a seek, a skip, a new context) keeps the state before it, so the relay's sink isn't stopped for a tenth of a second each time. A pause the bridge made for a stall stays `playing`: the device plays on from its buffer, and Q2 Pod would take `paused` as a reason to free the DAC and start its idle stop.
- With a track: `track=` (the Spotify id), `title=`, `artist=` (joined by `, `), `album=`, `duration=` and `position=` in ms, each value one line of at most 255 bytes. `position=` is Soloist's when the bridge sends the frame. The relay queues the snapshot at the corresponding received-audio packet clock and stamps `at=` with its own `CLOCK_MONOTONIC` when that audio reaches playback. Pending metadata waits through pause/refill, and follows trims. At initial buffering or after a reset, the position is shifted by the target and re-anchored when the sink starts. This does not measure the decoder/ALSA latency (step 1 observed up to about 0.5 s beyond the target).
- `cover=1` once the relay has this track's JPEG: the bridge sends `j` (Soloist's `default` cover, about 300 px, fetched over HTTPS) before the first state that says so. The id in `j` lets the relay keep a cover with its track if it delays the state.

The bridge keeps Soloist at volume 100, setting it back whenever the phone changes it (librespot's `--volume-ctrl fixed`): the device's volume is its own.

## Local controls and boundaries

The relay resolves `p` using its latest requested state, forwarding explicit `s` or `P`. Pause sends `c s` and stops periodic audio requests, while the current sink keeps playing its queue. The bridge pauses Soloist first, allows 300 ms for its fade, then stops/drains capture. Local and external pause both send `x s` only after the final captured packet; a locally triggered pause does not also emit a duplicate external boundary. This path never sets `paused_by_us`.

The relay drains all queued packets, including a partial startup buffer and the captured fade, then closes the PCM pipe and lets the player finish its hardware tail. Only after the sink is reaped does it publish `paused` and send `A 0 pause`. Queue drainage has a deadline of the starting ring level plus six seconds; after PCM EOF the player has two seconds. A missing boundary or stuck player therefore cannot hold ALSA indefinitely. Pause deliberately takes about the configured buffer plus the source/output tail (1-second LAN / 4-second remote target), as chosen by the user. It retains no audio for resume.

Play forwards `c P` and starts a fresh buffer. The sink waits for all local boundaries, a new `g`, fresh packets and the configured target. Old encoder tails are discarded while waiting for that fresh capture, though they are still acknowledged. A Play that interrupts drainage stops/discards the remaining old tail and starts fresh as well. External (phone) pause/resume follows the same policy. Local Play primes capture before dispatch; Connect resume starts capture from its playback event instead of waiting for a periodic probe. This places Soloist's resume fade at the start of playback, without a delayed join to retained audio.

Skip and seek stop/discard the local sink, ring and pending metadata at once. The bridge serializes the command with encoder shutdown, drains old audio, sends the command to Soloist, settles its state, then sends `x` and its snapshot. Until all outstanding reset boundaries arrive, the relay discards old audio and state while still acknowledging the audio. Both rapid Previous presses are retained, and capture cannot restart between their outstanding boundaries. Skip/seek keeps Soloist paused by the bridge across those boundaries. For the final mutation, the bridge primes the new encoder to its first packet before dispatch, discards that warmup packet, and holds output until the final boundary. Soloist can auto-play on skip even when paused; capture already records the intro. Stall recovery likewise primes capture before issuing Play. Priming has a three-second readiness deadline. Pulse capture preserves its first sample timestamp, then uses a steady sample clock. Immediately before dispatch, the bridge marks the presentation start and derives pre-skip from that timestamp, excluding samples captured while paused. For long priming it omits earlier packets while keeping at least 80 ms of decoder history. A warmup packet can travel in `g`; the remaining fully skipped packets travel as `a` with zero presented duration. They must still reach the decoder. Sources without capture timestamps retain the one-packet warmup fallback (pre-skip 1272). A readiness failure leaves recovery paused for a later probe. Multiple pause/play commands likewise keep the latest local choice until their echoed boundaries have drained; stale snapshots cannot undo rapid toggles. A control's state is deferred until its boundary, so the relay can distinguish the new snapshot from old state already in flight.

External pause drains as above. Deactivation and stop still stop/reap the sink immediately before publishing their nonplaying state, and send `A 0 pause` to end capture. A pause the bridge made for a stall remains `playing` and follows the existing stall recovery rule. Explicit pause is tracked separately from recovery ownership: opening the browser sends no Play, and automatic recovery resumes only a bridge-owned pause. Capture shutdown and stall work are bound to an encoder generation; late work cannot stop a replacement encoder.

## Audio

An `A 1` while capture is stopped starts a new encoder on the bridge, and each stop ends it, so nothing queues on the server while the relay isn't taking audio. The packets carry no Ogg framing. Each encoder run becomes an independent Ogg Opus stream on the relay. The queue retains its generation, warmup and exact final-sample count. When playback reaches the next generation, only the decoder drains/restarts; the same PCM player keeps ALSA open. This prevents a predictive decoder from interpreting a fresh encoder as continuation of the old one. Encoder padding and warmup are decoded then trimmed. Restarting a decoder partway through a queued generation supplies up to 80 ms of history as pre-roll, excluded from playback. Explicit pause drains that generation instead of retaining it. The stock ffmpeg line preserves its probe packets (no `-fflags nobuffer`, which discarded initial audio in exact-length tests).

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
