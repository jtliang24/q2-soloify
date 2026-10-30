# Step 4: home-network M1 verification

The initial pass completed on 2026-10-28; results and findings are in
[step4.md](step4.md). Use this checklist for a repeat pass. Do not flash firmware.
Complete the checklist, collect evidence and restore the device before changing
the implementation in response to findings.

## Prerequisites and connections

Use home Wi-Fi/Ethernet without guest/client isolation. Both directions matter:
laptop → M1 TCP **2302** (q2remote), M1 → laptop TCP **7443** (TLS bridge).
The server also needs DNS and outbound HTTPS to Spotify, a paired Soloist session,
its API key, and completed Web API OAuth. Soloist pairing and Web API login are
separate. Never run two copies of the paired Soloist data directory concurrently.

Run these commands from the q2-soloify checkout in `nix develop`. Substitute actual
addresses; `192.168.1.210` was the M1 address in step 3, not a permanent assignment.
Keep the evidence directory private because unredacted state/logs contain account,
device and track identifiers. The pairing file contains a bearer credential.

```sh
nix develop
export Q2REMOTE=../q2-pod/tools/q2remote.py
python "$Q2REMOTE" find
export Q2_HOST=192.168.1.210
export BRIDGE_LAN=192.168.1.100
export EVIDENCE=build/step4-m1
mkdir -p "$EVIDENCE"
chmod 700 "$EVIDENCE"
ip -brief address
ip route get "$Q2_HOST"
python "$Q2REMOTE" state
python "$Q2REMOTE" sh 'ip addr; ip route; uname -a'
```

Use the router's DHCP leases or the device's network settings if discovery fails.
`state` must return the installed firmware/device state, not an authentication or
connection error. q2remote needs its existing key file; do not print the key.
Record the installed version from Settings/About and screenshot it. Record
`git rev-parse HEAD` for both checkouts, `soloify --version`, `soloist --version`,
rootfs source/checksum, laptop/M1 addresses and the time of the pass.

On NixOS, temporarily open TCP 7443 if it is not already allowed:

```sh
sudo nixos-firewall-tool open tcp 7443
```

On another server, use that host's firewall tool to allow inbound TCP 7443 from the
M1/home subnet. Allow the laptop to reach M1 TCP 2302. No WAN port forwarding is
needed for this home-network pass. Record which rules were added, to remove only
those rules at cleanup. A guest SSID can block peers even with both firewalls open.

Authorize on the laptop if needed. Register exactly
`http://127.0.0.1:8888/callback` in the Spotify dashboard app; neither `localhost` nor
an extra slash matches. Set `SPOTIFY_CLIENT_ID` or use `--client-id ID`:

```sh
python -m soloify.cli --data data login --client-id ID
stat -c '%a' data/oauth.json
```

Open the printed URL on this laptop; expected callback: “Return to soloify on the
server.” Expected file mode: `600`. Browser denial/state mismatch/timeout does not
erase an existing authorization. Keep `data/oauth.json`, Soloist state, API keys,
bridge private key/token and generated `remote` out of screenshots and shared logs.
For a remote/container bridge, run login on the laptop and securely copy the OAuth
file into the service's data directory, mode 0600, or forward its loopback callback
port. The file must belong to the account paired with Soloist.

Stop competing laptop bridge/Soloist/container services before starting the test
bridge; stop services using their normal service manager. Start one bridge:

```sh
python -m soloify.cli --data data serve --listen 0.0.0.0 --port 7443 > "$EVIDENCE/bridge.log" 2>&1 &
export BRIDGE_PID=$!
ss -ltn 'sport = :7443'
```

Expected: a listener on 7443 and Soloist's loopback WebSocket. Soloist plays to the
`q2_soloify` null sink. `tools/soloify-sink level` can measure that sink; inspect
`pw-link -l` on native PipeWire to confirm Soloist feeds `q2_soloify`. Select Q2 in
the phone's Spotify app if Soloist is logged in but inactive. A service started with
`--source test` checks transport only and cannot validate account/playback actions.

Check M1 → bridge before installing anything:

