# The relay ↔ bridge wire

Version 1, through step 2: audio, state, covers and controls (PLAN.md; q2-pod's `docs/soloist.md`, "Relay ↔ bridge wire"); Q2SPOT/1 comes with step 4. One TLS connection, which the relay on the card opens to the bridge, carries everything.

## Connection

1. The relay connects to an endpoint from `.soloify/remote` (`HOST PORT TOKEN [sha256=HEX] [buffer=MS]`, one a line, tried in order) and does TLS 1.2 or later. With `sha256=`, the relay accepts only a leaf certificate whose DER SHA-256 is `HEX` and checks nothing else (so the device's clock doesn't matter). Without it, the relay checks the chain against `/etc/scrobble-ca.pem` and the name against `HOST`.
2. The relay sends `hello 1 TOKEN\n`. The bridge compares the token in constant time and answers `ok 1\n`, or closes the connection. A second relay that says hello replaces the first, whose link may be dead without either side knowing yet.
3. After that both sides send frames: a type byte, a big-endian u32 payload length (at most 1 MiB), then the payload.

## Frames

| Type | Direction | Payload |
| --- | --- | --- |
| `A` | relay → bridge | `1 MS`: send audio, MS being the relay's buffer target. `0 pause`: stop (step 3's local pause). |
| `k` | relay → bridge | u32: audio packets received since the hello, sent about every 200 ms while audio flows and at least every 5 s otherwise; the bridge drops a relay it hears nothing from for 20 s |
| `t` | relay → bridge | stats as text (`key=value` words), logged by the bridge |
| `c` | relay → bridge | a control datagram as Q2 Pod sent it: `p` play/pause, `P` play, `s` pause, `n` next, `b` previous, `S<ms>` seek |
| `a` | bridge → relay | u32 sequence number, then one Opus packet (20 ms, 48 kHz stereo) |
| `s` | bridge → relay | the state: librespot's status file lines (`key=value`, each ending `\n`), less `at=` |
| `j` | bridge → relay | a track id, `\n`, then that track's cover as a JPEG |

Reserved for step 4: `q` Q2SPOT/1 request (relay → bridge), `r` Q2SPOT/1 response (bridge → relay). The relay ignores frame types it doesn't know.

## State and covers

The bridge follows Soloist through its WebSocket and sends `s` whenever what Q2 Pod shows changes: the state, the track, its duration, a seek or a pause (not the position's steady advance). Each burst of Soloist's events settles for 100 ms first, so a play that names the new track a moment after saying `playing` gives one state, not two. A newly connected relay gets the current cover, then the current state.

- `state=` is `none` (Soloist isn't running, isn't logged in, or another device is active), `stopped`, `paused` or `playing`. Soloist's `buffering` (a seek, a skip, a new context) keeps the state before it, so the relay's sink isn't stopped for a tenth of a second each time. A pause the bridge made for a stall stays `playing`: the device plays on from its buffer, and Q2 Pod would take `paused` as a reason to free the DAC and start its idle stop.
- With a track: `track=` (the Spotify id), `title=`, `artist=` (joined by `, `), `album=`, `duration=` and `position=` in ms, each value one line of at most 255 bytes. `position=` is Soloist's at the moment the bridge sends the frame; the relay stamps `at=` with its own `CLOCK_MONOTONIC` on arrival, and step 3 moves both by the buffer the device plays behind Soloist.
- `cover=1` once the relay has this track's JPEG: the bridge sends `j` (Soloist's `default` cover, about 300 px, fetched over HTTPS) before the first state that says so. The id in `j` lets the relay keep a cover with its track if it delays the state.

The bridge keeps Soloist at volume 100, setting it back whenever the phone changes it (librespot's `--volume-ctrl fixed`): the device's volume is its own.

## Audio

Each `A 1` starts a new encoder on the bridge, and each stop ends it, so nothing queues on the server while the relay isn't taking audio. The packets carry no Ogg framing: the relay writes its own single Ogg Opus stream into the decoder, so one stream continues across encoders, and the decoder can restart whenever it needs to.

Flow control is in the application, not left to TCP. Kernel buffers and container port proxies can hold tens of seconds of audio, which would play late as a backlog. So the bridge counts packets sent minus packets acked. When that goes over its in-flight limit, the link has stalled. The limit is the relay's buffer target from `A 1` (at least 300 ms), else `serve --inflight` (2 s). So a stall longer than the relay's buffer pauses Soloist, and what's left in flight when the link returns is about one buffer, plus what the encoder flushes as Soloist pauses. The bridge then pauses Soloist and ends the encoder, and waits for the relay's next `A 1`.

The relay holds the only buffer. It starts its sink once it holds its target (`buffer=`, or `-b`). It then feeds the decoder in real time, at most 300 ms ahead, rather than as fast as the pipe takes it: a pipe bounds bytes, and silence's packets are small enough that seconds of them would wait there uncounted. On an underrun it rebuffers to the target while audio keeps coming, without stopping Soloist, so nothing is missed. The relay never declares a stall itself: a 1 s hiccup on a 1 s LAN target would stop Soloist, when the bridge's in-flight limit rides it out. While it wants audio it repeats `A 1` every second, which the bridge ignores while it streams. After a stall the bridge restarts on the first `A 1` that comes once the acks have brought in-flight back under half the limit. So audio resumes only when the link carries data both ways, and the audio from before the stall, still in flight, plays on without a gap. With no bytes for 6 s while it wants audio, the relay takes the link for dead and reconnects; TCP keepalive (5 s idle, three 2 s probes) catches a dead link otherwise.
