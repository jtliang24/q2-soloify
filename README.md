# q2-soloify

Soloify plays Spotify on Q2 Pod devices (Shanling Q2, M1 Plus) through Spotify's own [Soloist](https://developer.spotify.com/documentation/soloist), for accounts librespot can't play. Soloist can't run on the device, so it runs on a computer at home. The **bridge** (this repo's Python server) runs Soloist, encodes its audio as Opus and serves it over TLS. The **relay** on the device's card dials the bridge, decodes the audio and plays it, keeping the Spotify page's controls, browsing and Now Playing working. The design is in [PLAN.md](PLAN.md) and the protocol in [docs/wire.md](docs/wire.md).

```
home computer                                     M1 Plus
phone's Spotify app ──cloud──► Soloist ─► bridge ══ TLS, port 7443 ══► relay (.soloify/q2-soloify) ─► headphones / USB
                                           ▲                                   │
                                           └── browsing, queue, controls ◄─────┘
```

## Set up the bridge and connect the M1

The first four steps are on the computer that will run the bridge (Linux, macOS or Windows), the last on the M1. The computer and the M1 must be on the same LAN without client isolation (a guest SSID can block them), and the computer has to stay on while you listen.

### What you need

- A Spotify **Premium** account, and the Spotify app on a phone or computer on the same LAN, for pairing.
- Your own Soloist API key, saved as `soloist_api.key` in your checkout with mode 0600 (`chmod 600 soloist_api.key`). Spotify's terms forbid redistributing Soloist, so each machine fetches its own build and each user has their own key. Builds expire 90 days after their build date; the bridge replaces its copy before then.
- A Spotify developer app, for browsing: register exactly `http://127.0.0.1:8888/callback` as its redirect URI (neither `localhost` nor a trailing slash matches) and note its client ID.
- An M1 Plus running firmware with the Soloify row (q2-pod's `step5-soloify` branch). Apps can't install Soloify yet, because there's no release to install ([PLAN.md](PLAN.md) step 6), so you copy its files to the card yourself. Without the firmware, the relay can stand in for `.spotify/librespot` ([docs/step4-runbook.md](docs/step4-runbook.md) does that on an older build).
- To build the relay for the device, a Linux machine (or [Nix](#nix)) with `clang`, `lld` (`ld.lld`) and `squashfs-tools` (`unsquashfs`), and a stock M1 rootfs: `../q2-pod/build-m1/stock.squashfs` from a q2-pod checkout beside this one.

### 1. Run the bridge

Pick the route for your computer:

| Computer | Route |
| --- | --- |
| Linux (any distribution) | [native](#linux-native), or the [container](#container-docker-or-podman) if you'd rather not install the dependencies |
| macOS, Windows | the [container](#macos-and-windows-docker-desktop): their Soloist plays only to the system's default output |
| NixOS, or Linux with Nix | [`nix develop`](#nix) |

#### Linux (native)

Install the dependencies with your package manager:

| Need | Debian / Ubuntu | Fedora | Arch |
| --- | --- | --- | --- |
| Python 3.11+ with `venv` | `python3 python3-venv` | `python3` | `python` |
| `ffmpeg` (encodes the audio) | `ffmpeg` | `ffmpeg` (RPM Fusion) or `ffmpeg-free` | `ffmpeg` |
| `pactl`, `parec`, and PipeWire's or PulseAudio's server | `pulseaudio-utils` and `pipewire-pulse` or `pulseaudio` | `pulseaudio-utils` and `pipewire-pulseaudio` or `pulseaudio` | `libpulse` and `pipewire-pulse` or `pulseaudio` |
| `libatomic` (Soloist links it) | `libatomic1` | `libatomic` | `gcc-libs` |
| `curl` | `curl` | `curl` | `curl` |

Soloist also dlopens `libpulse` and ffmpeg's libraries, which the packages above pull in. In this checkout, install the bridge into a virtual environment:

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e .                                          # aiohttp and cryptography; adds the `soloify` command
python -m soloify.cli login --client-id YOUR_CLIENT_ID    # then open the printed URL on this computer
```

`login` stores a Web API token in `data/oauth.json` (mode 0600). Without it, Connect playback still works but the device's browsing, search and queue don't.

Pair Soloist with your account, once:

```sh
tools/soloist-fetch --cache .      # data/soloist/, from ./soloist_release_<arch>.tar.gz or a download
tools/soloist-run --pair -v
```

In Spotify on your phone (or the desktop app on this computer), open the device picker and choose **Q2**. The log ends `pairing complete, credentials stored`. A host firewall hides Soloist from the phone, so open the ZeroConf port the log names for the pairing, then close it again:

```sh
sudo ufw allow PORT/tcp                                                         # ufw; `sudo ufw delete allow PORT/tcp` afterwards
sudo firewall-cmd --add-port=PORT/tcp                                           # firewalld; lasts until reload
sudo nft add rule inet filter input tcp dport PORT accept                       # plain nftables; adjust the table
```

Soloist also announces itself over mDNS (UDP 5353), so allow that too if the phone can't see **Q2**. Soloist's session lives in `data/soloist-state/`; never run two copies of it at once.

Open the bridge's port to the M1 (TCP 7443 from the M1's subnet), then serve:

```sh
sudo ufw allow from 192.168.1.0/24 to any port 7443 proto tcp     # or firewalld / nftables / your router's equivalent
python -m soloify.cli serve
```

The log shows `listening on 7443, pin sha256=…`, and the bridge keeps running Soloist. Use `--name` to change the Connect name (default `Q2`), `--bitrate 96|128|160` for the default quality, and `--source test` to serve a beep instead of Soloist when you only want to check the connection.

#### Nix

On NixOS, or any Linux with Nix, `nix develop` supplies every dependency above:

```sh
nix develop
python -m soloify.cli login --client-id YOUR_CLIENT_ID
tools/soloist-fetch --cache . && tools/soloist-run --pair -v
```

On NixOS, open the ZeroConf port with `sudo nixos-firewall-tool open tcp PORT` (then `sudo nixos-firewall-tool reset`), and the bridge's with `sudo nixos-firewall-tool open tcp 7443`. Then `python -m soloify.cli serve`.

#### macOS and Windows (Docker Desktop)

The container runs Spotify's Linux build of Soloist into a private PulseAudio sink, so nothing depends on your system's audio. You need:

- **Docker Desktop**, running. On Windows it uses the WSL 2 backend (enable WSL 2 when the installer asks, and keep "Use WSL 2 based engine" ticked). On macOS, either Apple silicon or Intel works.
- This checkout, and `soloist_api.key` in it (mode 0600 on macOS; on Windows, just keep it out of shared folders). Run the commands below from the checkout in Terminal (macOS), or in PowerShell or a WSL shell (Windows).
- Python 3.11+ on the host, only for the one-off `login`.

**1. Build and start the bridge**

```sh
docker compose up -d --build --wait
docker compose logs -f soloify        # the first start fetches Soloist; look for "listening on 7443, pin sha256=…"
```

The first start downloads Soloist into the `soloify-data` volume (mounted at `/data`), so it needs internet and a minute. `restart: unless-stopped` brings the bridge back after a reboot once Docker Desktop is running. Add Docker Desktop to your login items so it does.

**2. Pair Soloist (once)**

Docker Desktop runs containers in a VM, so the phone can't discover Soloist inside it. Pair a native Soloist on the host instead, then copy its session into the volume. This needs a native Soloist for the host, which exists for macOS on Apple silicon, and for Windows through WSL 2 (where the Linux build runs):

```sh
# on the host (macOS, or inside WSL 2 on Windows), in the checkout
tools/soloist-fetch --cache .
tools/soloist-run --pair -v        # choose Q2 in the Spotify app; wait for "pairing complete"

docker compose cp data/soloist-state soloify:/data/soloist-state
docker compose exec -u root soloify chown -R soloify:soloify /data/soloist-state
docker compose restart soloify
```

If native pairing isn't possible for your machine, [docs/step0-runbook.md](docs/step0-runbook.md) tries the alternatives and [docs/spikes.md](docs/spikes.md) has the results.

**3. Authorize browsing (optional, recommended)**

```sh
python3 -m venv .venv && . .venv/bin/activate     # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -e .
python -m soloify.cli login --client-id YOUR_CLIENT_ID     # open the printed URL on this computer

docker compose cp data/oauth.json soloify:/data/oauth.json
docker compose exec -u root soloify sh -c 'chown soloify:soloify /data/oauth.json && chmod 600 /data/oauth.json'
```

**4. Let the M1 reach it**

Docker Desktop publishes port 7443 on the host's LAN address, so the M1 connects to your computer's address, never to the container's.

- Find the address: macOS `ipconfig getifaddr en0` (Wi-Fi; `en1` or the Ethernet interface otherwise); Windows `ipconfig` and read the IPv4 address of the active adapter. Give the computer a fixed address or a router DHCP reservation, because `remote` holds it.
- macOS: if the application firewall is on, allow incoming connections for Docker when it asks (System Settings → Network → Firewall).
- Windows: allow the port on the private network, in an administrator PowerShell: `New-NetFirewallRule -DisplayName "Soloify bridge" -Direction Inbound -Protocol TCP -LocalPort 7443 -Profile Private -Action Allow`. Set the Wi-Fi network's profile to Private.
- Don't also run `soloify serve` natively while the container holds port 7443.

Check from another computer on the LAN: `nc -vz HOST_ADDRESS 7443` (or `Test-NetConnection HOST_ADDRESS -Port 7443` in PowerShell).

Update later with `git pull && docker compose up -d --build`. Stop with `docker compose down`; the volume, and with it the pairing, survives. `docker compose down -v` deletes it.

### 2. Make the card's `remote` file

`remote` tells the relay where the bridge is and holds its token and certificate pin. Use the computer's LAN address, never `127.0.0.1`:

```sh
umask 077
python -m soloify.cli remote --lan 192.168.1.100 > remote
# container: docker compose exec -T -u soloify soloify soloify remote --lan 192.168.1.100 > remote
```

On Windows, run the container form from WSL, Git Bash or `cmd` (`cmd /c "docker compose exec -T ... > remote"`). PowerShell's `>` writes UTF-16 and the relay can't read it.

It prints one line per endpoint: `HOST PORT TOKEN sha256=PIN`. To listen away from home, add `--wan NAME` for the name your router forwards to this computer; the relay tries the LAN address first. The token is a credential, so keep `remote` out of shared logs and screenshots. [card/remote.sample](card/remote.sample) explains the format.

### 3. Build the relay

```sh
python relay/build.py --rootfs ../q2-pod/build-m1/stock.squashfs     # build/relay/q2-soloify
```

This needs `clang`, `ld.lld` and `unsquashfs` on the PATH (`apt install clang lld squashfs-tools`, `dnf install clang lld squashfs-tools`, `pacman -S clang lld squashfs-tools`, or `nix develop`). On macOS or Windows, build on a Linux machine or in WSL 2; the container image doesn't include the toolchain.

Install the MIPS build, not the host one (`--host`).

### 4. Copy the files to the card

Make `.soloify/` at the root of the M1's card (`/mnt/mmc/.soloify` on the device) with:

| File | From |
| --- | --- |
| `q2-soloify` | `build/relay/q2-soloify` |
| `run`, `aplay.sh` | `card/` |
| `remote` | step 2 |

Make `q2-soloify`, `run` and `aplay.sh` executable (`chmod 755`) and `remote` private (`chmod 600`). Put the card in a computer, or push the files over Wi-Fi with q2-pod's `tools/q2remote.py push`. [card/README.md](card/README.md) describes each file.

### 5. Connect

1. Check the bridge is reachable: on the M1, `telnet BRIDGE_LAN_ADDRESS 7443` should connect (it doesn't speak TLS, so it ends at once).
2. Open **Streaming → Soloify** on the M1, which follows Spotify's row. If the row is missing, `.soloify/q2-soloify` isn't on the card or Soloify is switched off in Streaming Configuration.
3. The page loads the Spotify menu. If Soloist is logged in but idle, select **Q2** in the Spotify app on your phone, then play from the device's playlists, search or queue.

`.soloify/spot.log` records the relay. A working start reads `state and controls ready`, then `connected to ADDRESS:7443, buffer 1000 ms`.

Only one of Spotify and Soloify runs at a time. Opening one stops the other, and Stop Streaming Services stops either.

### Settings

Soloify Delay (Auto, 1, 2, 4 or 8 s) and Soloify Quality (96, 128 or 160 kbit/s) sit in Streaming Configuration. They write `.soloify/.delay` and `.soloify/.quality`, which the relay reads at each connection. Auto is a 1 s buffer on the LAN and 4 s beyond it. A longer delay means fewer dropouts on poor Wi-Fi, and a change stops a running Soloify so the next open uses it. To unpair, delete `.soloify/remote`.

### If it doesn't work

| Symptom | Likely cause |
| --- | --- |
| "Copy remote to .soloify" | `.soloify/remote` is missing; Retry re-reads it |
| "Soloify isn't running" or "didn't answer" | the launcher failed; read `.soloify/spot.log`, and check `q2-soloify` and `run` are executable |
| `telnet` can't connect | the firewall (host or Windows/macOS) blocks 7443, or the M1 is on a different network or a guest SSID |
| The relay connects, then drops at once | the token or `sha256=` pin doesn't match this bridge's `data/`; remake `remote` |
| Menus load but nothing plays | Soloist isn't logged in, or its build has expired (it exits with code 10): re-pair, or check the bridge log |
| Playback works but browsing says to log in | run `soloify login` on the server |

## Status

The bridge and relay carry audio, state, covers and controls, and browsing, search, queue and settings work through the existing Spotify page. Pause stops output at once and rewinds Soloist to the last heard position; resume refills a fresh buffer from that bookmark. Deploy the bridge and relay together (protocol version 2). The results so far:

- [Step 4](docs/step4.md): laptop validation, live Spotify checks and the M1 pass, with the [reliability follow-up](docs/step4-followup.md) and the repeatable [M1 runbook](docs/step4-runbook.md).
- [Step 5](docs/step5.md): the firmware's Soloify row, Delay and Quality, and the M1 pass. Scrub through the UI still fails on the M1, as in [step 3](docs/step3.md).
- [Pause and rewind](docs/pause-rewind.md): implementation and M1 runbook, with [follow-up progress](docs/pause-rewind-followup.md). Physical verification is partial.

Spotify's terms forbid redistributing Soloist, so this repo never holds it. Each machine fetches its own build, and each user makes their own API key (with Spotify Premium). `.gitignore` keeps `soloist_release_*` and `*.key` out of git.

## Development

Building, testing and running from a checkout (the Nix shell, the relay build, `serve`, `login`, the Soloist launchers) is in [docs/development.md](docs/development.md).

## Container (Docker or Podman)

For macOS and Windows, whose Soloist plays only to the system's default output (the step-by-step setup is [above](#macos-and-windows-docker-desktop)), and for Linux hosts that prefer it. The image runs Spotify's Linux build (fetched into the data volume at start) into a private PulseAudio null sink, `q2_soloify`; nothing reaches the host's audio. The key goes in as the compose secret `soloist_api_key`, read from `./soloist_api.key`.

```sh
docker compose up -d                                      # the bridge (soloify serve), port 7443
docker compose -f compose.yaml -f compose.linux.yaml up -d  # Linux: host network, for pairing
docker compose -f compose.yaml -f compose.dev.yaml up -d    # dev: ./ mounted at /import, kept idle
docker compose -f compose.yaml -f compose.dev.yaml exec -u soloify soloify sh
```

`podman compose` takes the same files. The dev override installs the repo's `soloist_release_<arch>.tar.gz` instead of downloading, and runs `sleep infinity` instead of the bridge, so Soloist or `soloify serve` can be run by hand through `exec`. Shells go in as `-u soloify`: the entrypoint starts as root only to hand the secret and the volume to that user. `hadolint container/Containerfile` lints the image.

Pairing needs the phone's Spotify app on Soloist's LAN, but only once: a paired container plays on the default network, controlled through Spotify's cloud. Under Docker Desktop on macOS and Windows pair a native Soloist and copy its state in, as above; the first pairing may need a fallback; [docs/step0-runbook.md](docs/step0-runbook.md) tries them, and [docs/spikes.md](docs/spikes.md) has the results.

## Layout

| Path | What |
| --- | --- |
| `bridge/soloify/` | the bridge (Python): TLS listener, Soloist and its WebSocket, state and covers, audio |
| `relay/` | the card's relay (C) and its build |
| `card/` | the launcher `run`, `aplay.sh`, `remote.sample` and measurement helper `log.sh` |
| `tests/` | the bridge's and the relay's tests |
| `tools/soloist-fetch`, `tools/soloist-run`, `tools/soloify-sink` | fetch and run Soloist; the null sink and its level (POSIX sh, also used in the image) |
| `tools/soloify-stall` | step 1's forced stall: blocks the bridge's port for a while (root) |
| `container/` | `Containerfile`, `entrypoint`, `pulse.pa` |
| `compose*.yaml` | the service; Linux host networking; development |
| `flake.nix` | the optional Nix dev shell and the `soloist` launcher, on q2-pod's nixpkgs revision |
