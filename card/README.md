# The card's files

What goes in `.soloify/` on the device's card (PLAN.md section 1). Step 1 runs the relay by hand; `run`, the restart loop with Q2 Pod's pid file, arrives with the librespot contract in step 3.

| File | What |
| --- | --- |
| `q2-soloify` | the relay, from `relay/build.py --rootfs …` (one binary for the Q2 and the M1 Plus) |
| `aplay.sh` | the sink, unchanged from q2-librespot's `.spotify/aplay.sh`, so `SPOT_OUT` and Q2 Pod's Bluetooth and USB output work alike |
| `remote` | made by `soloify remote` on the bridge's machine; `remote.sample` explains it. Never in the ZIP, so updates and Remove keep it |
| `log.sh` | step 1's measurements: battery, Wi-Fi bytes, load, the relay's CPU and memory, into `load.log` |

By hand, from a shell on the device (q2remote on a dev build):

```sh
cd /mnt/mmc/.soloify && setsid nohup ./q2-soloify -v > relay.log 2>&1 < /dev/null &
setsid nohup ./log.sh > /dev/null 2>&1 < /dev/null &
```

Stop both with `kill`. Nothing else may be playing: the relay's sink needs the DAC.
