# Development notes

How to build, test and run q2-soloify from a checkout. Setup for using it is in the [README](../README.md).

## Nix shell

```sh
nix develop
tools/soloist-fetch --cache .   # data/soloist/ from ./soloist_release_<arch>.tar.gz, or a download
soloist --version
python -m pytest
ruff check . && ruff format --check .
python relay/build.py --rootfs ../q2-pod/build-m1/stock.squashfs   # build/relay/q2-soloify
```

`relay/build.py --host` builds the relay for this machine, which the tests run against an in-process bridge. `--rootfs` builds it for the devices, linked like q2-pod's helpers against a stock rootfs's glibc 2.28 and OpenSSL 1.1.1f. The relay's sources are listed in `relay/build.py`: besides the session code, `manage.c` (the Soloify app's page), `swap.c` (the Spotify slot's swap) and `pair.c` (pairing by code and the away record). `tests/conftest.py` gives every test's relay a safe environment: a relay with no `remote` pairs by itself, so `SOLOIFY_PAIR_BROADCAST=127.0.0.1` and `SOLOIFY_PAIR_UDP_PORT=9` keep its discovery on loopback, where nothing answers, instead of the real network's broadcast address, and `SOLOIFY_AWAY_COUNT` moves the WAN failure count (`/tmp/q2-soloify.away` on the device) into the test's own directory. A test that pairs sets its own. `card/run`'s tests use `SOLOIFY_ROOT` (a fake `/proc` and `/sys` for the device check), `SOLOIFY_Q2POD`, `SOLOIFY_RELAY_BASE` and `SOLOIFY_MANAGE`. `tests/test_relay_mips.py` runs that build under `qemu-mipsel`, decoding with the stock ffmpeg, when it finds q2-pod's `build-m1/stock.squashfs` beside this repo, or `$SOLOIFY_ROOTFS`.

`python -m soloify.cli login --client-id ID` (or `SPOTIFY_CLIENT_ID`) authorizes browsing through PKCE. Register exactly `http://127.0.0.1:8888/callback` in the Spotify dashboard and open the printed URL on the server laptop. Tokens are stored in `data/oauth.json` with mode 0600 and loaded by `serve`. Soloist pairing is separate; missing Web API authorization leaves Connect playback running.

`python -m soloify.cli serve` fetches Soloist when `data/soloist/` has none (and again before it expires), runs it into the `q2_soloify` null sink with its WebSocket on loopback, and serves its audio, state, covers and controls on 7443; `--source test` serves a beep each second instead, without Soloist. `tests/fixtures/soloist_ws.jsonl` is a recorded Soloist session, which `tests/soloist_fake.py` replays ([docs/step2.md](step2.md)). `python -m soloify.cli remote --lan ADDRESS --wan NAME` prints the card's `.soloify/remote`. `python -m soloify.cli pair-device` waits for a device's code and pairs it (add `--no-discovery` to skip the UDP 7444 listener). The wire, pairing included, is in [docs/wire.md](wire.md).

The Nix shell supplies dependencies and `PYTHONPATH`; invoke the bridge with `python -m soloify.cli` from this checkout. An installed package also provides the `soloify` console command.

The shell has Python with the bridge's dependencies (aiohttp, cryptography, pytest), ffmpeg, `pactl`/`parec`, PipeWire's tools, clang and ld.lld for the relay (q2-pod's MIPS helper recipe), `qemu-mipsel`, hadolint and docker-compose.

On Linux, `soloist` in the shell runs the fetched binary through Nix's loader, with `libatomic`, `libpulse`, `libpipewire` and ffmpeg's libraries. Soloist reads PipeWire's `client.conf` only from standard paths, which NixOS lacks, so without `/etc/pipewire/client.conf` the launcher points `PIPEWIRE_CONFIG_DIR` at the package's; otherwise Soloist falls back to PulseAudio. `SOLOIST_BIN` overrides the binary.

`tools/soloist-run [OPTIONS]` runs Soloist as the bridge will: the name `soloify` (`SOLOIFY_NAME`), state and cache under `data/` (`SOLOIFY_DATA`) and the key from `soloist_api.key` (`SOLOIST_API_KEY_FILE`), plus extra options such as `--pair`, `-d q2_soloify` or `--ws 127.0.0.1:0`. Soloist takes its key only as `-k`, so `ps` shows it while it runs. Keep the key file at mode 0600.
