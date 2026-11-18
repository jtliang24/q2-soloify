"""Standard-library-only setup launcher. Also shipped verbatim as soloify-setup.py."""

import argparse
import importlib.util
import io
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import urllib.request
import venv
import zipfile
from pathlib import Path

VERSION = "0.0.1"
REPOSITORY = "jtliang24/q2-soloify"
RELEASE = f"https://github.com/{REPOSITORY}/releases/download/v{VERSION}"
IMAGE = f"ghcr.io/{REPOSITORY}:{VERSION}"
HOST_NETWORK_DOC = "https://docs.docker.com/engine/network/drivers/host/"
DOCKER_INSTALL = {
    "Darwin": "https://docs.docker.com/desktop/setup/install/mac-install/",
    "Windows": "https://docs.docker.com/desktop/setup/install/windows-install/",
    "Linux": "https://docs.docker.com/engine/install/",
}


class SetupError(RuntimeError):
    """An actionable setup failure with a message safe to display."""


def output(*cmd: str) -> str:
    """Bounded, read-only probes. Never echo raw Docker diagnostics or environment secrets."""
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=12, check=False)
        return done.stdout.strip() if done.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def lan_addresses() -> list[str]:
    found = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))
            found.add(sock.getsockname()[0])
        found.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    return sorted(ip for ip in found if not ip.startswith("127.") and ip != "0.0.0.0")


def desktop_host_network() -> bool | None:
    """Inspect known Desktop settings on Linux; unknown schemas require explicit confirmation."""
    for name in ("settings-store.json", "settings.json"):
        path = Path.home() / ".docker/desktop" / name
        try:
            value = json.loads(path.read_text()).get("hostNetworkingEnabled")
            if isinstance(value, bool):
                return value
        except (OSError, ValueError, AttributeError):
            pass
    return None


def detect(root: Path) -> dict:
    system, machine = platform.system(), platform.machine().lower()
    docker = shutil.which("docker")
    compose = bool(docker and output("docker", "compose", "version"))
    context = output("docker", "context", "show") if docker else ""
    endpoint = (
        output("docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}")
        if docker
        else ""
    )
    endpoint = os.environ.get("DOCKER_HOST", endpoint)
    local = endpoint.startswith(("unix://", "npipe://"))
    info = output("docker", "info", "--format", "{{json .}}") if docker else ""
    try:
        info = json.loads(info) if info else {}
    except ValueError:
        info = {}
    desktop = "docker desktop" in info.get("OperatingSystem", "").lower()
    missing = []
    if not docker:
        missing.append("Docker CLI")
    if not compose:
        missing.append("Docker Compose v2")
    if not info:
        missing.append("access to a running Docker daemon")
    if info and info.get("OSType") != "linux":
        missing.append("switch Docker to Linux containers")
    if docker and not local:
        missing.append("a local Docker context (remote engines are unsupported)")
    if machine not in {"x86_64", "amd64", "aarch64", "arm64", "armv7l", "armv8l"}:
        missing.append("supported host architecture")
    nix = bool(shutil.which("nix"))
    runtime = Path(
        os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{getattr(os, 'getuid', lambda: 0)()}")
    )
    audio = bool(output("pactl", "info")) or (runtime / "pulse/native").exists()
    native_missing = []
    if system != "Linux":
        native_missing.append("Linux (native bridge audio is supported only on Linux)")
    if machine not in {"x86_64", "aarch64", "arm64"}:
        native_missing.append("x86_64 or aarch64 Nix environment")
    if not nix:
        native_missing.append("Nix with nix-command and flakes enabled")
    elif not output("nix", "show-config", "--extra-experimental-features", "nix-command flakes"):
        native_missing.append("a working Nix installation")
    if not audio:
        native_missing.append("a running PulseAudio or PipeWire PulseAudio server")
    methods = {"docker": missing, "native": native_missing}
    saved_method = None
    try:
        state = json.loads((root / "setup-state.json").read_text())
        if isinstance(state, dict) and state.get("method") in ("docker", "native"):
            saved_method = state["method"]
    except (OSError, ValueError):
        pass
    candidates = (saved_method,) if saved_method else ("docker", "native")
    recommended = next((method for method in candidates if not methods[method]), None)
    return {
        "os": system,
        "architecture": machine,
        "python": platform.python_version(),
        "python_environment": "virtualenv" if sys.prefix != sys.base_prefix else "system",
        "docker_context": context,
        "docker_desktop": desktop,
        "daemon_access": bool(info),
        "linux_containers": info.get("OSType") == "linux",
        "compose": compose,
        "host_network": desktop_host_network() if desktop and system == "Linux" else None,
        "needs_host_network": system == "Linux",
        "nix": nix,
        "native_audio": audio,
        "methods": methods,
        "saved_method": saved_method,
        "recommended": recommended,
        "lan_addresses": lan_addresses(),
        "existing": [
            str(p)
            for p in (root / "setup-state.json", root / "data", Path.cwd() / "data")
            if p.exists()
        ],
    }