```sh
python "$Q2REMOTE" sh --timeout 5 "telnet '$BRIDGE_LAN' 7443 </dev/null"
```

Expected: telnet reports a connection, then exits at EOF because
telnet does not speak TLS. The installed M1 has neither `nc` nor the shell's `timeout`; use telnet and q2remote's host-side deadline. If that deadline expires, q2remote identifies the remote process group to stop.
A successful relay TLS/hello after installation is the definitive check.
A TCP success alone does not verify the certificate pin or token.

## Safe preparation and verified relay

Record original Screen Off, standby, volume, output mode, shuffle/repeat, selected
backend and current playback. Temporarily set Screen Off to Off and standby Off
through the UI; use a low comfortable volume for the hardware audio checks. Stop
playback and competing device streaming backends through their existing UI before
replacing files. Verify no backend still owns ALSA.

Build the MIPS relay, never the host binary for installation:

```sh
python relay/build.py --rootfs ../q2-pod/build-m1/stock.squashfs
sha256sum build/relay/q2-soloify > "$EVIDENCE/relay.sha256"
umask 077
python -m soloify.cli --data data remote --lan "$BRIDGE_LAN" > "$EVIDENCE/remote"
```

The build prints its checksum. Record it in the result sheet. The laptop-verified MIPS relay checksum is recorded in [step4.md](step4.md); a changed source revision requires fresh laptop verification. `remote` must name
the laptop's home-network address, not 127.0.0.1. Bridge and relay must both include
step 4 follow-up (`q`/`r`, `B` reset-begin, `g` generation/warmup and `e` final-sample frames).

The installed older hub used **Q2 Pod → Spotify** in step 3. For this firmware,
temporarily use `/mnt/mmc/.spotify`: replace `librespot` with the verified relay,
plus `run`, `aplay.sh` and a temporary `remote`. `card/run` falls back to
`./librespot` when `./q2-soloify` is absent. If `.spotify/q2-soloify` already exists,
record it and back it up too: that executable takes precedence. Keep `.spotify/cache`,
`.cache-size`, saved login and `.soloify` unchanged. Do not install firmware or Apps.
If installed firmware already supports a Soloify row, use `.soloify` instead,
adapt the backup manifest and install as `q2-soloify`; record that route explicitly.

Create a fresh backup; never overwrite step 3's backup. This manifest records both
present files and absences, including a pre-existing `remote`:

```sh
python "$Q2REMOTE" sh '
set -e
b=/mnt/mmc/.soloify-step4-backup
[ ! -e "$b" ] || { echo "Backup already exists: choose a fresh name"; exit 1; }
mkdir "$b"
cd /mnt/mmc/.spotify
for f in librespot run aplay.sh remote q2-soloify; do
  if [ -e "$f" ]; then
    cp -p "$f" "$b/$f"
    sha256sum "$f" >> "$b/original.sha256"
  else
    echo "$f" >> "$b/absent"
  fi
done
sync
cd "$b"
sha256sum -c original.sha256
'
python "$Q2REMOTE" pull /mnt/mmc/.soloify-step4-backup/original.sha256 "$EVIDENCE/original.sha256"
```

Expected: every present original checksums OK. Verify that the backup's copies
match the source before proceeding; retain the manifest and absence list. If a
backup was already there, stop and choose a new explicit directory consistently
for installation and restoration. Do not reuse it silently.

```sh
python "$Q2REMOTE" push build/relay/q2-soloify /mnt/mmc/.spotify/librespot
python "$Q2REMOTE" push card/run /mnt/mmc/.spotify/run
python "$Q2REMOTE" push card/aplay.sh /mnt/mmc/.spotify/aplay.sh
python "$Q2REMOTE" push "$EVIDENCE/remote" /mnt/mmc/.spotify/remote
python "$Q2REMOTE" sh 'chmod 755 /mnt/mmc/.spotify/librespot /mnt/mmc/.spotify/run /mnt/mmc/.spotify/aplay.sh; chmod 600 /mnt/mmc/.spotify/remote; sha256sum /mnt/mmc/.spotify/librespot; sync'
```

