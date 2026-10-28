# Step 1: relay audio on the M1

PLAN.md step 1: the relay (`relay/`) on the M1 Plus's card, against `soloify serve` with Soloist on the NixOS laptop. Two passes on 2026-10-27, on the dev build of Q2 Pod 1.4.2 with q2remote: the first found a bug in the relay, and the second, with the fix, ran every check but the battery hour. The checks: delay at the target on home Wi-Fi and on a phone hotspot, a forced 15 s block, a network switch, the bitrate A/B, and an hour on battery. The battery hour ran later that evening, from a full charge with the laptop on Ethernet, and took about 90 minutes of streaming to get a figure (see "Battery hour").

Setup: the relay run by hand with `-q` (a minimal stand-in for librespot's pid file, control socket and `state=playing`, so Q2 Pod powers the DAC and holds standby once Streaming → Spotify is opened), `log.sh` beside it, `.soloify/remote` with the LAN address and the WAN one (a router port forward, which also answers from inside). Auto-standby Off, Screen Off set to Off for the pass. Soloist played an album on repeat on the user's account.

## Summary

| Check | Result |
| --- | --- |
| Delay, home Wi-Fi | play, pause and skip about 1–1.5 s at the 1 s target, about 2.5 s at 2 s (the user's stopwatch) |
| Delay, phone hotspot | about 4 s at the 4 s WAN target; ring 3.6–3.7 s, in-flight 160–300 ms, no underruns |
| Forced 15 s block (`tools/soloify-stall 15`), twice | the bridge pauses Soloist, audio comes back by itself as the block lifts, and the ring is the same as before (no growing delay) |
| Network switch | home → hotspot and back reconnect by themselves; back home the relay takes the LAN address again |
| Bitrate A/B | the user hears no difference between 96 and 160 kbit/s |
| Hour on battery | the gauge held 100% for the whole hour; with 30 more minutes it fell about 0.16%/min with the screen dark (roughly 10 hours from a full charge); the relay ran clean throughout (see "Battery hour") |

**Defaults (user, 2026-10-27):** the delay target and the bitrate become settings on the device once the UI exists (PLAN.md section 5), defaulting to **1 s** on the LAN (4 s beyond it) and **160 kbit/s**. A longer target trades delay for fewer dropouts: the user would pick it on mildly spotty Wi-Fi.

## Findings

- **The relay spun after a bridge stall** (pass 1, fixed in `06a2b3f`). The ack timer moved on only when there was something new to ack, so 200 ms without a packet left the loop's wake time in the past, and `relay_wait` returned without polling. The relay ran at ~92% CPU, stopped reading the link and feeding the sink, and read again only for 200 ms after each 5 s idle ack. On the M1 the bridge then stalled again after every restart (44 stalls, 4 s underruns), because Soloist's resume takes longer than that window. Now the timer always advances and `relay_wait` polls once even past its deadline. `tests/test_relay_audio.py::test_a_stall_neither_spins_the_relay_nor_repeats` measures the relay's CPU through a freeze, with an encoder that starts late, as Soloist's resume does.
- **With the fix the relay idles.** About 1% of the CPU (log.sh's ticks) on the LAN, through stalls and outages alike; 6.7 MB resident; ~80 MB of RAM left free. The stock ffmpeg decodes for about 5%.
- **The ring sits a little under its target.** 840–1060 ms at 1 s, 1600–1800 ms at 2 s, 3600–3700 ms at 4 s. The control delay is about the ring plus half a second: ALSA's buffer is 22528 frames (511 ms), and the bridge's encoder adds its pages.
- **Stalls follow the Wi-Fi, not the relay.** The neighbourhood's 2.4 GHz is congested: the laptop-to-M1 round trip averaged 189 ms (max 410 ms) while the laptop-to-router one was 26 ms, at -58 dBm. That stalled the bridge at the 2 s in-flight limit twice in 15 minutes, for 4–7.5 s each. At the 1 s target, 7 stalls came in 4 minutes. Each one paused Soloist and resumed it within 0.5–1.6 s of the link coming back, with no repeat.
- **The laptop was the worst link.** About every 5 minutes its Wi-Fi tried a 6 GHz access point, failed to authenticate and was offline 6–7 s (journalctl, 09:00:05 and 09:05:20). A server on Ethernet won't do that; it caused the one dropout heard during the 2 s run.
- **A block longer than 6 s is a reconnect, not a stall.** The relay takes 6 s without bytes as a dead link, so a 15 s block ends with a fresh connection rather than the stalled one resuming. Either way the bridge pauses Soloist at the in-flight limit and audio resumes on the new link at once.
- **After a fallback the relay stays on the WAN address.** When the LAN connect times out (1.5 s) during an outage, the relay connects through the WAN address and keeps it until the next reconnect. Over home Wi-Fi that means hairpinning through the router; it played fine, but it's worth retrying the LAN address later.
- **Bitrate.** The M1 receives 27.3 KB/s at 160 kbit/s and 18.7 KB/s at 96 (framing, TLS and acks included), and sends about 5 KB/s either way.
- **Leaving the hotspot cost about a second.** The 4 s ring covered most of the 6 s it took to notice the dead link and reconnect at home.
- **Battery drain with the screen on:** 47% to 36% in 17 minutes (Screen Off set to Off), so roughly 0.65% a minute. The screen-off figure is in "Battery hour" below.
- **The first aplay fails behind local music.** With local music paused, Q2 Pod's player keeps the PCM until Streaming → Spotify is opened, so the relay's first aplay blocks, then fails with an I/O error, and the ring grows (to 18.8 and 25.9 s) until the drift trim cuts it back. Step 3's full contract, which starts the sink only on `spot_take`, removes this.
- **librespot leaves `/tmp/q2-librespot.tmp` as a directory.** `-q`'s pid file, written through `.tmp`, failed; it uses `.new` now (`cb28c63`).
- **Stock Standby suspends the M1 while the relay plays without Q2 Pod's hold** (Wi-Fi off, card unmounted). Pass 1 lost a run to it before Streaming → Spotify was opened; the user turned Auto-standby off for the passes.

## For later steps

- Settings UI (step 5): Soloify's delay target and bitrate on the device, defaulting to 1 s on the LAN, 4 s beyond it, and 160 kbit/s. The relay applies the target; the bitrate is the bridge's, so the relay passes it along with its audio request.
- Relay contract (step 3): start the sink on `spot_take`, so the first aplay never races local music's PCM hold.
- Relay: go back to the LAN address after a WAN fallback, for instance by retrying it at the next idle moment.
- Bridge host: Ethernet was steady for the battery hour. Opening the bridge's port (7443) has to cover every interface the host serves on, not only Wi-Fi; worth a line in the setup docs.

## Battery hour

Run on the evening of 2026-10-27 from a full charge: the M1 unplugged at 100%, Screen Off 1 min (the user's own value), Auto-standby Off, the defaults (1 s target on the LAN, 160 kbit/s), Soloist playing an album on repeat, and `log.sh` writing the battery and the relay's load every 10 s. The bridge ran on the laptop on **Ethernet** (192.168.1.200), after a ten-minute trial: Wi-Fi had dropped out about every five minutes in the passes, and Ethernet had no reconnect, underrun or stall in the ten minutes. The M1's connect to the Ethernet address timed out until the user opened port 7443 in the laptop's firewall (`nixos-firewall-tool open tcp 7443`, a rule that lasts until `nixos-firewall-tool reset`), so the laptop's earlier rule evidently covered Wi-Fi only.

| | First hour | Second leg |
| --- | --- | --- |
| Window (device time) | 01:34:18 to 02:34:20 | 02:35:58 to 03:07:55 |
| Gauge | 100% in all 360 samples | 100, 99 at 02:38, 98 at 02:44, 97 at 02:51, 96 at 02:57, 95 at 03:03 |
| Voltage (spot reads) | 4.180 V (about minute 33), 4.155 V (43), 4.137 V (53), 4.122 V (end) | 4.110 V (start), 4.053 V (end) |
| Relay | 0 reconnects, 0 drops, 1 underrun (about minute 20), ring 0.84 to 1.10 s | 0 reconnects, 0 underruns, ring 0.64 to 0.70 s |
| Relay CPU | 1.1 ticks/s (about 1%) | 1.2 ticks/s |
| Bridge stalls | 0 | 0 |

- **The gauge could not resolve the first hour.** It never left 100% although the voltage fell; the percentage reads flat across the top of the curve. A percentage-only log could not give a figure, so the user asked for 30 more minutes (the relay and logger were stopped for about a minute and a half in between, and the panel was lit for about a minute to reopen the Spotify page).
- **Screen-off drain: about 0.16% a minute.** Four steps (99 to 95) in 25.4 minutes, 6.0 to 6.5 minutes each: roughly 9% an hour, so about 10 hours from a full charge if the gauge stayed linear, against 0.65% a minute with the screen on. The 100% plateau itself lasted about 64 minutes, so the first percent costs more time than the rest; the slope is from the steps only.
- **The relay and the decoder cost about 1% of the CPU** over both legs, with 6.8 MB resident, 78 MB of RAM free, about 28 KB/s received and 5 KB/s sent (the same as the passes).
- **Not measured:** the drain without the relay (local music or idle) for comparison. The user chose the Soloify hour alone.