def report(facts: dict) -> None:
    print(
        f"Soloify {VERSION}: {facts['os']} {facts['architecture']}; "
        f"Python {facts['python']} ({facts['python_environment']})"
    )
    for name, missing in facts["methods"].items():
        print(f"  {name}: " + ("ready" if not missing else "; ".join(missing)))
    print("Docker context:", facts["docker_context"] or "unavailable")
    print("LAN addresses:", ", ".join(facts["lan_addresses"]) or "none detected")
    if facts["needs_host_network"] and facts["docker_desktop"]:
        print("Linux Desktop pairing host networking:", facts["host_network"])
    if facts["saved_method"]:
        print("Existing installation method:", facts["saved_method"])
        if not facts["recommended"]:
            print(
                "Fix the requirements listed for the existing installation method and rerun. "
                "Use a new --root for a separate installation."
            )
    print("Recommended:", facts["recommended"] or "no usable method")
    if facts["existing"]:
        print("Existing resources:", ", ".join(facts["existing"]))
    if facts["methods"]["docker"]:
        print("Install/start Docker:", DOCKER_INSTALL.get(facts["os"], DOCKER_INSTALL["Linux"]))
        if Path("/etc/NIXOS").exists():
            print(
                "NixOS: enable virtualisation.docker.enable, add your user to docker, rebuild "
                "and log in again."
            )
    if facts["methods"]["native"]:
        print(
            "Native Linux: https://nixos.org/download/ ; enable nix-command/flakes and "
            "PulseAudio compatibility."
        )


def ask_yes(prompt: str, default: bool = True) -> bool:
    value = input(prompt + (" [Y/n]: " if default else " [y/N]: ")).strip().lower()
    return value in {"y", "yes"} if value else default


def host_network_ready(root: Path, facts: dict) -> bool:
    if not facts["needs_host_network"] or not facts["docker_desktop"]:
        return True
    if facts["host_network"] is True:
        return True
    print(
        "Docker Desktop: Settings → Resources → Network → Enable host networking → Apply and "
        "restart"
    )
    print(HOST_NETWORK_DOC)
    if not ask_yes("Confirm host networking is enabled and Docker has restarted", False):
        return False
    again = detect(root)
    return not again["methods"]["docker"] and again["host_network"] is not False


def release_asset(name: str) -> bytes:
    """Verify artifacts against the same release's HTTPS checksum manifest."""
    import hashlib

    try:
        with urllib.request.urlopen(f"{RELEASE}/SHA256SUMS", timeout=60) as response:
            rows = response.read().decode().splitlines()
    except OSError as exc:
        raise SetupError(
            "Could not download release checksums. Check access to GitHub and rerun setup."
        ) from exc
    except UnicodeError as exc:
        raise SetupError(
            "Could not read release checksums. Download the launcher from the matching release."
        ) from exc
    sums = dict(
        (line.split()[1].lstrip("*"), line.split()[0]) for line in rows if len(line.split()) == 2
    )
    if name not in sums:
        raise SetupError(
            f"Release checksum missing for {name}. Download the launcher from the matching release."
        )
    try:
        with urllib.request.urlopen(f"{RELEASE}/{name}", timeout=120) as response:
            data = response.read()
    except OSError as exc:
        raise SetupError(
            f"Could not download {name}. Check access to GitHub and rerun setup."
        ) from exc
    if hashlib.sha256(data).hexdigest() != sums[name]:
        raise SetupError(
            f"Release checksum mismatch for {name}. Rerun setup to download a fresh copy."
        )
    return data


def source_root() -> Path | None:
    # Works in the checked-out script and in the package module. Installed wheels have no flake.
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").exists() and (parent / "bridge/soloify").is_dir():
            return parent
    return None


