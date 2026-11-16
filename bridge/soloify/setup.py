"""Guided installation, native pairing/session transfer, location setup and private card export.

Imported only after prerequisite selection. Setup never installs system prerequisites or changes
routers/firewalls. Progress contains no credentials; actual resources are checked on every run.
"""

import argparse
import asyncio
import contextlib
import csv
import getpass
import hashlib
import io
import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import time
import uuid
import webbrowser
import zipfile
from pathlib import Path

from . import __version__, server, soloist, spotify
from .setup_bootstrap import IMAGE, ask_yes, detect, release_asset, source_root

PORT = 7443
# Pairing stores account credentials under settings/Users/<id>-user/. An unpaired state
# directory still has .device_id, .lock, cache/ and crashpad/, so those prove nothing.
# PAIRED is the same check as inline source for the release image, which may predate this module.
PAIRED = "any(f.is_file() for f in (STATE/'settings'/'Users').glob('*/*'))"


def paired(state: Path) -> bool:
    return any(f.is_file() for f in (state / "settings/Users").glob("*/*"))


def path_input(value: str) -> Path:
    """Accept quoted Explorer/Finder paths and POSIX shell drag-and-drop escapes."""
    value = value.strip()
    if value.startswith("& "):
        value = value[2:].lstrip()  # PowerShell drag-and-drop
    if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    elif os.name != "nt":
        value = re.sub(r"\\([ \\()'\"])", r"\1", value)
    return Path(value).expanduser().resolve()


def restrict(path: Path) -> None:
    path.chmod(0o700 if path.is_dir() else 0o600)
    if os.name == "nt":
        # chmod on Windows does not set a private ACL. Grant only this user's SID explicitly.
        result = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True
        )
        sid = next(csv.reader([result.stdout.strip()]))[1]
        grant = f"*{sid}:" + ("(OI)(CI)F" if path.is_dir() else "F")
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", grant],
            capture_output=True,
            check=True,
        )


@contextlib.contextmanager
def installation_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    restrict(root)
    with (root / "setup.lock").open("a+b") as lock:
        restrict(root / "setup.lock")
        try:
            if os.name == "nt":
                import msvcrt

                lock.write(b"0")
                lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Another setup is running for this installation") from exc
        yield


