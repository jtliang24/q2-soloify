# Step 0 runbook: macOS and Windows

For the MacBook Air (Apple Silicon), Windows 11 x86_64 and Windows 11 ARM. Each run checks that Soloist plays into the container's sink, and finds one way to pair. Results go in [spikes.md](spikes.md); paste the report block at the end back into the session.

What the NixOS runs showed ([spikes.md](spikes.md)): a container on Docker's default NAT network can't be paired, but once paired it plays and the phone controls it through Spotify's cloud. A session copied from another Linux machine also works. So each host only needs one pairing. After that the container runs on the default network.

## On the laptop, before you start

```sh
cd ~/Projects/q2-soloify
git bundle create /tmp/q2-soloify.bundle --all
# fallback 4: the session the laptop's container paired (B3); private, handle like the key
docker run --rm -v q2-soloify_soloify-data:/data -v /tmp:/out debian:trixie-slim \
    tar -C /data -czf /out/b3-state.tgz soloist-state
```

Copy `q2-soloify.bundle`, `soloist_api.key` and `b3-state.tgz` to each machine. Delete `b3-state.tgz` from each when done. Never run two copies of one session at once: they share a device id. Keep the laptop's container stopped while another machine uses `b3-state.tgz`.

## Prerequisites (each machine)

- Docker Desktop 4.34 or later. For fallback 1, turn on Settings → Resources → Network → **Enable host networking**, then Apply and restart.
- Mac only: [OrbStack](https://orbstack.dev) for fallback 2 (install it when you reach that fallback).
- Windows: Git for Windows (for `git clone` of the bundle).
- The phone on the same Wi-Fi as the machine. Set the machine never to sleep during the test.
- When macOS or Windows asks whether Docker or `soloist` may accept incoming connections, allow it (Windows: private networks), and note that it asked.

## Setup

macOS (Terminal, zsh):

```sh
git clone ~/Downloads/q2-soloify.bundle q2-soloify && cd q2-soloify
cp ~/Downloads/soloist_api.key . && chmod 600 soloist_api.key
dc()  { docker compose -f compose.yaml -f compose.dev.yaml "$@"; }
dch() { docker compose -f compose.yaml -f compose.dev.yaml -f compose.linux.yaml "$@"; }
sw_vers -productVersion; docker version --format '{{.Server.Version}}'
dc up -d --build --wait
dc exec -u soloify soloify sh -c 'uname -m; soloist --version'
```

Windows (PowerShell):

```powershell
git clone $HOME\Downloads\q2-soloify.bundle q2-soloify; cd q2-soloify
Copy-Item $HOME\Downloads\soloist_api.key .
function dc  { docker compose -f compose.yaml -f compose.dev.yaml @args }
function dch { docker compose -f compose.yaml -f compose.dev.yaml -f compose.linux.yaml @args }
[Environment]::OSVersion.Version; $env:PROCESSOR_ARCHITECTURE; docker version --format '{{.Server.Version}}'
dc up -d --build --wait
dc exec -u soloify soloify sh -c 'uname -m; soloist --version'
```

The repo holds only the x86_64 tarball, so on the Mac and on Windows ARM the entrypoint downloads the arm64 Linux build (`dc logs` shows "fetching Soloist"). `--wait` returns once Soloist is installed and PulseAudio answers, so the `exec` that follows finds both. `uname -m` should say `aarch64` there and `x86_64` on Windows x86.

## Pairing: try in order, stop at the first that works

Each pairing run waits for the phone. In Spotify's device picker, choose **Q2**. If Q2 doesn't appear within two minutes, stop it with Ctrl-C, write down "not listed", and go to the next fallback. A pairing that worked prints `pairing complete, credentials stored` and exits.

To keep only the lines worth reporting from a log: on the Mac, `grep -E 'ready|ZeroConf|Reachability|getInfo|addUser|pairing complete|logged in|Using (Pulse|Pipe)' FILE`; in PowerShell, `Select-String -Pattern 'ready|ZeroConf|Reachability|getInfo|addUser|pairing complete|logged in|Using (Pulse|Pipe)' FILE`.

**1. Host networking (Docker Desktop).**

```sh
dch up -d --wait
dch exec -T -u soloify soloify soloist-run --pair -v > pair1.log 2>&1   # PowerShell: same line
```

While it waits, on the Mac only, run `dns-sd -B _spotify-connect._tcp` in a second window: does an entry named like a UUID appear? Ctrl-C it after 10 s.

**2. OrbStack (Mac only).** Quit Docker Desktop, start OrbStack, then:

```sh
docker context use orbstack
dch up -d --build --wait
dch exec -T -u soloify soloify soloist-run --pair -v > pair2.log 2>&1
```

If this one pairs, stay on OrbStack for the play check. Otherwise switch back with `docker context use desktop-linux` and restart Docker Desktop.

**3. Pair the native Soloist, then copy its session into the container.**

macOS:

```sh
tools/soloist-fetch data/native/soloist          # the osx_arm64 build
PATH="$PWD/data/native/soloist:$PATH" SOLOIFY_DATA=data/native tools/soloist-run --pair -v > pair3.log 2>&1
src=data/native/soloist-state
```

Windows (on ARM, the x86_64 build runs under Windows' emulation):

```powershell
New-Item -ItemType Directory -Force data\native | Out-Null
curl.exe -fL -o data\native\soloist.zip https://soloist-builds.spotifycdn.com/soloist_release_windows_x86_64.zip
Expand-Archive -Force data\native\soloist.zip data\native\soloist
$exe = (Get-ChildItem -Recurse data\native\soloist -Filter soloist.exe).FullName
& $exe -p -n Q2 -k (Get-Content soloist_api.key -Raw).Trim() -D data\native\soloist-state -v *> pair3.log
$src = "data\native\soloist-state"
```

Then copy (both systems; in PowerShell write `$src` the same way):

```sh
dc up -d --wait
dc exec soloify rm -rf /data/soloist-state
dc cp $src soloify:/data/soloist-state
dc exec soloify sh -c 'chown -R soloify:soloify /data/soloist-state && chmod -R go-rwx /data/soloist-state'
```

`-k` puts the key on the command line, so don't paste the command's echo anywhere.

**4. Copy the laptop's session.** Make sure the laptop's container is stopped.

```sh
tar -xzf ~/Downloads/b3-state.tgz        # Windows: tar -xzf $HOME\Downloads\b3-state.tgz (tar.exe is built in)
dc up -d --wait
dc exec soloify rm -rf /data/soloist-state
dc cp soloist-state soloify:/data/soloist-state
dc exec soloify sh -c 'chown -R soloify:soloify /data/soloist-state && chmod -R go-rwx /data/soloist-state'
```

## Play check (after whichever fallback paired)

On the default network, as it will run day to day. If you paired with `dch`, go back with `dc up -d --wait`; the volume keeps the session. In one window:

```sh
dc exec -T -u soloify soloify soloist-run --ws 127.0.0.1:0 -v > play.log 2>&1
```

In a second window (same `cd`, and define `dc` again):

```sh
dc exec -u soloify soloify sh -c 'D=/data/soloist-state; soloist ctl -D $D status; soloist ctl -D $D activate; sleep 2; soloist ctl -D $D play spotify:track:7wYCva1bI1AkHRQoVXhy8F; sleep 4; soloist ctl -D $D now; soloist ctl -D $D volume 100; sleep 1; soloify-sink level 10'
```

It passes when `status` says `logged in: yes`, `now` says `playing`, and `level` shows a `mean_volume` above -40 dB, while the machine's speakers stay silent. Then, on the phone, check that Q2 shows as the playing device and that pause and skip work from there. Stop it with Ctrl-C in the first window. `dc down` stops the container. `dc down -v` also deletes the stored session.

## Report (paste back)

```text
host:            (MacBook Air / Windows x86 / Windows ARM)
os, arch:        (sw_vers, or OSVersion + PROCESSOR_ARCHITECTURE)
docker:          (server version; Docker Desktop or OrbStack)
container:       (uname -m; soloist --version)
firewall prompt: (none / which program, allowed)
fallback 1:      (paired / not listed / skipped) + dns-sd seen? (Mac)
fallback 2:      (Mac: paired / not listed / skipped)
fallback 3:      (paired natively? session works in the container?)
fallback 4:      (session works in the container?)
play check:      (status, now, level lines; speakers silent?; phone sees Q2, pause and skip work?)
log lines:       (the filtered lines from the pair and play logs that worked)
```