def provision(root: Path, method: str) -> int:
    source = source_root()
    if method == "native" and not os.environ.get("SOLOIFY_SETUP_NIX"):
        ref = "path:" + str(source) if source else f"github:{REPOSITORY}/v{VERSION}"
        env = dict(os.environ, SOLOIFY_SETUP_NIX="1")
        # Nix manages the runtime and dependencies, including its Soloist loader on NixOS.
        return subprocess.call(
            [
                "nix",
                "develop",
                "--extra-experimental-features",
                "nix-command flakes",
                ref,
                "-c",
                "python3",
                str(Path(__file__).resolve()),
                "--root",
                str(root),
                "--selected",
                method,
            ],
            env=env,
        )
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if method == "native":
        python = Path(sys.executable)
        packages = root / "python"
        packages.mkdir(exist_ok=True)
        if source:
            packages = source / "bridge"
        else:
            with zipfile.ZipFile(
                io.BytesIO(release_asset(f"soloify-{VERSION}-py3-none-any.whl"))
            ) as wheel:
                wheel.extractall(packages)
    else:
        env_path = root / "venv"
        dependencies = all(importlib.util.find_spec(n) for n in ("aiohttp", "cryptography"))
        venv.EnvBuilder(with_pip=not dependencies, system_site_packages=dependencies).create(
            env_path
        )
        python = env_path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        packages = source / "bridge" if source else env_path / "soloify-package"
        if not source:
            packages.mkdir(exist_ok=True)
            with zipfile.ZipFile(
                io.BytesIO(release_asset(f"soloify-{VERSION}-py3-none-any.whl"))
            ) as wheel:
                wheel.extractall(packages)
        if not dependencies:
            if source:
                artifact = source
            else:
                artifact = root / f"soloify-{VERSION}-py3-none-any.whl"
                artifact.write_bytes(release_asset(artifact.name))
            subprocess.run([str(python), "-m", "pip", "install", str(artifact)], check=True)
    env = dict(os.environ, PYTHONPATH=str(packages))
    return subprocess.call(
        [str(python), "-m", "soloify.setup", "--root", str(root), "--method", method], env=env
    )


def main(argv: list[str] | None = None, *, installed: bool = False) -> int:
    parser = argparse.ArgumentParser(
        description="Guided Soloify setup (Python 3.11+). No system prerequisites are installed."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="read-only readiness report; no containers or downloads",
    )
    parser.add_argument(
        "--json", action="store_true", help="read-only JSON readiness report (implies --check)"
    )
    parser.add_argument("--root", type=Path, default=Path.home() / ".soloify-bridge")
    parser.add_argument("--selected", choices=("docker", "native"), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if sys.version_info < (3, 11):  # noqa: UP036 - standalone bootstrap may precede installation
        print("Python 3.11 or later is required.")
        return 2
    root = args.root.expanduser().resolve()
    try:
        facts = detect(root)
        if args.json:
            print(json.dumps(facts, indent=2))
        else:
            report(facts)
        if args.check or args.json:
            return 0 if facts["recommended"] else 2
        if not facts["recommended"]:
            return 2
        method = args.selected
        if method is None:
            method = (
                input(f"Method [docker/native] ({facts['recommended']}): ").strip().lower()
                or facts["recommended"]
            )
        if facts["saved_method"] and method != facts["saved_method"]:
            print(
                "Rerun with the existing installation method or choose a new --root "
                "for a separate installation."
            )
            return 2
        if method not in facts["methods"] or facts["methods"][method]:
            print("Selected method is unavailable; fix the requirements above and rerun.")
            return 2
        if method == "docker" and not host_network_ready(root, facts):
            if not ask_yes(
                "Continue using a paired session import instead of container pairing", False
            ):
                return 2
            os.environ["SOLOIFY_PAIR_IMPORT_ONLY"] = "1"
        if installed and (method != "native" or os.environ.get("SOLOIFY_SETUP_NIX")):
            from .setup import main as wizard_main

            return wizard_main(["--root", str(root), "--method", method])
        return provision(root, method)
    except (KeyboardInterrupt, EOFError):
        print("\nSetup cancelled; existing credentials and identity are retained.")
        return 130
    except SetupError as exc:
        print(exc)
        return 2
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(
            f"Setup could not continue ({type(exc).__name__}). "
            "Check prerequisites and rerun; existing data is retained."
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
