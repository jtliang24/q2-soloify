# Step 4: browsing and laptop verification

Implemented on the laptop, 2026-10-28. The M1 pass ran the same evening following
[step4-runbook.md](step4-runbook.md) and its results are [below](#m1-pass-results). No
firmware was flashed; card files were temporarily replaced and restored. Step 3's
historical hardware results remain in [step3.md](step3.md).

## Implementation

`soloify --data data login --client-id ID` (or `SPOTIFY_CLIENT_ID`) prints a PKCE
URL and binds only `127.0.0.1:8888/callback`. That exact HTTP URI must be registered
in the Spotify dashboard. State is checked, denial and timeout are handled, and
callback query strings are excluded from access logs. Client ID, access/refresh
tokens and expiry are atomically saved in `data/oauth.json`, mode 0600. `serve`
loads them automatically; missing OAuth gives an actionable server-login message
without stopping existing Soloist playback. No client secret is needed.

The Web API client uses asynchronous HTTP with four-second per-request deadlines,
serialized refresh and a single retry after 401. Concurrent requests share the
refreshed token. HTTP 401/403 map to `refused`, network failures to `offline`, and
429 to `throttled`. Retry-After starts a monotonic cooldown: requests fail promptly
until it expires, without sleeping in the reader or removing saved login.
Restricted playlist contents map to `unavailable`.

The bridge implements 17 advertised operations: account, capabilities, playlists,
playlist details, liked tracks, saved albums, followed artists, album and artist
details, four-category search, play, queue, skip, enqueue, settings, shuffle and
repeat. Sign-out/download operations give explanatory `unsupported` replies.
Q2SPOT/1 escaping, six-field rows, opaque cursors, 8 KiB requests, 64 KiB responses
and at most 50 rows are preserved. Display strings are truncated at UTF-8 character
boundaries; URI/cursor fields are not truncated. Oversized pages continue before
the first unsent row. Filtered unavailable/local tracks retain absolute context
positions, so duplicate playlist entries use the selected offset.

Playback uses the persisted Soloist Connect ID (`soloist-state/.device_id`) only
after matching it exactly in Web API devices. A matching display name is never
sufficient. Context requests use `context_uri` plus `offset.position`; standalone
tracks use a URI list. Liked Songs tries its account collection context. If Spotify
rejects that context with 400/403/404, the bridge plays the selected and following
playable tracks from the retained fetched page. That fallback stops at the page's
end (at most 50 tracks); its response message explains the bounded continuation.
The last 16 fetched Liked Songs pages are retained; an evicted selection requires
opening the page again.

Queue and settings use Soloist's local WebSocket. Queries and commands share one
pending transaction, with a three-second deadline; disconnect fails it, and a
timeout closes the socket so a late reply cannot satisfy the next request. Queue
skip checks position/URI, then re-fetches and checks URI and queue entry UID before
each `skip_next`. A phone can still change the queue after a check; Soloist has no
atomic skip-to-entry operation. Repeat explicitly disables the other repeat flag
before enabling the chosen mode.

The real C relay asynchronously forwards four client slots over authenticated TLS.
A u32 correlation ID precedes unchanged Q2SPOT/1 text in `q`/`r`, independently of
firmware request IDs. The bridge keeps browse handlers separate from its frame
reader. Browse work is bounded/cancelled on disconnect; actions are never replayed.
Browse playback changes share the control lock. Play/queue-skip drain/reset audio
and finish with the existing ordered boundary mechanism (`x Q`) before returning
the response. Invalid or cancelled-before-dispatch actions also release the relay
reset. The bridge deadline is 10 seconds; the relay expires a slot after 11 seconds
and allows 500 ms to deliver the timeout response. See [wire.md](wire.md).

## Spotify compatibility and live results

Compatibility follows Spotify's [PKCE flow](https://developer.spotify.com/documentation/web-api/tutorials/code-pkce-flow)
and [February 2026 changes](https://developer.spotify.com/documentation/web-api/references/changes/february-2026).
Playlist contents use `/playlists/{id}/items` and the new `item` field, with support
for older `track` response fields. Profile fields removed in development mode stay
blank; the bridge does not infer subscription status from the Soloist API key.
Artist pages show albums, with an explanation that top tracks are unavailable in
development mode; the removed top-tracks endpoint is not queried. Both search and
[artist albums](https://developer.spotify.com/documentation/web-api/reference/get-an-artists-albums)
currently cap `limit` at 10. Other library pages request up to 50.

The user completed browser authorization for the development app on this laptop.
The final opt-in pass used Soloist **1.3.10.16, build 1793016086 (20261026),
g19412df6ea**, and passed in **9.50 s**. Audio routing was verified from actual
PipeWire links: the native stream advertises itself as `Spotify`/`spotify`, and its
output connects only to `q2_soloify`. It need not appear among PulseAudio sink inputs.
Test playback was paused, original shuffle/repeat settings restored and the test
Soloist process stopped. Existing paired credentials and OAuth remain for normal
use; no temporary device installation exists to restore.

| Laptop live check | Result |
| --- | --- |
| Account/capabilities | Passed; 17 supported operations, no fabricated subscription |
| Playlists | 50 rows then 8 on the next page |
| Liked Songs | Two pages of 50 playable rows |
| Saved albums/followed artists | 34 albums, 5 artists |
| Album details | 10 tracks on the sampled album |
| Artist albums | 10 rows with continuation; initial limit-50 request exposed the smaller upstream limit, corrected and rechecked |
| Search tracks/albums/artists/playlists | Passed: 10/10/10/6 rows, all with continuation cursors |
| Accessible playlist contents | 50 rows with continuation |
| Editorial playlist contents | `unavailable`, with restricted-contents explanation |
| Soloist device | Persisted ID matched Web API devices exactly |
| Album context selection | Requested offset and selected track verified from Soloist state |
| Liked Songs context selection | Native context accepted; selected track verified, no fallback needed on this account |
| Enqueue/queue selection | Passed; 50 upcoming rows, selected queue track verified |
| Stale queue | `unavailable` with refresh instruction, no skip sent |
| Shuffle and repeat off/context/track | Passed; options events confirmed each transition |
| Null-sink routing/restoration | Passed; original shuffle/repeat restored, playback paused and process stopped |
| Duplicate playlist selection against Spotify | Deferred live: no duplicate fixture selected; offset selection is covered by automated tests |
| Liked URI-list fallback against Spotify | Not needed live; rejection and bounded fallback covered by automated tests |

The opt-in test writes a status-only summary to `build/step4-live.json`; raw Soloist
logs in `build/step4-live.log` include private identifiers and must be redacted
before sharing. Credentials are not included in either test output deliberately.
To repeat, stop competing Soloist/bridge processes first:

```sh
SOLOIFY_LIVE=1 nix develop -c python -m pytest tests/test_live.py -s -q
```

Do not provoke live 401/429 failures. Those cases are injected in the local suite.

## Automated laptop validation

The suite uses loopback fake OAuth/Web API services and Soloist WebSockets, the
actual host C relay over TLS, and the MIPS relay under QEMU with stock libraries
and ffmpeg. It covers successful PKCE, incorrect state, denial/timeout, token
permissions, refresh/concurrent expiry, 401/403/404/429 cooldowns, network timeout,
malformed JSON, all operations and search categories, cursors, Unicode/escaping,
page size reduction, unavailable tracks, original duplicate positions, restricted
playlists, stale queues, all repeat transitions and bounded Liked Songs fallback.

Relay tests exercise four concurrent clients using the same firmware request ID,
reverse-order responses, late replies after slot reuse, an 11-second timeout,
disconnect/reconnect without replay, and slow browsing while audio acknowledgements
and ordinary controls continue. Invalid browse play and cancellation before the
control lock release the audio reset without dispatching an action. WebSocket tests
check response serialization, unrelated events, error/disconnect and timeout
reconnection without replay. MIPS/QEMU checks both audio decoding and browse text.

The final full run passed **106 tests**, with **1 opt-in live test skipped**, in
**74.58 s**, including both MIPS/QEMU tests. The separate opt-in live pass succeeded
as recorded above. Ruff lint/format checks and `git diff --check` passed. A final
focused browsing check also covers a null track URI being filtered without losing
original positions.

The verified MIPS relay is `build/relay/q2-soloify`, SHA-256:

```text
16bf4b30b045945f23408fa395828ff613cc17938452b613eb39f6ce60f041a9
```

One intermediate full run hit the existing launcher test's socket-cleanup timing
assertion; its focused rerun and two subsequent complete runs passed. No launcher
change was made for step 4.

```sh
nix develop -c python -m pytest -q
nix develop -c ruff check .
nix develop -c ruff format --check .
git diff --check
nix develop -c python relay/build.py --rootfs ../q2-pod/build-m1/stock.squashfs
```

## M1 pass results

Run 2026-10-28 on the home network, laptop wired and the M1 on Wi-Fi in the same
subnet, with the laptop firewall open for TCP 7443. Soloify revision `1e9fe94`, q2-pod
`b504ee0`, Soloist 1.3.10.16 (build 1793016086), relay SHA-256 `16bf4b30…41a9` (host and
installed copy matched). Firmware: Buildroot 2020.02.1 build with the older hub UI, so
Streaming → Spotify was the route (`.spotify/` temporarily holding the relay as
`librespot`, `run`, `aplay.sh` and a temporary `remote`; the originals were backed up
and checksummed first). The user operated the M1; screenshots and device state came from
q2remote and Spotify's Web API read-only. Raw logs and screenshots stay private in
`build/step4-m1/` (they contain identifiers).

| ID | Result | Notes |
| --- | --- | --- |
| F01 | Pass | Correct display name; no Premium shown because Spotify omits it for development-mode apps |
| F02 | Pass, limited | Sign Out asks to confirm, then shows "Sign out on the server" for about one second. The 17-operation list was not visible on the device; laptop tests are the evidence |
| F03 | Pass, partial | Playlists, Liked Songs, Albums, Artists (5) load; "Ø" and U+2019 pass through. Percent/tab/newline names not available: deferred |
| F04 | Pass, partial | Album order matched Spotify (rows 1–6 compared); artist shows albums with a More row. The restriction message is not displayed by this firmware |
| F05 | **Fail** | All four search categories give "Invalid operation arguments" |
| F06 | Pass, partial | Liked Songs page 2 and artist "More" continue from the right position. Playlists and search continuation untested |
| F07 | Deferred | No editorial playlist reachable: Spotify's `/me/playlists` returned 58 playlists, none Spotify-owned. Laptop live check covers `unavailable` |
| F08 | Pass | Selected middle row plays in playlist context; next track matches the list. Start delay about 1.5 s. Continuation beyond a page not exercised |
| F09 | Pass | Playlist with 142 tracks and three duplicate pairs: each copy resolved to its own neighbor (first attempt had tapped the earlier copy) |
| F10 | Pass | Native Liked Songs context accepted (`collection`); no URI-list fallback |
| F11 | Pass | Title, artist, album, artwork and advancing position agree. The state file's position is event-time only |
| F12 | Partial | Hardware pause stops and reaps the sink and releases ALSA; resume position is right, but resume has an audible blip (split-second audio, instant stop, restart). Same blip with a Connect pause from the phone. No on-screen pause control |
| F13 | Partial | Next is clean. Previous lands on the right track after a double press but gave about 5 s of silence (ring 0, underruns) and a stall. UI scrub/seek absent: deferred |
| F14 | Pass | Enqueue works from hold-Play/Pause and long press with no visible feedback; queue row 7 started with the six earlier entries skipped, in one audio cycle |
| F15 | Pass | Stale row gives `unavailable` with a refresh message (too long for one line), no skip; the refreshed first row then plays. Each rejected request cuts audio for about 0.8 s |
| F16 | Pass | Shuffle on reshuffled the queue and off restored it, with no audio interruption |
| F17 | Pass, partial | Off → All → One → Off verified; this firmware cannot go One → All, so that transition is deferred |
| I01 | Pass | About five minutes of browsing while playing: ring 620–700 ms, no growth, no new stalls |
| I02 | Pass | Bridge stopped: "Can't Reach the Spotify Bridge" with Retry; Retry recovered after restart with the same data directory, certificate and token. The 12 s limit was not timed |
| I03 | Pass | Play and enqueue submitted offline were rejected and not replayed on reconnect (empty queue and no player before and after) |
| I04 | Pass, with finding | 3 s block: bounded recovery but extra stalls within about 15 s. 15 s block: reconnect, resumed from the paused position, no accumulated delay |
| I05 | Pass | Local player took ALSA and Soloist paused; reopening Spotify handed the sink back |
| I06 | Pass | Launcher, relay, sink and state gone about 33 s after pause (about 17 s after leaving the page) |
| I07 | Partial | Stop Streaming Services works; no Rockbox route: deferred |
| I08 | Pass | Stop command removed launcher, relay, `ffmpeg` and `aplay` (transient zombies reaped within about 15 s). The installed firmware's definition is identical to the runbook's |

### Findings for fix planning

Plan these only now that the device is restored.

1. **F05 search.** The bridge answers the exact request the firmware code builds, and the laptop live search passed. A missing or empty term reproduces the message. A candidate cause is the firmware's search handler clearing its term when the box closes (`patch/spotify.c`, `searched()`); unverified. Capturing the request text needs request logging in the bridge or relay `-v`, which this pass did not change.
2. **Resume blip (F12)** from both the hardware button and Connect pause.
3. **Previous after a rapid double press (F13):** about 5 s of silence with relay underruns.
4. **Spontaneous stalls** (unacknowledged audio at the 1000 ms threshold, recovering in 0.6–1.8 s) during ordinary playback, and re-stalls after a link block (I04). An earlier theory that screenshot traffic caused them was wrong: stalls also occurred while nothing was being captured. The relay buffer ran at 340–700 ms against a 1000 ms target.
5. **Rejected requests interrupt audio** (stale queue, offline play) because the relay output is cleared before the action is validated.
6. **Reopening Spotify resumed a paused track** without a play press.
7. **Minor UX:** no enqueue acknowledgment, the stale-queue and Sign Out messages are cramped or fleeting, the artist restriction message is not shown on this firmware, a small gap follows U+2019 in titles, and volume read 13 after being set to 10 (cause unknown).
8. **Upstream:** editorial playlists do not appear in a development-mode account's playlist list.
9. **Runbook corrections:** the M1 has no `nc` (telnet worked for the TCP check), `soloify` was not on `PATH` in `nix develop` (the bridge ran with `PYTHONPATH=bridge`), and the first Soloist login after starting the bridge failed once and succeeded on restart.

### Restoration

The original `librespot`, `run` and `aplay.sh` pass `sha256sum -c`; `remote` and `q2-soloify` were absent originally and are absent again; the on-card backup `.soloify-step4-backup` is kept. The test bridge and Soloist are stopped, the generated `remote` was shredded, no stall table remains, ALSA is closed and no relay is running. The restored original backend launches (it reports "Spotify refused this account", the known audio-key limitation).
