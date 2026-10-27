# The relay ↔ bridge wire

Version 1, step 1's subset (PLAN.md; q2-pod's `docs/soloist.md`, "Relay ↔ bridge wire"). One TLS connection, which the relay on the card opens to the bridge, carries everything.

## Connection

1. The relay connects to an endpoint from `.soloify/remote` (`HOST PORT TOKEN [sha256=HEX] [buffer=MS]`, one a line, tried in order) and does TLS 1.2 or later. With `sha256=`, the relay accepts only a leaf certificate whose DER SHA-256 is `HEX` and checks nothing else (so the device's clock doesn't matter). Without it, the relay checks the chain against `/etc/scrobble-ca.pem` and the name against `HOST`.
2. The relay sends `hello 1 TOKEN\n`. The bridge compares the token in constant time and answers `ok 1\n`, or closes the connection. A second relay that says hello replaces the first, whose link may be dead without either side knowing yet.
3. After that both sides send frames: a type byte, a big-endian u32 payload length (at most 1 MiB), then the payload.

## Frames

| Type | Direction | Payload |
| --- | --- | --- |
| `A` | relay → bridge | `1`: send audio. `0 pause`: stop (step 3's local pause). |
| `k` | relay → bridge | u32: audio packets received since the hello, sent about every 200 ms while audio flows and at least every 5 s otherwise; the bridge drops a relay it hears nothing from for 20 s |
| `t` | relay → bridge | stats as text (`key=value` words), logged by the bridge |
| `a` | bridge → relay | u32 sequence number, then one Opus packet (20 ms, 48 kHz stereo) |

Reserved for later steps: `c` control, `q` Q2SPOT/1 request (relay → bridge); `s` state, `j` cover, `r` Q2SPOT/1 response (bridge → relay).

## Audio

Each `A 1` starts a new encoder on the bridge, and each stop ends it, so nothing queues on the server while the relay isn't taking audio. The packets carry no Ogg framing: the relay writes its own single Ogg Opus stream into the decoder, so one stream continues across encoders, and the decoder can restart whenever it needs to.

Flow control is in the application, not left to TCP. Kernel buffers and container port proxies can hold tens of seconds of audio, which would play late as a backlog. So the bridge counts packets sent minus packets acked. When that goes over its in-flight limit (`serve --inflight`, 2 s by default), the link has stalled. The bridge then pauses Soloist and ends the encoder, and waits for the relay's next `A 1`.

The relay holds the only buffer. It starts its sink once it holds its target (`buffer=`, or `-b`). On an underrun it rebuffers to the target while audio keeps coming, without stopping Soloist, so nothing is missed. The relay never declares a stall itself: a 1 s hiccup on a 1 s LAN target would stop Soloist, when the bridge's in-flight limit rides it out. While it wants audio it repeats `A 1` every second, which the bridge ignores while it streams. After a stall the bridge restarts on the first `A 1` that comes once the acks have brought in-flight back under half the limit. So audio resumes only when the link carries data both ways, and the audio from before the stall, still in flight, plays on without a gap. With no bytes for 6 s while it wants audio, the relay takes the link for dead and reconnects; TCP keepalive (5 s idle, three 2 s probes) catches a dead link otherwise.