If an existing `.spotify/q2-soloify` was backed up, also replace that path with the
same verified relay for this temporary test (restore it afterward):

```sh
python "$Q2REMOTE" push build/relay/q2-soloify /mnt/mmc/.spotify/q2-soloify
python "$Q2REMOTE" sh 'chmod 755 /mnt/mmc/.spotify/q2-soloify; sha256sum /mnt/mmc/.spotify/q2-soloify'
```

 Compare the
installed checksum to `relay.sha256`. Open Spotify in the installed hub; expected
relay log: `state and controls ready`, then `connected to ADDRESS:7443`. Retry
reloads configuration and wakes reconnect backoff. Do not repeatedly launch it by
hand while the UI is starting its restart loop.

## Ordered functional checklist

Record **pass/fail/deferred**, evidence and the actual UI route for each row. A
helper/socket check does not count as a firmware UI pass. Older firmware may lack
capability-based hiding, artist continuation, scrub, a Soloify row, Stop Streaming
Services or Rockbox routes. Mark unavailable routes deferred; do not flash to add
them. Unsupported download/sign-out entries may remain visible on older UI.

| ID | Action and expected result |
| --- | --- |
| F01 | Open account/menu; correct display name, no invented Premium/subscription when Spotify omits it. No OAuth gives actionable server-login copy while Connect audio can continue. |
| F02 | Inspect capabilities (UI where available, otherwise protocol evidence): 17 operations; downloads/sign-out absent. An old UI's visible unsupported action gives the explanation. |
| F03 | Open Playlists, Liked Songs, Saved Albums and Followed Artists; Unicode names and literal percent/tab/newline escaping do not corrupt rows. |
| F04 | Open a saved album and an artist; album tracks have the right order, artist shows albums with the top-track restriction explanation. |
| F05 | Search tracks, albums, artists and playlists separately, including a Unicode term. Search and artist pages have at most 10 results, other pages at most 50. |
| F06 | Navigate another page in each section with enough results. Cursors preserve query/list identity; a list may show fewer rows after unavailable tracks are filtered. Older artist UI without More is deferred for artist continuation. |
| F07 | Open a known editorial/restricted playlist (e.g. Today's Top Hits). Expect `unavailable` and restricted-contents copy, with Retry/back usable. Empty accessible lists remain valid. |
| F08 | Play a middle album/playlist row. Verify selected track and the following track from its context, including continuation beyond a visible page when practical. |
| F09 | In an existing accessible playlist with duplicate entries, select the later copy. Verify context offset identifies that copy by its following neighbor. URI alone cannot prove which duplicate was selected. No suitable fixture: deferred, with reason. |
| F10 | Play Liked Songs. This laptop/account accepted native context playback. If rejected on this app/account, expect the URI-list fallback message: continuation stops at the end of the fetched playable page (at most 50). |
| F11 | Now Playing title/artist/album, complete artwork and advancing position agree with the selected track. Capture a screenshot and state; initial position settles as output begins. |
| F12 | Pause/resume from UI and hardware. Pause drains queued audio and the source fade, then reaps the sink/releases ALSA before reporting paused. Expect the buffer delay plus an output tail; resume starts fresh without a delayed blip. Verify phone controls too. |
| F13 | Next/previous and UI seek/scrub change to the right track/position, without pre-reset audio. Installed centre gesture toggled screen in step 3: defer UI scrub if still absent. A direct `S60000` control is separate diagnostic evidence. |
| F14 | Enqueue one track; Queue shows upcoming tracks in order. Select a queue position, verify it starts and earlier entries were skipped. |
| F15 | Open Queue, change it from the phone, then choose a now-stale row. Expect `unavailable`, refresh instruction and no unintended skip. Reopen Queue and choose the current row successfully. |
| F16 | Shuffle on then off, verify on phone/local settings and subsequent playback. |
| F17 | Repeat off → context → track → context → off. Verify each setting and no leftover second repeat flag. Restore original options at cleanup. |

Screenshots and diagnostics, naming each by its checklist ID:

```sh
python "$Q2REMOTE" shot --settle "$EVIDENCE/F11-now-playing.png"
python "$Q2REMOTE" state > "$EVIDENCE/F11-ui-state.json"
python "$Q2REMOTE" sh 'cat /tmp/q2-librespot.state; cat /proc/asound/card0/pcm0p/sub0/status; ps' > "$EVIDENCE/F11-device-state.txt"
python "$Q2REMOTE" pull /mnt/mmc/.spotify/spot.log "$EVIDENCE/relay.log"
```

Do not copy `remote` or OAuth tokens into the published results. Redact bearer
credentials, account/device identifiers and private playlist details from shared
logs. Keep private originals only as long as needed.

## Integration and recovery checklist

| ID | Action and expected result |
| --- | --- |
| I01 | Browse/search/page while audio plays; position, covers, acknowledgements and hardware controls continue. No growing latency/backlog. |
| I02 | Stop the test bridge, open a browse page: offline/Retry within 12 seconds. Restart with the same data/certificate/token, press Retry and confirm menu recovery. |
| I03 | Submit enqueue/play/skip once during a lost connection, then reconnect. No action is replayed on reconnect; make a fresh explicit request to retry. Capture before/after queue/state. |
| I04 | Optional short/long link loss using `tools/soloify-stall 3` and `15` on the laptop. It temporarily blocks 7443 only and removes its table on exit. Recovery resumes bounded audio without accumulating delay. Do not block TCP 2302. |
| I05 | Start local music (Bucket) from the installed UI; Soloist pauses and local player takes ALSA. Recast/open Spotify and verify handover back. |
| I06 | Pause, close the browser and wait the installed idle-stop interval. Launcher/relay/sink and state disappear; local playback can continue. Record the actual elapsed interval. |
| I07 | Exercise Stop Streaming Services and Rockbox through firmware UI where those rows exist. Record absent routes as deferred. |
| I08 | Exercise the shared stop command below with Spotify as the only streaming output: launcher, relay, ffmpeg and aplay gone; ALSA closed. |

The current q2-pod checkout's `SPOT_KILL` is below. Record the definition associated
with the installed firmware too; if it differs, test and record that version.
It kills all `aplay` processes, so perform it with local/other playback stopped:

```sh
python "$Q2REMOTE" sh 'kill -9 $(cat /tmp/q2-librespot); killall -9 librespot aplay; rm -f /tmp/q2-librespot /tmp/q2-librespot.state /tmp/q2-librespot.out /tmp/q2-librespot.download'
python "$Q2REMOTE" sh 'ps; cat /proc/asound/card0/pcm0p/sub0/status'
```

Do not deliberately invalidate live tokens or induce 429 responses. Injected
401/429, timeout and malformed-response cases belong to the automated fake-service
suite. If Spotify naturally throttles, stop requests and honor Retry-After; record
the event without probing it repeatedly.

## Troubleshooting

| Symptom | Distinguish and remedy |
| --- | --- |
| q2remote timeout | Laptop → M1 TCP 2302, wrong DHCP address, guest isolation, firmware dev helper absent, or its firewall. Authentication rejection means key mismatch, not reachability. |
| No relay TCP connection | M1 → server TCP 7443, listen address, host firewall, address in remote, Docker port publication or guest isolation. Check each direction independently. |
| TCP works, TLS/hello fails | Wrong certificate pin/token/data directory. Generate `remote` from the running bridge's data directory and transfer it securely. Without a pin, CA/name/clock validation applies. |
| Account says run login | OAuth absent/expired/refused or service using a different data directory. Run PKCE on the server; don't re-pair Soloist as a substitute. Redirect mismatch means the dashboard URI differs. |
| Pair/choose Q2 message; state none | Soloist not logged in, its WebSocket disconnected, or another Connect device active. Check paired session and choose/activate Q2 on phone. Never select a Web API device by name alone. |
| First Soloist login fails just after bridge startup | Check the bridge log for the Soloist WebSocket and a ready session. The initial M1 pass needed one orderly bridge restart before login succeeded. Stop that bridge and its Soloist before restarting with the same data directory; do not run two paired copies or erase credentials. Web API OAuth is separate. |
| `soloify` command missing in `nix develop` | The shell supplies dependencies and `PYTHONPATH`, not an installed console script. Use `python -m soloify.cli` from this checkout (or set `PYTHONPATH=bridge` outside the shell). |
| Artist only shows albums | Expected development-mode restriction on top tracks. Artist-album and search API requests use limit 10. Old UI may not paginate artists. |
| Playlist unavailable | Spotify restricts editorial/non-owned contents, or playlist is gone. An accessible owned playlist distinguishes this from a network failure. |
| Browse offline/timeout, audio still works | Web API connectivity, expired authorization, local WebSocket timeout, or slow upstream. Transactions are bounded; Retry is explicit and playback actions are not replayed. |
| Stale queue unavailable | Queue changed since display or between checks. Reopen it. Duplicate entries require position/entry identity, not a name match. |
| Old audio after play/seek | Record bridge/relay checksums and ordered `x Q`/control boundaries; do not swap binaries during the pass. Preserve evidence for a later fix. |

## Cleanup and restoration

Finish this before planning fixes, even after failures. Stop test device playback
and its restart loop. TERM to the launcher is the normal graceful stop; the shared
stop test above also verifies the forced path. Check children and ALSA are gone.

```sh
python "$Q2REMOTE" sh 'if [ -f /tmp/q2-librespot ]; then kill $(cat /tmp/q2-librespot); fi'
python "$Q2REMOTE" sh '
set -e
b=/mnt/mmc/.soloify-step4-backup
cd /mnt/mmc/.spotify
for f in librespot run aplay.sh remote q2-soloify; do
  if [ -f "$b/$f" ]; then cp -p "$b/$f" "$f"; fi
done
if [ -f "$b/absent" ]; then
  while read -r f; do
    case "$f" in librespot|run|aplay.sh|remote|q2-soloify) rm -f "$f";; *) exit 1;; esac
  done < "$b/absent"
fi
sha256sum -c "$b/original.sha256"
sync
'
kill "$BRIDGE_PID"
wait "$BRIDGE_PID"
```

Expected: every original checksum OK, original modes restored, originally absent
files absent. Verify the restored backend launches normally, then stop it if that
was the initial condition. Leave the on-card backup available with its manifest.
Remove RAM diagnostic helpers and temporary `remote` copies. Preserve the user's
existing OAuth/Soloist/bridge credentials; remove only disposable test credentials
or an isolated test data directory created for this pass. Remove the local generated
`$EVIDENCE/remote` securely from the evidence set. Restore screen timeout, standby,
volume, output selection and original shuffle/repeat through UI/phone. Step 3's
values were Screen Off 1 min (`LIGHTTIME=3`), standby Off, volume 20; use the values
recorded at the start of this pass, not these historical defaults.

If this pass opened NixOS TCP 7443, close that temporary rule:

```sh
sudo nixos-firewall-tool close tcp 7443
```

Remove only firewall rules/processes introduced for the pass; `soloify-stall` cleans
its own table. Verify no test bridge, Soloist, relay, decoder or player remains and
ALSA is released. Confirm no competing copied Soloist session was left running.

## Results template

```text
Date/time and tester:
Installed firmware/version, UI route, screenshot:
q2-soloify revision / q2-pod revision:
Soloist version / rootfs checksum:
Host and installed relay SHA-256 (must match):
M1 address / bridge LAN address / ports:
OAuth complete / Soloist paired / null sink verified:
Original screen timeout / standby / volume / output / shuffle / repeat:
Backup directory / original manifest / absent files:

Check ID | pass/fail/deferred | observed result | evidence | follow-up issue
F01 ... F17 (one row per check; split each library/search/repeat variant)
I01 ... I08 (UI routes and direct command checks recorded separately)

Restoration:
Original checksums all OK:
Originally absent files removed:
Settings restored (actual values):
Processes stopped and ALSA released:
Temporary credentials/config/firewall rules removed:
Original backend verified:
Deferred routes and reason:
Follow-up issues (plan only after pass and restoration):
```
