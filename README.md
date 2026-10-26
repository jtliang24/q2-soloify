# q2-soloify

Soloify plays Spotify on Q2 Pod devices (Shanling Q2, M1 Plus) through Spotify's own [Soloist](https://developer.spotify.com/documentation/soloist), for accounts librespot can't play. Soloist runs on a computer at home; the bridge here sends its audio to a relay on the device's card. See [PLAN.md](PLAN.md). Nothing plays yet: this is the development setup.

Spotify's terms forbid redistributing Soloist, so this repo never holds it. Each machine fetches its own build, and each user makes their own API key (with Spotify Premium). `.gitignore` keeps `soloist_release_*` and `*.key` out of git.

## Development (Nix)

```sh
nix develop
tools/soloist-fetch --cache .   # data/soloist/ from ./soloist_release_<arch>.tar.gz, or a download
soloist --version
python -m pytest
ruff check . && ruff format --check .
```

The shell has Python with the bridge's dependencies (aiohttp, cryptography, pytest), ffmpeg, `pactl`/`parec`, PipeWire's tools, clang and ld.lld for the relay (q2-pod's MIPS helper recipe), `qemu-mipsel`, hadolint and docker-compose.

On Linux, `soloist` in the shell runs the fetched binary through Nix's loader, with `libatomic`, `libpulse`, `libpipewire` and ffmpeg's libraries. Soloist reads PipeWire's `client.conf` only from standard paths, which NixOS lacks, so without `/etc/pipewire/client.conf` the launcher points `PIPEWIRE_CONFIG_DIR` at the package's; otherwise Soloist falls back to PulseAudio. `SOLOIST_BIN` overrides the binary.

`tools/soloist-run [OPTIONS]` runs Soloist as the bridge will: the name `Q2` (`SOLOIFY_NAME`), state and cache under `data/` (`SOLOIFY_DATA`) and the key from `soloist_api.key` (`SOLOIST_API_KEY_FILE`), plus extra options such as `--pair`, `-d q2_soloify` or `--ws 127.0.0.1:0`. Soloist takes its key only as `-k`, so `ps` shows it while it runs. Keep the key file at mode 0600.

## Container (Docker or Podman)

For macOS and Windows, whose Soloist plays only to the system's default output, and for Linux hosts that prefer it. The image runs Spotify's Linux build (fetched into the data volume at start) into a private PulseAudio null sink, `q2_soloify`; nothing reaches the host's audio. The key goes in as the compose secret `soloist_api_key`, read from `./soloist_api.key`.

```sh
docker compose up -d                                      # the bridge (soloify serve), port 7443
docker compose -f compose.yaml -f compose.linux.yaml up -d  # Linux: host network, for pairing
docker compose -f compose.yaml -f compose.dev.yaml up -d    # dev: ./ mounted at /import, kept idle
docker compose -f compose.yaml -f compose.dev.yaml exec -u soloify soloify sh
```

`podman compose` takes the same files. The dev override installs the repo's `soloist_release_<arch>.tar.gz` instead of downloading, and runs `sleep infinity` until the bridge exists. Shells go in as `-u soloify`: the entrypoint starts as root only to hand the secret and the volume to that user. `hadolint container/Containerfile` lints the image.

Pairing needs the phone's Spotify app on Soloist's LAN. Under Docker Desktop's VM on macOS and Windows that may not work; PLAN.md step 0 lists the fallbacks to try.

## Layout

| Path | What |
| --- | --- |
| `bridge/soloify/` | the bridge (Python); only `soloify --version` so far |
| `tests/` | its tests |
| `tools/soloist-fetch`, `tools/soloist-run` | fetch and run Soloist (POSIX sh, also used in the image) |
| `container/` | `Containerfile`, `entrypoint`, `pulse.pa` |
| `compose*.yaml` | the service; Linux host networking; development |
| `flake.nix` | the dev shell and the `soloist` launcher, on q2-pod's nixpkgs revision |
