# The card's files

The [guided setup](../README.md#guided-setup) exports this folder for the device's card.
The matching maintainer card ZIP includes the MIPS relay, `run`, `aplay.sh`, and release
version metadata. Setup adds a private `remote`; releases contain no credentials or
user settings. Firmware must include Q2-Pod's **Streaming → Soloify** row from the
`step5-soloify` integration, with protocol v2. Firmware installation is separate.
Existing installations, including future Apps installations, can export only `remote`.
The manual [M1 runbook](../docs/step4-runbook.md) remains available for development.

| File | What |
| --- | --- |
| `q2-soloify` | the relay, from `relay/build.py --rootfs …` (one binary for the Q2 and the M1 Plus) |
| `run` | backgrounds itself, owns `/tmp/q2-librespot`, starts the relay with `-q`, and restarts it with 5–60 s backoff; logs to `spot.log`. In `.soloify` on a Q2 it runs the manager instead (below). On the M1 Plus it deletes `.soloify/q2app`, so the firmware lists no second Soloify row |
| `aplay.sh` | the sink, unchanged from q2-librespot's `.spotify/aplay.sh`, so `SPOT_OUT` and Q2 Pod's Bluetooth and USB output work alike |
| `remote` | written by pairing with a code (`soloify pair-device` on the bridge's machine, the code shown by the manager's Pair with server or, with no `remote`, by the relay's page as its error), or made by guided setup or `soloify remote` and copied here; `remote.sample` explains it. Never in the ZIP, so updates keep it |
| `away` | `verified <unix time> <host>` lines: the relay's first connection through each WAN line (any `remote` line after the first). A pairing with another WAN host removes it. WAN failures in a row, with no LAN success between them, are counted in `/tmp/q2-soloify.away` |
| `.delay`, `.quality` | the firmware's Soloify Delay (milliseconds) and Quality (96, 128 or 160 kbit/s) settings, one integer each. The relay reads them at each connection: `-b` beats `.delay`, which beats the endpoint's `buffer=`; with neither, 1 s on the LAN and 4 s beyond it. `.quality` goes to the bridge in the `A 1` request, and without it the bridge's `--bitrate` applies. Never in the ZIP, so updates and Remove keep them |
| `log.sh` | step 1's measurements: battery, Wi-Fi bytes, load, the relay's CPU and memory, into `load.log` |

On a Q2 with upstream Q2 Pod, `q2app` lists Soloify in Library → Apps, and its `run` starts
`q2-soloify --manage`: the app's page at `/tmp/q2-soloify`, with Status (what the Spotify slot
holds, whether `remote` is there, whether the bridge answers a `ping`, and Away: not set up, not
yet verified, verified on a date, or not reached after 3 WAN failures), Pair with server, the swap's rows (below),
Delay and Quality. Pair with server finds the bridge by broadcast (or asks for its address), shows the code
to type into `soloify pair-device`, and writes `remote`. It writes `remote`, `.delay` and
`.quality` here, and in `.spotify` too while that runs the relay, and ends itself
after 2 minutes unused. `run` tells the devices apart by `/sys/class/graphics/fb0/virtual_size`
(the Q2's 320 × 375 framebuffer, two pages: `320,750`; the M1 Plus's `320,960`) and the M1 Plus's
`Goodix-CTP` touchscreen in `/proc/bus/input/devices`; a device that is neither is taken for a Q2.

The manager's swap rows put the relay in the Spotify slot's place (upstream Q2 Pod on the Q2
only). **Use Soloify for Spotify** stops Spotify (a TERM to the pid in `/tmp/q2-librespot`, then
any `librespot` left; Spotify running as root, before Q2 Pod 1.9.1, needs **Stop Streaming
Services** first), backs up `.spotify/{librespot,run,aplay.sh}` once into `.soloify/backup/`
with a `SOURCE` line (librespot's size and SHA-256), copies `aplay.sh`, `run`, `remote`, `.delay`
and `.quality` into `.spotify`, then the relay as `.spotify/librespot` last, and records its hash
in `backup/relay.sha256`. Every copy is written aside, synced and renamed, so a cut copy leaves
`*.tmp` files and the old binary, and trying again mends it. `.spotify/cache` and `.cache-size`
are never touched, and a relay never replaces a backup of real librespot. **Update Spotify's
copy** shows when the slot runs an older relay, **Repair** when the App Store's Spotify Update
put librespot back over the swap (that librespot becomes the backup), and **Restore Spotify**
while the relay is in place: it copies the backup back (librespot last) and deletes `.spotify`'s
`remote`, `.delay` and `.quality`; `backup/` stays. Without a backup it removes only Soloify's
files and says to install Spotify from the App Store. The App Store's Remove of Soloify deletes
`.soloify`, `backup/` included, but not `.spotify`: Restore first.

Compatible firmware calls `run` from the Soloify row. To run the launcher by hand on a development device:

```sh
cd /mnt/mmc/.soloify
/bin/sh ./run
```

A TERM to the pid in `/tmp/q2-librespot` stops the loop and relay. With `-q`, Q2 Pod manages the DAC and local-music handover. Only one Spotify/Soloify backend may run at a time. Running the binary without `-q` remains useful for audio-only tests; that mode needs exclusive access to the output.