def private_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "nt":
        restrict(path.parent)  # keep even the initial temporary file private on NTFS
    fd, name = tempfile.mkstemp(prefix=".setup-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
        restrict(Path(name))
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def port_value(value: str | int) -> int:
    try:
        port = int(value)
    except (ValueError, TypeError) as exc:
        raise ValueError("Use a TCP port from 1 to 65535") from exc
    if not 1 <= port <= 65535:
        raise ValueError("Use a TCP port from 1 to 65535")
    return port


def endpoint(value: str, *, public: bool = True) -> str:
    value = value.strip().lower()
    if not value or any(c in value for c in "/:@\\ \t\r\n?#"):
        raise ValueError("Use an IPv4 address or DNS hostname, without a URL scheme, path or port")
    try:
        ip = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        if (
            len(value) > 253
            or "." not in value
            or value.endswith(".")
            or re.fullmatch(r"[\d.]+", value)
            or not all(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in value.split(".")
            )
        ):
            raise ValueError("Use a valid DNS hostname or IPv4 address") from None
        if public and value.endswith((".local", ".localhost", ".internal", ".home", ".lan")):
            raise ValueError("Use a public DNS or DDNS hostname") from None
        return value
    if (public and not ip.is_global) or ip.is_unspecified or ip.is_loopback or ip.is_multicast:
        raise ValueError(
            "Use a public IPv4 address for away listening"
            if public
            else "Use the bridge LAN address"
        )
    return str(ip)


def tls_check(host: str, port: int, pin: str, *, public: bool = False) -> tuple[bool, str]:
    """DNS, TCP and TLS with the relay's certificate pin; no relay login or token is sent."""
    try:
        addresses = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
        if public and any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            return False, "DNS resolves to a non-public address"
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # trust is the exact DER SHA-256 pin below
        with socket.create_connection((host, port), timeout=4) as raw:
            with context.wrap_socket(raw, server_hostname=host) as secured:
                actual = hashlib.sha256(secured.getpeercert(binary_form=True)).hexdigest()
                if actual != pin:
                    return False, "certificate pin mismatch; check the forwarding destination"
        return True, "DNS/TCP/TLS and certificate pin verified"
    except (OSError, ssl.SSLError):
        return False, "DNS/TCP/TLS connection failed; check address, forwarding and firewall"


def validate_mips(data: bytes) -> None:
    # ELF32 little-endian MIPS, o32 + MIPS32r2 + NaN2008, matching relay/build.py and Q2-Pod.
    if (
        len(data) < 52
        or data[:6] != b"\x7fELF\x01\x01"
        or struct.unpack_from("<H", data, 18)[0] != 8
        or struct.unpack_from("<I", data, 36)[0] & 0xF0001400 != 0x70001400
    ):
        raise ValueError("The card package must contain the matching MIPS32r2 NaN2008 relay")


def card_files(archive: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        expected = {".soloify/q2-soloify", ".soloify/run", ".soloify/aplay.sh", ".soloify/VERSION"}
        if set(bundle.namelist()) != expected or len(bundle.namelist()) != len(expected):
            raise ValueError("Card ZIP has unexpected or missing files")
        files = {Path(name).name: bundle.read(name) for name in expected}
    if files.pop("VERSION").decode().strip() != __version__:
        raise ValueError("Card package version differs from this wizard")
    validate_mips(files["q2-soloify"])
    return files


def export_card(folder: Path, remote: str, files: dict[str, bytes] | None) -> None:
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    if files:
        for name, content in files.items():
            if name not in {"q2-soloify", "run", "aplay.sh"}:
                raise ValueError("Unexpected card file")
            private_write(folder / name, content)
            (folder / name).chmod(0o755)
    private_write(folder / "remote", remote.encode())


def compose_config(
    install_id: str, key: Path, port: int, auto: bool, volume: str | None = None
) -> dict:
    if not re.fullmatch(r"[a-f0-9]{12}", install_id):
        raise ValueError("Invalid installation id")
    if volume and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", volume):
        raise ValueError("Invalid Docker volume name")
    return {
        "name": f"soloify-{install_id}",
        "services": {
            "soloify": {
                "image": IMAGE,
                "ports": [f"{port}:{port}"],
                "command": ["soloify", "serve", "--port", str(port)],
                "volumes": ["data:/data"],
                "secrets": ["soloist_api_key"],
                "restart": "unless-stopped" if auto else "no",
            }
        },
        "secrets": {"soloist_api_key": {"file": str(key)}},
        "volumes": {
            "data": {"name": volume, "external": True}
            if volume
            else {"name": f"soloify-{install_id}-data"}
        },
    }


class Wizard:
    def __init__(self, root: Path, method: str):
        self.root, self.method = root.resolve(), method
        self.progress = self.root / "setup-state.json"
        try:
            self.state = json.loads(self.progress.read_text())
            if not isinstance(self.state, dict):
                raise ValueError("Invalid setup state")
        except FileNotFoundError:
            self.state = {}
        self.data = self.root / "data"
        self.key = self.root / "secrets/api.key"
        self.compose = self.root / "compose.json"
        self.override = self.root / "pairing.json"
        self.results = {}
        self.maintenance = False
        self.manual_child = None

    def save(self):
        # Explicit allowlist prevents token/client secrets from leaking into resumable progress.
        names = {
            "installation",
            "method",
            "port",
            "lan",
            "wan",
            "wan_port",
            "auto",
            "export",
            "wan_external",
            "lan_playback",
            "configured",
            "native_unit",
            "volume",
        }
        private_write(
            self.progress,
            json.dumps({k: v for k, v in self.state.items() if k in names}, indent=2).encode(),
        )

    def prompt(self, text: str, default: str = "") -> str:
        answer = input(text + (f" [{default}]" if default else "") + ": ").strip()
        if answer.lower() in {"quit", "cancel"}:
            raise KeyboardInterrupt
        return answer or default

    def valid(self, text: str, default, validate):
        while True:
            try:
                return validate(self.prompt(text, str(default) if default else ""))
            except ValueError as exc:
                print(str(exc))

    def command(self, args: list[str], *, capture=True, timeout=180) -> str:
        # Do not print command arguments, stdout or stderr: these may hold credentials.
        try:
            result = subprocess.run(args, capture_output=capture, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Operation timed out; retry setup") from exc
        if result.returncode:
            raise RuntimeError("Operation failed; check Docker/service availability and retry")
        return result.stdout.strip() if capture else ""

    def dc(self, *args: str, timeout=180) -> str:
        cmd = ["docker", "compose", "-f", str(self.compose)]
        return self.command([*cmd, *args], timeout=timeout)

    def execute(self, *args: str, user="soloify") -> str:
        return self.dc("exec", "-T", "-u", user, "soloify", *args)

    def python(self, script: str, *args: str) -> str:
        return self.execute("python3", "-c", script, *args)

    def optional_path(self, label: str, detected: list[Path]) -> Path | None:
        available = [p for p in dict.fromkeys(detected) if p.exists()]
        for index, candidate in enumerate(available, 1):
            print(f"  {index}. {candidate}")
        while True:
            answer = self.prompt(label + " (path, listed number, or Enter to skip)")
            if not answer:
                return None
            if answer.isdecimal() and 1 <= int(answer) <= len(available):
                return available[int(answer) - 1]
            candidate = path_input(answer)
            if candidate.exists():
                return candidate
            print("That path does not exist. Quoted and dragged-in paths are accepted.")

    def credentials(self):
        if self.key.exists() and not ask_yes("Replace the stored API key", False):
            restrict(self.key)
            return
        print("Get your own Soloist API key: https://developer.spotify.com/dashboard/soloist")
        print("Spotify Premium is required. Choose a file, or skip to enter the key privately.")
        source = self.optional_path("API key file", [Path.cwd() / "soloist_api.key"])
        while True:
            key = (
                source.read_text().strip()
                if source
                else getpass.getpass("Soloist API key (hidden): ").strip()
            )
            if key and not any(c.isspace() for c in key):
                private_write(self.key, (key + "\n").encode())
                return
            print("The key must be nonempty and contain no whitespace.")
            source = None

    def existing_storage(self):
        if self.method == "docker":
            if self.compose.exists() or self.state.get("volume"):
                return
            names = self.command(["docker", "volume", "ls", "--format", "{{.Name}}"])
            volumes = [name for name in names.splitlines() if "soloify" in name.lower()]
            if not volumes:
                return
            print("Detected existing Soloify volumes:")
            for index, name in enumerate(volumes, 1):
                print(f"  {index}. {name}")
            selection = self.prompt(
                "Reuse a listed volume (number), or Enter for a new installation"
            )
            if selection:
                if not selection.isdecimal() or not 1 <= int(selection) <= len(volumes):
                    raise ValueError("Choose a listed volume number")
                if not ask_yes(
                    "Confirm the original bridge is stopped; reuse its identity and paired session",
                    False,
                ):
                    raise KeyboardInterrupt
                self.state["volume"] = volumes[int(selection) - 1]
                self.save()
            return
        if (self.data / "bridge").exists():
            return
        candidates = [Path.cwd() / "data"] if (Path.cwd() / "data/bridge").exists() else []
        source = self.optional_path(
            "Import existing bridge data (preserves its identity)", candidates
        )
        if source is None:
            return
        if source.is_symlink() or any(p.is_symlink() for p in source.rglob("*")):
            raise ValueError("Existing data may not contain symbolic links")
        identity = server.Identity(source / "bridge")
        identity.ssl_context()  # check certificate/key before copying
        if not ask_yes(
            "Confirm the original bridge is stopped; import its identity and session", False
        ):
            raise KeyboardInterrupt
        for name in ("bridge", "soloist-state"):
            if (source / name).is_dir():
                shutil.copytree(source / name, self.data / name)
        if (source / "oauth.json").exists():
            private_write(self.data / "oauth.json", (source / "oauth.json").read_bytes())
        for path in [self.data, *self.data.rglob("*")]:
            restrict(path)

    def initialize_docker(self):
        config = compose_config(
            self.state["installation"],
            self.key,
            self.state["port"],
            self.state["auto"],
            self.state.get("volume"),
        )
        private_write(self.compose, json.dumps(config, indent=2).encode())
        # A separate override keeps host networking out of the durable service configuration.
        service = {
            "command": ["sleep", "infinity"],
            "environment": {"SOLOIFY_SETUP_MAINTENANCE": "1"},
        }
        if sys.platform.startswith("linux") and not os.environ.get("SOLOIFY_PAIR_IMPORT_ONLY"):
            service.update(network_mode="host", ports=[])
        # Compose merge needs !reset for ports; JSON cannot express YAML tags. A standalone
        # maintenance config uses the identical project and volume instead of merging ports.
        config["services"]["soloify"].update(service)
        config["services"]["soloify"]["ports"] = []
        private_write(self.override, json.dumps(config, indent=2).encode())
        self.maintenance = True
        self.command(["docker", "compose", "-f", str(self.override), "up", "-d"], timeout=600)
        for _ in range(30):
            try:
                self.execute("pactl", "info")
                return
            except RuntimeError:
                time.sleep(1)
        raise RuntimeError("Container audio service did not become ready")

    def import_session(self, source: Path):
        if not source.is_dir() or not paired(source):
            raise ValueError("Choose a soloist-state directory that has completed pairing")
        if source.is_symlink() or any(p.is_symlink() for p in source.rglob("*")):
            raise ValueError("Session imports may not contain symbolic links")
        if self.method == "docker":
            self.execute("mkdir", "-p", "/data/soloist-state")
            # Copy directory contents, including dotfiles, after stopping the native pairing child.
            self.dc("cp", str(source) + "/.", "soloify:/data/soloist-state")
            self.execute("chown", "-R", "soloify:soloify", "/data/soloist-state", user="root")
            self.execute("chmod", "-R", "go-rwx", "/data/soloist-state", user="root")
        else:
            destination = self.data / "soloist-state"
            if destination.resolve() != source.resolve():
                shutil.copytree(source, destination, dirs_exist_ok=True)
            for path in [destination, *destination.rglob("*")]:
                path.chmod(0o700 if path.is_dir() else 0o600)

    def has_session(self) -> bool:
        if self.method == "docker":
            script = f"from pathlib import Path; STATE=Path('/data/soloist-state'); print({PAIRED})"
            return self.python(script) == "True"
        return paired(self.data / "soloist-state")

    def ensure_build(self):
        if self.method == "docker":
            self.python(
                "import asyncio; from pathlib import Path; from soloify.soloist import Soloist; "
                "asyncio.run(Soloist(Path('/data'),Path('/run/soloify/api.key')).ensure())"
            )
        else:
            asyncio.run(soloist.Soloist(self.data, self.key).ensure())

    def archive_import(self):
        candidates = list(Path.cwd().glob("soloist_release_*.tar.gz"))
        archive = self.optional_path("Optional matching Linux Soloist archive", candidates)
        if archive is None:
            return
        if self.method == "docker":
            self.dc("cp", str(archive), "soloify:/data/import.tar.gz")
            try:
                self.python(
                    "import asyncio; from pathlib import Path; from soloify.soloist import "
                    "Soloist; "
                    "s=Soloist(Path('/data'),Path('/run/soloify/api.key')); "
                    "asyncio.run(s.fetch(Path('/data/import.tar.gz').read_bytes())); s.activate()"
                )
            finally:
                self.execute("rm", "-f", "/data/import.tar.gz")
        else:
            build = soloist.Soloist(self.data, self.key)
            asyncio.run(build.fetch(archive.read_bytes()))
            build.activate()

    def pairing_probe(self) -> bool:
        # Confirm container->host TCP reaches the actual host namespace before discovery pairing.
        # This does not claim to prove that the phone's multicast traffic/firewall can reach it.
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            try:
                self.python(
                    "import socket; "
                    "s=socket.create_connection(('127.0.0.1',int(__import__('sys').argv[1])),3); "
                    "s.close()",
                    str(port),
                )
                listener.settimeout(4)
                peer, _ = listener.accept()
                peer.close()
                return True
            except (OSError, RuntimeError):
                print(
                    "Host-network probe failed. Recheck Docker settings/restart, or import a "
                    "paired session."
                )
                return False

    def pair_process(self, args: list[str]) -> bool:
        # Soloist takes the API key as -k. Never echo argv or unrestricted native logs.
        with tempfile.TemporaryDirectory(prefix="soloify-pair-") as temp:
            path = Path(temp) / "pair.log"
            with path.open("w") as log:
                child = subprocess.Popen(args, stdout=log, stderr=log, text=True)
                try:
                    deadline = time.monotonic() + 180
                    with path.open(errors="replace") as reader:
                        while child.poll() is None and time.monotonic() < deadline:
                            time.sleep(0.25)
                            for line in reader.readlines():
                                # Only reveal the port needed for a temporary firewall exception.
                                if "ZeroConf" in line:
                                    match = re.search(r"(?:port|:)\s*(\d{2,5})\b", line, re.I)
                                    if match:
                                        print(
                                            f"Allow inbound TCP {match[1]} "
                                            "from your LAN while pairing."
                                        )
                    return child.poll() == 0
                finally:
                    if child.poll() is None:
                        child.terminate()
                        try:
                            child.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait()

    def native_pair(self) -> bool:
        if sys.platform.startswith("linux"):
            build = soloist.Soloist(self.data, self.key)
            cmd, _ = build._command()
            # The Nix loader uses SOLOIST_BIN; it is not a shell command string.
            os.environ["SOLOIST_BIN"] = str(build.binary)
            state = build.state
        else:
            state = self.root / "native/soloist-state"
            native = self.root / "native/soloist"
            native.mkdir(parents=True, exist_ok=True)
            if sys.platform == "darwin":
                if __import__("platform").machine().lower() not in {"arm64", "aarch64"}:
                    print(
                        "Native pairing requires Apple Silicon. Import an existing paired "
                        "session on this Mac."
                    )
                    return False
                data = soloist._download(soloist.URL.format("osx_arm64"))
                soloist.unpack(data, native)
                cmd = [str(native / "soloist")]
            else:
                data = soloist._download(
                    "https://soloist-builds.spotifycdn.com/soloist_release_windows_x86_64.zip"
                )
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    names = [n for n in archive.namelist() if Path(n).name == "soloist.exe"]
                    if len(names) != 1:
                        raise ValueError("Native archive has no unique soloist.exe")
                    private_write(native / "soloist.exe", archive.read(names[0]))
                cmd = [str(native / "soloist.exe")]
        if not sys.platform.startswith("linux"):
            import datetime

            version = self.command([*cmd, "--version"])
            built = soloist.build_date(version)
            if (
                built is None
                or not datetime.date.today() - soloist.LIFETIME < built <= datetime.date.today()
            ):
                raise RuntimeError(
                    "Native pairing download is invalid or expired; import a paired session"
                )
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.pair_process(
            [
                *cmd,
                "--pair",
                "-n",
                "soloify",
                "-k",
                self.key.read_text().strip(),
                "-D",
                str(state),
                "-v",
            ]
        ):
            return False
        if not paired(state):
            print("Soloist exited without storing a paired session.")
            return False
        if self.method == "docker":
            self.import_session(state)
        return True

    def pair(self):
        if self.has_session() and not ask_yes("Replace the existing paired Spotify session", False):
            return
        print("Keep Spotify and this computer on the same LAN, without guest/client isolation.")
        print(
            "In Spotify's device picker choose soloify. Allow Soloist through the host firewall on "
            "private networks."
        )
        print(
            "NixOS: temporarily open the printed TCP port with sudo nixos-firewall-tool open "
            "tcp PORT."
        )
        print(
            "After pairing, remove that temporary firewall exception. Ctrl-C cancels; each "
            "attempt waits up to 3 minutes."
        )
        while True:
            source = self.optional_path(
                "Import paired soloist-state folder instead", [Path.cwd() / "data/soloist-state"]
            )
            if source:
                if ask_yes(
                    "Confirm the original Soloist is stopped and this session may be imported",
                    False,
                ):
                    self.import_session(source)
                    return
                continue
            if os.environ.get("SOLOIFY_PAIR_IMPORT_ONLY"):
                print("A paired session import is required until host networking is available.")
                if not ask_yes("Choose a paired session folder", True):
                    raise RuntimeError("A paired session is required")
                continue
            if self.method == "docker" and sys.platform.startswith("linux"):
                paired = self.pairing_probe() and self.pair_process(
                    [
                        "docker",
                        "compose",
                        "-f",
                        str(self.override),
                        "exec",
                        "-T",
                        "-u",
                        "soloify",
                        "soloify",
                        "soloist-run",
                        "--pair",
                        "-v",
                    ]
                )
                # docker exec cancellation alone may leave its remote child; always terminate it.
                if not paired:
                    # Stopping the maintenance container ends any docker-exec child as well.
                    self.dc("stop")
                    self.initialize_docker()
            else:
                paired = self.native_pair()
            if paired:
                print(
                    "Pairing completed; the pairing process is stopped before session "
                    "transfer/startup."
                )
                return
            print(
                "Pairing did not finish. Check same LAN, disable client isolation and allow "
                "the ZeroConf TCP port."
            )
            if not ask_yes("Retry pairing or session import", True):
                raise RuntimeError("Pairing is incomplete; rerun to resume")

    def authorize(self):
        if self.method == "docker":
            present = (
                self.python("from pathlib import Path; print(Path('/data/oauth.json').exists())")
                == "True"
            )
        else:
            present = (self.data / "oauth.json").exists()
        if present and not ask_yes("Replace existing browsing authorization", False):
            return
        print(
            "Browsing is optional. Without authorization, Connect playback works; device "
            "browsing, search and queue are unavailable."
        )
        if not ask_yes("Authorize Spotify browsing", True):
            return
        folder = self.root / "oauth-transfer" if self.method == "docker" else self.data
        imported = self.optional_path("Import oauth.json", [Path.cwd() / "data/oauth.json"])
        if imported:
            auth = json.loads(imported.read_text())
            if not isinstance(auth, dict) or not all(
                auth.get(k) for k in ("client_id", "refresh_token")
            ):
                raise ValueError("OAuth file needs client_id and refresh_token")
            spotify.save(folder / "oauth.json", auth)
        else:
            print("Create an app at https://developer.spotify.com/dashboard and register exactly:")
            print(spotify.REDIRECT)
            while True:
                client_id = self.prompt("Spotify app client ID")
                try:

                    def announce(url):
                        print("Open the authorization page in this computer's browser:", url)
                        webbrowser.open(url)

                    asyncio.run(spotify.login(folder, client_id, announce=announce))
                    break
                except (spotify.APIError, OSError):
                    print(
                        "Authorization failed or timed out. Check client ID, exact redirect "
                        "URI and local callback port 8888."
                    )
                    if not ask_yes("Retry authorization", True):
                        return
        if self.method == "docker":
            self.dc("cp", str(folder / "oauth.json"), "soloify:/data/oauth.json")
            self.execute("chown", "soloify:soloify", "/data/oauth.json", user="root")
            self.execute("chmod", "600", "/data/oauth.json", user="root")
            (folder / "oauth.json").unlink()

    def locations(self):
        facts = detect(self.root)
        default = self.state.get("lan") or next(iter(facts["lan_addresses"]), "")
        lan = self.valid(
            "Bridge LAN IPv4 address (reserve this address in your router's DHCP settings)",
            default,
            lambda v: str(ipaddress.IPv4Address(endpoint(v, public=False))),
        )
        internal = self.valid("Bridge internal TCP port", self.state.get("port", PORT), port_value)
        away = ask_yes("Listen at home and away (No = at home only)", bool(self.state.get("wan")))
        old = (
            self.state.get("wan"),
            self.state.get("wan_port"),
            self.state.get("lan"),
            self.state.get("port"),
        )
        self.state.update(lan=lan, port=internal)
        if away:
            print(
                "Away device → public hostname/address + external TCP port → home router → "
                "bridge LAN address + internal TCP port."
            )
            print(
                "Keep the bridge powered, awake and connected. Reserve its LAN address before "
                "forwarding."
            )
            print("Public addresses can change: use DNS/DDNS and keep its IPv4 record updated.")
            host = self.valid(
                "Public IPv4 address or DNS/DDNS hostname", self.state.get("wan", ""), endpoint
            )
            external = self.valid("External TCP port", self.state.get("wan_port", PORT), port_value)
            print(f"Router forwarding rule: TCP external {external} → {lan}:{internal} (TCP).")
            print(
                f"Allow inbound TCP {internal} in this computer's firewall. "
                f"Docker publishes {internal}:{internal}."
            )
            print(
                "Keep Spotify discovery and OAuth callback 8888 local; forward only the "
                "bridge TCP port."
            )
            print(
                "Compare your router's WAN IPv4 with your public IPv4. Private/100.64.0.0/10 "
                "WAN or a mismatch can mean double NAT or carrier-grade NAT."
            )
            print(
                "For multiple routers, forward TCP through each router. For carrier-grade "
                "NAT, ask your ISP for inbound/public IPv4 service."
            )
            print(
                "Router changes are manual. Unresolved WAN does not prevent LAN listening. "
                "VPN provisioning is outside this setup."
            )
            self.state.update(wan=host, wan_port=external)
            if old != (host, external, lan, internal):
                self.state["wan_external"] = False
        else:
            self.state.pop("wan", None)
            self.state.pop("wan_port", None)
            self.state["wan_external"] = False
        self.save()

    def start(self):
        if self.method == "docker":
            config = compose_config(
                self.state["installation"],
                self.key,
                self.state["port"],
                self.state["auto"],
                self.state.get("volume"),
            )
            private_write(self.compose, json.dumps(config, indent=2).encode())
            # Use ONLY the normal config: removes pairing host networking.
            self.dc("up", "-d", timeout=600)
            self.maintenance = False
            print(
                "Docker startup: enable Docker Desktop's start-at-login option, or enable the "
                "Linux Docker service."
            )
            print(
                "The container restart policy is",
                "unless-stopped." if self.state["auto"] else "disabled.",
            )
        else:
            if self.state.get("native_unit") and not self.state["auto"]:
                self.command(["systemctl", "--user", "disable", "--now", self.unit_name()])
            serve = [
                sys.executable,
                "-m",
                "soloify.cli",
                "--data",
                str(self.data),
                "serve",
                "--key",
                str(self.key),
                "--port",
                str(self.state["port"]),
            ]
            launcher = self.root / "start.py"
            code = (
                "import os\n"
                + f"os.environ['PYTHONPATH'] = {os.environ.get('PYTHONPATH', '')!r}\n"
                + "os.execv("
                + repr(sys.executable)
                + ", "
                + repr(serve)
                + ")\n"
            )
            private_write(launcher, code.encode())
            source = source_root()
            ref = "path:" + str(source) if source else f"github:jtliang24/q2-soloify/v{__version__}"
            cmd = [
                shutil.which("nix") or "nix",
                "develop",
                "--extra-experimental-features",
                "nix-command flakes",
                ref,
                "-c",
                sys.executable,
                str(launcher),
            ]
            if self.state["auto"]:
                unit = Path.home() / ".config/systemd/user" / self.unit_name()
                unit.parent.mkdir(parents=True, exist_ok=True)

                def escape(arg):
                    return (
                        '"' + arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'
                    )

                text = (
                    "[Unit]\nDescription=Soloify bridge\nAfter=network-online.target\n\n[Service]\n"
                    f"ExecStart={' '.join(map(escape, cmd))}\n"
                    f"WorkingDirectory={escape(str(self.root))}\n"
                    "Restart=on-failure\nRestartSec=10\nUMask=0077\n\n[Install]\nWantedBy=default.target\n"
                )
                private_write(unit, text.encode())
                self.command(["systemctl", "--user", "daemon-reload"])
                self.command(["systemctl", "--user", "enable", self.unit_name()])
                self.command(["systemctl", "--user", "restart", self.unit_name()])
                self.state["native_unit"] = True
                print(
                    "The systemd user service starts at login. For startup before login and "
                    "after logout, enable user lingering with loginctl enable-linger USER."
                )
                print(
                    "Keep your PulseAudio/PipeWire user audio service available after logout too."
                )
            else:
                print("Start manually:", __import__("shlex").join(cmd))
                if ask_yes("Start the bridge now", True):
                    log = os.open(
                        self.root / "bridge.log", os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600
                    )
                    try:
                        self.manual_child = subprocess.Popen(
                            cmd, stdout=log, stderr=log, start_new_session=True
                        )
                        private_write(self.root / "bridge.pid", str(self.manual_child.pid).encode())
                    finally:
                        os.close(log)
                self.state["native_unit"] = False
        self.save()

    def unit_name(self):
        return f"soloify-{self.state['installation']}.service"

    def remote(self) -> str:
        lan, wan = self.state["lan"], self.state.get("wan")
        if self.method == "docker":
            args = ["soloify", "remote", "--lan", lan, "--port", str(self.state["port"])]
            if wan:
                args += ["--wan", wan, "--wan-port", str(self.state["wan_port"])]
            return self.execute(*args) + "\n"
        identity = server.Identity.load(self.data)
        lines = server.remote_lines(identity, [lan], self.state["port"])
        if wan:
            lines += server.remote_lines(identity, [wan], self.state["wan_port"])
        return "\n".join(lines) + "\n"

    def export(self, remote: str):
        complete = ask_yes("Export complete .soloify folder (No = remote configuration only)", True)
        files = None
        if complete:
            source = self.optional_path(
                "Matching maintainer card ZIP (or Enter to download release)", []
            )
            data = (
                source.read_bytes() if source else release_asset(f"soloify-card-{__version__}.zip")
            )
            files = card_files(data)
        destination = path_input(
            self.prompt(
                "Export directory containing .soloify",
                self.state.get("export", str(self.root / "export")),
            )
        )
        folder = destination / ".soloify"
        if any((folder / name).exists() for name in (["remote", *files] if files else ["remote"])):
            if not ask_yes(
                "Replace the existing exported files (device delay/quality settings are retained)",
                False,
            ):
                self.results["File export"] = "skipped; previous export retained"
                return
        export_card(folder, remote, files)
        self.state["export"] = str(destination)
        self.save()
        self.results["File export"] = str(folder) + (
            " (complete)" if files else " (configuration only)"
        )
        print(
            f"Copy {folder} to the root of your device's card as .soloify "
            "(enable hidden files in your file manager)."
        )
        print(
            "On the card, run, aplay.sh and q2-soloify must be executable; remote must be private."
        )
        print(
            "Compatible Q2-Pod firmware needs the Streaming → Soloify row (step5-soloify "
            "integration, protocol v2)."
        )
        print(
            "Q2-Pod firmware installation is separate. Future Apps installation can use "
            "configuration-only export."
        )

    def verify(self, remote: str):
        pin = remote.splitlines()[0].split()[3].removeprefix("sha256=")
        ok, detail = False, "not checked"
        for _ in range(10):
            ok, detail = tls_check(self.state["lan"], self.state["port"], pin)
            if ok:
                break
            time.sleep(1)
        self.results["Bridge readiness"] = detail
        self.state["lan_playback"] = ask_yes(
            "On the device, open Streaming → Soloify. Does LAN playback work", False
        )
        self.results["LAN playback"] = (
            "confirmed by user" if self.state["lan_playback"] else "verification pending"
        )
        if self.state.get("wan"):
            _, message = tls_check(self.state["wan"], self.state["wan_port"], pin, public=True)
            print("WAN check from this computer:", message)
            print(
                "A home-network check cannot prove outside access; NAT loopback can also make "
                "it fail."
            )
            print(
                "Disconnect the device from home Wi-Fi, join a phone hotspot using mobile "
                "data, and try playback."
            )
            self.state["wan_external"] = ask_yes(
                "Did playback succeed over the hotspot with home Wi-Fi disconnected", False
            )
            self.results["WAN verification"] = (
                "external playback confirmed by user"
                if self.state["wan_external"]
                else "WAN configured; external verification pending"
            )
        else:
            self.results["WAN verification"] = "at home only"
        self.save()

    def diagnostics(self):
        if self.method == "docker":
            status = json.loads(
                self.python(
                    "import asyncio,json; from pathlib import Path; from soloify.soloist "
                    "import Soloist; "
                    "print(json.dumps(asyncio.run(Soloist(Path('/data'),Path('/run/soloify/api.key')).status())))"
                )
            )
        else:
            status = asyncio.run(soloist.Soloist(self.data, self.key).status())
        self.results["Soloist renewal"] = (
            f"{status['version'] or 'unknown version'}; build {status['build_date']}; "
            f"estimated expiry {status['estimated_expiry']}; "
            f"eligible: {status['renewal_eligible']}; "
            f"automatic: {status['automatic_renewal']}"
        )

    def stop_native(self):
        if self.state.get("native_unit"):
            self.command(["systemctl", "--user", "stop", self.unit_name()])
            if not self.state.get("auto"):
                self.command(["systemctl", "--user", "disable", self.unit_name()])
        pid_file = self.root / "bridge.pid"
        if pid_file.exists():
            pid = int(pid_file.read_text())
            proc = Path(f"/proc/{pid}/cmdline")
            if proc.exists():
                args = proc.read_bytes().split(b"\0")
                if b"soloify.cli" not in args or str(self.data).encode() not in args:
                    raise RuntimeError(
                        "Saved process id belongs to another program; inspect bridge.pid"
                    )
                os.kill(pid, signal.SIGTERM)
                for _ in range(50):
                    if not proc.exists():
                        break
                    time.sleep(0.1)
                else:
                    raise RuntimeError("Previous bridge is still stopping")
            pid_file.unlink()

    def resources_present(self) -> bool:
        """Do not treat saved progress as proof that credentials or persistent storage survive."""
        if not self.key.is_file() or not self.key.read_text().strip():
            return False
        if self.method == "native":
            try:
                identity = server.Identity(self.data / "bridge")
                identity.ssl_context()
                return self.has_session()
            except (OSError, ValueError):
                return False
        if not self.compose.is_file():
            return False
        config = json.loads(self.compose.read_text())
        volume = config["volumes"]["data"]["name"]
        try:
            self.command(["docker", "volume", "inspect", volume])
            # Explicit entrypoint avoids the service/audio bootstrap. The data mount is read-only.
            probe = self.command(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--entrypoint",
                    "python3",
                    "--mount",
                    f"type=volume,src={volume},dst=/data,readonly",
                    IMAGE,
                    "-c",
                    "from pathlib import Path; "
                    "p=Path('/data'); STATE=p/'soloist-state'; "
                    "print(all((p/n).is_file() for n in "
                    f"['bridge/cert.pem','bridge/key.pem','bridge/token']) and {PAIRED})",
                ]
            )
            return probe == "True"
        except RuntimeError:
            return False

    def run(self) -> int:
        if self.method == "native" and not os.environ.get("SOLOIFY_SETUP_NIX"):
            raise RuntimeError("Run native setup through the Nix-managed launcher")
        with installation_lock(self.root):
            return self._run()

    def _run(self) -> int:
        os.umask(0o077)
        if self.state.get("method", self.method) != self.method:
            print(
                "This installation uses another method. Rerun that method or choose a new --root."
            )
            return 2
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state.setdefault("installation", uuid.uuid4().hex[:12])
        if not re.fullmatch(r"[a-f0-9]{12}", self.state["installation"]):
            raise ValueError("Invalid installation id in saved progress")
        self.state["method"] = self.method
        self.state.setdefault("port", PORT)
        intact = not self.state.get("configured") or self.resources_present()
        if not intact:
            print(
                "Stored progress exists, but installation resources are incomplete. "
                "Resume setup to repair them."
            )
        resume = (
            self.state.get("configured")
            and intact
            and ask_yes("Update locations/export using the existing pairing and credentials", True)
        )
        try:
            if not resume:
                if self.progress.exists() and not ask_yes(
                    "Setup may stop this installation while updating it. Continue", False
                ):
                    return 130
                # Stop an existing managed bridge before importing or pairing a session.
                if self.method == "native":
                    self.stop_native()
                elif self.method == "docker" and self.compose.exists():
                    self.dc("stop")
                self.existing_storage()
                self.credentials()
                self.state["auto"] = ask_yes(
                    "Start Soloify automatically", self.state.get("auto", True)
                )
                self.save()
                if self.method == "docker":
                    self.initialize_docker()
                self.archive_import()
                self.ensure_build()
                self.pair()
                self.authorize()
            self.locations()
            if resume and self.method == "native":
                self.stop_native()
            self.start()
            self.export(self.remote())
            self.verify(self.remote())
            self.diagnostics()
            self.state["configured"] = True
            self.save()
            print("\nSetup results:")
            for name, result in self.results.items():
                print(f"  {name}: {result}")
            return 0
        finally:
            # Remove temporary host-network container even on cancellation/errors. Preserve volume.
            if self.method == "docker" and self.maintenance:
                try:
                    self.dc("stop")
                except (RuntimeError, OSError):
                    print(
                        "Could not stop pairing container; stop this installation with Docker "
                        "Compose before retrying."
                    )
            self.override.unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--method", choices=("docker", "native"), required=True)
    args = parser.parse_args(argv)
    try:
        return Wizard(args.root, args.method).run()
    except (KeyboardInterrupt, EOFError):
        print("\nSetup cancelled; progress saved and credentials retained. Rerun to resume.")
        return 130
    except (
        OSError,
        ValueError,
        RuntimeError,
        subprocess.SubprocessError,
        zipfile.BadZipFile,
        soloist.tarfile.TarError,
    ):
        print(
            "Setup could not finish. Check prerequisites and retry; progress and credentials "
            "are retained."
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
