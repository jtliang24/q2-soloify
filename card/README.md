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
| `run` | backgrounds itself, owns `/tmp/q2-librespot`, starts the relay with `-q`, and restarts it with 5–60 s backoff; logs to `spot.log` |
| `aplay.sh` | the sink, unchanged from q2-librespot's `.spotify/aplay.sh`, so `SPOT_OUT` and Q2 Pod's Bluetooth and USB output work alike |
| `remote` | made by guided setup or `soloify remote` on the bridge's machine; `remote.sample` explains it. Never in the ZIP, so updates and Remove keep it |
| `.delay`, `.quality` | the firmware's Soloify Delay (milliseconds) and Quality (96, 128 or 160 kbit/s) settings, one integer each. The relay reads them at each connection: `-b` beats `.delay`, which beats the endpoint's `buffer=`; with neither, 1 s on the LAN and 4 s beyond it. `.quality` goes to the bridge in the `A 1` request, and without it the bridge's `--bitrate` applies. Never in the ZIP, so updates and Remove keep them |
| `log.sh` | step 1's measurements: battery, Wi-Fi bytes, load, the relay's CPU and memory, into `load.log` |

Compatible firmware calls `run` from the Soloify row. To run the launcher by hand on a development device:

```sh
cd /mnt/mmc/.soloify
/bin/sh ./run
```

A TERM to the pid in `/tmp/q2-librespot` stops the loop and relay. With `-q`, Q2 Pod manages the DAC and local-music handover. Only one Spotify/Soloify backend may run at a time. Running the binary without `-q` remains useful for audio-only tests; that mode needs exclusive access to the output.
