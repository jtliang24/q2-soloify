# Step 4: browsing and laptop verification

Implemented on the laptop, 2026-10-28. **M1 installation and verification have not
been performed for step 4.** No firmware or card files were changed. The next
device pass follows [step4-runbook.md](step4-runbook.md), including restoration
before planning fixes. Step 3's historical hardware results remain in [step3.md](step3.md).

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

## Pending M1 pass

All step 4 hardware checks remain pending: menu/UI routes, page navigation,
artwork and packet-clock position through the M1 sink, actual audio controls,
context/duplicate/queue selection, browsing during playback, Retry/reconnect,
local-music handover, idle stop and shared/UI stop routes. The installed older hub
may lack artist continuation, scrub, Stop Streaming Services or Rockbox routes;
record them as deferred individually. No firmware flash is required. Follow the
[home-network runbook](step4-runbook.md), complete the pass and restore original
files/settings before planning fixes.
