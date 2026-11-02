# Step 5: firmware seams

Soloify is now an app in Q2 Pod's firmware (M1 Plus only): its own Streaming row and Configuration switch, a second backend on Spotify's page, Delay and Quality settings, and an Apps row. The firmware is in q2-pod, branch `step5-soloify` off `staging` (commits `dbbba1e`, `6855871`, `9788114`). This repo gained the settings plumbing. What the firmware does is written up in q2-pod's `docs/soloist.md` ("What the firmware does") and `docs/internals.md`.

## What changed here

- **Relay.** It reads `.delay` (milliseconds) and `.quality` (96, 128 or 160) beside `remote`, at each connection. The buffer target is `-b`, else `.delay`, else the endpoint's `buffer=`, else 1 s on the LAN and 4 s beyond. A valid `.quality` goes out as the bitrate field of `A 1 MS KBPS`; anything else is ignored.
- **Bridge.** `A 1 MS [KBPS]`: a bitrate of 96, 128 or 160 sets the encoder for the run that request starts, otherwise `--bitrate`. An old relay, with no field, is unchanged.
- **Docs and card.** `docs/wire.md`'s `A` row, and `card/README.md` for the two files, which a ZIP never holds, so updates and Remove keep them.

## Laptop gates (2026-11-01)

| Gate | Result |
| --- | --- |
| q2-soloify tests | 254 passed, 1 skipped, including the settings precedence, invalid files, the bitrate field and a bad bitrate; the host and MIPS relays build |
| q2-pod `test/m1plus/patch.py` | 1381 MIPS scenarios passed (1374 before), 0 known gaps, 35 not ported |
| `build`, `boot` (plain, `--home-menu`, `--media`), `playback`, `m4b`, `id3`, `headset_io`, `ldac`, `bluealsa` | pass |
| `test/m1plus/ports.py` | pass |
| `test/q2compare.py --suites` | 44 suite runs passed; Q2 Legacy and iPod `update.tar` byte-identical to 1.5.8's build |

Existing tests changed in two ways: the M1's `SPOT_KILL` text now names `q2-soloify` (`KILLALL` in `test/m1plus/patch.py`; the refused-key path's own `killall` is unchanged), and the Apps lists end with the Soloify row. New tests cover the Soloify page, rows, texts, switching, Configuration and Apps.

## M1 pass (2026-11-01)

Build `V02Ds` ("CFW. Version 1.5.8 dev 14"), flashed by the user through System Update → TF card update, with the relay `e95d3036…` (the laptop's final MIPS build), `run` and `aplay.sh` from `card/`, and a fresh `remote`. The bridge ran on the laptop on the home LAN, Soloist logged in. Not repeated, as already proven: audio, pause and rewind, USB unplug, the browsing operations, the 30-minute run, hotspot stalls, battery.

| ID | Result |
| --- | --- |
| D1 | Pass. `demo`'s checksum matches the build, About shows dev 14, and `config.ini` is identical to the backup after the flash |
| D2 | Pass. The Soloify row follows Spotify with Spotify's icon, and Configuration has its switch (the SOLOIFY key hides and shows it; the hide list is by name). The row's absence without `q2-soloify` is host-tested only |
| D3 | Partly. The page's title, Now Playing tag and menu say Soloify and the menu loads. The "no `remote`" message and the "isn't running" wording are host-tested only; they weren't repeated on the device |
| D4 | Pass. The search term survives the category level in all four categories (the bridge saw `term_bytes=1`, where step 4's search failed with none), Back, and More… keeps it. Unicode through the T9 keyboard was not typed on the device; the host regression covers it |
| D5 | Pass. Soloify's menu ends at Account (no Downloaded Tracks or Downloads), and Spotify's page still shows Downloads. Soloify's Account and playlist rows were host-tested |
| D6 | Mostly. A search result plays with art, the "Soloify" tag and progress, and holding Play/Pause adds the track to the queue. **Scrub fails**: a double Centre press locks the screen instead of entering scrub, and a tap on the progress bar doesn't seek. This is step 3's finding again |
| D7 | Pass. Opening Spotify from Soloify leaves only librespot, and the reverse leaves only `q2-soloify`; Stop Streaming Services leaves none of `q2-soloify`, `librespot` or `aplay`. The idle stop was not waited for |
| D8 | Pass. Delay and Quality rows, labels and files `.delay`/`.quality` as planned. The first change stopped the running Soloify with a toast. After Delay 4 s and Quality 96, the relay's log shows `buffer 4000 ms, quality 96 kbit/s`, the bridge reports `target=4000`, and its encoder ran at 96k |
| D9 | Pass. The App Store lists Soloify last as "Update" (a binary without a tag), the other rows unchanged, and the saved `sources.list` has no Soloify line. Install and Remove were not tried: there is no release yet (step 6) |
| D10 | Repeat steps Off → All → One → Off. Not checked: the enqueue acknowledgment (the toast had gone before the screenshot; the Queue showed the track), the U+2019 gap, and volume 13 against 10 |

### Findings

1. **Scrub is not reachable on the M1** (D6). Spotify's page takes a double Centre press as scrub, but the screen locks first. Plan a fix separately; the tap on the bar does nothing either.
2. **Stale lock files after a stop.** `SPOT_KILL` kills the loop with `kill -9`, so `/tmp/q2-librespot.runlock` and `.lock` stay. The next launch recovered from them in both backends, so this is only noted.
3. **Every App Store check asks GitHub for `jtliang24/q2-soloify`'s release.** It answers 404 until step 6 publishes one, so the check never counts as complete and repeats when the page opens, against the API's 60 requests an hour.
4. Opening the Soloify row for the first time runs `SPOT_KILL` before the launcher (the backend switch): harmless, and it covers a process that outlived `demo`.

### Restoration

Screen Off back to 5 minutes, volume 22 and the shuffle and repeat state as found (shuffle on, repeat off); `.delay` and `.quality` removed; streaming stopped; bridge and Soloist stopped. `config.ini` differs from the backup only by an empty `STREAMHIDE=` line, which reads the same as none. The new `.soloify` files (relay, `run`, `aplay.sh`, `remote`) are left installed so the Soloify row works; the folder as it was is in `/mnt/mmc/.soloify-step5-backup`, and the image that was flashed over is `/mnt/mmc/.q2/update-pre-step5.bin`. The dev build stays flashed for step 8. The user closes TCP 7443 with `sudo nixos-firewall-tool close tcp 7443`.
