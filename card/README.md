# The card's files

What goes in `.soloify/` on the device's card (PLAN.md section 1). Step 3 adds `run`, the restart loop with Q2 Pod's pid file, and the relay's `-q` device contract. Step 4 adds browsing over TLS; its [M1 runbook](../docs/step4-runbook.md) is pending execution. The Soloify firmware and Apps rows arrive in step 5; the existing Spotify page was used for the temporary compatibility check ([results](../docs/step3.md)).

| File | What |
| --- | --- |
| `q2-soloify` | the relay, from `relay/build.py --rootfs …` (one binary for the Q2 and the M1 Plus) |
| `run` | backgrounds itself, owns `/tmp/q2-librespot`, starts the relay with `-q`, and restarts it with 5–60 s backoff; logs to `spot.log` |
| `aplay.sh` | the sink, unchanged from q2-librespot's `.spotify/aplay.sh`, so `SPOT_OUT` and Q2 Pod's Bluetooth and USB output work alike |
| `remote` | made by `soloify remote` on the bridge's machine; `remote.sample` explains it. Never in the ZIP, so updates and Remove keep it |
| `log.sh` | step 1's measurements: battery, Wi-Fi bytes, load, the relay's CPU and memory, into `load.log` |

Once firmware supports the Soloify backend, its row calls `run`. To run the launcher by hand on a development device:

```sh
cd /mnt/mmc/.soloify
/bin/sh ./run
```

A TERM to the pid in `/tmp/q2-librespot` stops the loop and relay. With `-q`, Q2 Pod manages the DAC and local-music handover. Only one Spotify/Soloify backend may run at a time. Running the binary without `-q` remains useful for audio-only tests; that mode needs exclusive access to the output.
