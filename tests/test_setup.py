"""Read-only detection, credential boundaries and guided setup's observable behavior."""

import asyncio
import json
import socket
import struct
import subprocess
import sys

import pytest

from soloify import cli, server, setup
from soloify import setup_bootstrap as boot


def probes(
    monkeypatch,
    *,
    system="Linux",
    docker=True,
    nix=True,
    desktop=False,
    local=True,
    containers="linux",
    audio=True,
):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(boot.platform, "system", lambda: system)
    monkeypatch.setattr(
        boot.platform, "machine", lambda: "arm64" if system == "Darwin" else "x86_64"
    )
    monkeypatch.setattr(
        boot.shutil,
        "which",
        lambda name: (
            f"/bin/{name}" if (name == "docker" and docker) or (name == "nix" and nix) else None
        ),
    )
    calls = []

    def probe(*args):
        calls.append(args)
        if args[:2] == ("docker", "info"):
            return (
                json.dumps(
                    {
                        "OSType": containers,
                        "OperatingSystem": "Docker Desktop" if desktop else "Debian",
                    }
                )
                if docker
                else ""
            )
        if args[:3] == ("docker", "compose", "version"):
            return "2.30.0" if docker else ""
        if args[:3] == ("docker", "context", "show"):
            return "desktop-linux" if desktop else "default"
        if args[:3] == ("docker", "context", "inspect"):
            return "unix:///run/docker.sock" if local else "ssh://remote"
        if args[:2] == ("nix", "show-config"):
            return "sandbox = true" if nix else ""
        if args[:2] == ("pactl", "info"):
            return "Server Name: PulseAudio" if audio else ""
        return ""

    monkeypatch.setattr(boot, "output", probe)
    monkeypatch.setattr(boot, "lan_addresses", lambda: ["192.168.1.20"])
    return calls


@pytest.mark.parametrize("system", ["Linux", "Darwin", "Windows"])
def test_docker_preferred_and_correct_install_links(tmp_path, monkeypatch, system):
    calls = probes(monkeypatch, system=system)
    facts = boot.detect(tmp_path)
    assert facts["recommended"] == "docker"
    assert facts["needs_host_network"] is (system == "Linux")
    expected = {
        "Linux": "https://docs.docker.com/engine/install/",
        "Darwin": "https://docs.docker.com/desktop/setup/install/mac-install/",
        "Windows": "https://docs.docker.com/desktop/setup/install/windows-install/",
    }
    assert boot.DOCKER_INSTALL[system] == expected[system]
    assert not any("run" in c or "create" in c for c in calls)


def test_native_fallback(tmp_path, monkeypatch):
    probes(monkeypatch, docker=False)
    facts = boot.detect(tmp_path)
    assert facts["recommended"] == "native"
    assert facts["native_audio"]


@pytest.mark.parametrize("local,containers", [(False, "linux"), (True, "windows")])
def test_remote_context_and_windows_containers_are_unavailable(
    tmp_path, monkeypatch, local, containers
):
    probes(monkeypatch, local=local, containers=containers)
    assert boot.detect(tmp_path)["methods"]["docker"]


def test_no_method_exits_without_credentials_downloads_or_writes(tmp_path, monkeypatch):
    probes(monkeypatch, system="Windows", docker=False, nix=False, audio=False)
    root = tmp_path / "absent"
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("must not prompt"))
    monkeypatch.setattr(boot, "provision", lambda *a: pytest.fail("must not provision"))
    assert boot.main(["--root", str(root)]) == 2
    assert not root.exists()


@pytest.mark.parametrize("flags", [["--check", "--json"], ["--json"]])
@pytest.mark.parametrize("run", [boot.main, lambda args: cli.main(["setup", *args])])
def test_check_is_non_mutating_and_does_not_confirm_networking(
    tmp_path, monkeypatch, capsys, flags, run
):
    calls = probes(monkeypatch, desktop=True)
    monkeypatch.setattr(boot, "desktop_host_network", lambda: False)
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("check must not prompt"))
    root = tmp_path / "missing"
    assert run([*flags, "--root", str(root)]) == 0
    assert json.loads(capsys.readouterr().out)["recommended"] == "docker"
    assert not root.exists()
    assert all(c[1] in {"compose", "context", "info", "show-config"} for c in calls)


def test_existing_native_method_drives_report_and_default(tmp_path, monkeypatch, capsys):
    probes(monkeypatch)
    (tmp_path / "setup-state.json").write_text('{"method": "native"}')
    args = ["--root", str(tmp_path)]
    assert boot.main([*args, "--json"]) == 0
    facts = json.loads(capsys.readouterr().out)
    assert facts["saved_method"] == facts["recommended"] == "native"

    prompts = []
    selected = []

    def choose_default(prompt):
        prompts.append(prompt)
        return ""

    def provision(root, method):
        selected.append((root, method))
        return 0

    monkeypatch.setattr("builtins.input", choose_default)
    monkeypatch.setattr(boot, "provision", provision)
    assert boot.main(args) == 0
    text = capsys.readouterr().out
    assert "Existing installation method: native" in text
    assert "Recommended: native" in text
    assert len(prompts) == 1 and "(native)" in prompts[0]
    assert selected == [(tmp_path, "native")]


@pytest.mark.parametrize("nix", [False, True])
def test_existing_root_rejects_method_switch_before_provisioning(
    tmp_path, monkeypatch, capsys, nix
):
    probes(monkeypatch, nix=nix)
    (tmp_path / "setup-state.json").write_text('{"method": "native"}')
    monkeypatch.setattr(boot, "provision", lambda *a: pytest.fail("must not provision"))
    if nix:
        monkeypatch.setattr("builtins.input", lambda _: "docker")
    else:
        monkeypatch.setattr("builtins.input", lambda _: pytest.fail("must not prompt"))
        assert boot.main(["--root", str(tmp_path), "--json"]) == 2
        facts = json.loads(capsys.readouterr().out)
        assert facts["saved_method"] == "native"
        assert facts["recommended"] is None
    assert boot.main(["--root", str(tmp_path)]) == 2
    text = capsys.readouterr().out
    assert "new --root" in text
    if not nix:
        assert "Nix with nix-command and flakes enabled" in text


@pytest.mark.parametrize(
    "setting,confirmed,after,ready",
    [
        (False, False, True, False),
        (False, True, False, False),
        (None, True, True, True),
        (True, False, False, True),
    ],
)
def test_desktop_restart_and_recheck(tmp_path, monkeypatch, setting, confirmed, after, ready):
    facts = {"needs_host_network": True, "docker_desktop": True, "host_network": setting}
    monkeypatch.setattr(boot, "ask_yes", lambda *a: confirmed)
    monkeypatch.setattr(
        boot, "detect", lambda *a: {"methods": {"docker": []}, "host_network": after}
    )
    assert boot.host_network_ready(tmp_path, facts) is ready


def test_mac_windows_do_not_require_host_network_toggle(tmp_path):
    assert boot.host_network_ready(tmp_path, {"needs_host_network": False})


@pytest.mark.parametrize(
    "value",
    ['"a folder/key.txt"', "'a folder/key.txt'", r"a\ folder/key.txt", "& 'a folder/key.txt'"],
)
def test_dragged_or_quoted_paths(value, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert setup.path_input(value) == tmp_path / "a folder/key.txt"


@pytest.mark.parametrize(
    "value",
    [
        "192.168.1.2",
        "10.0.0.1",
        "100.64.0.1",
        "127.0.0.1",
        "http://home.example",
        "home.example/path",
        "home.example:443",
        "999.1.1.1",
        "host.local",
        "-bad.example",
        "a..example",
        "a.example\ntoken",
        "::1",
    ],
)
def test_bad_wan_endpoints_are_rejected(value):
    with pytest.raises(ValueError):
        setup.endpoint(value)


@pytest.mark.parametrize("value", ["8.8.8.8", "bridge.example.com", "my-home.example.com"])
def test_wan_endpoint(value):
    assert setup.endpoint(value) == value


@pytest.mark.parametrize("value", ["-1", "0", "65536", "x", "7443.0"])
def test_bad_port(value):
    with pytest.raises(ValueError):
        setup.port_value(value)


def test_private_export_identity_and_distinct_ports(tmp_path, capsys):
    data = tmp_path / "data"
    identity = server.Identity.load(data)
    before = {p.name: p.read_bytes() for p in (data / "bridge").iterdir()}
    assert (
        cli.main(
            [
                "--data",
                str(data),
                "remote",
                "--lan",
                "192.168.1.2",
                "--port",
                "7443",
                "--wan",
                "home.example",
                "--wan-port",
                "8443",
            ]
        )
        == 0
    )
    remote = capsys.readouterr().out
    assert remote.splitlines() == [
        f"192.168.1.2 7443 {identity.token} sha256={identity.pin}",
        f"home.example 8443 {identity.token} sha256={identity.pin}",
    ]
    folder = tmp_path / ".soloify"
    setup.export_card(
        folder, remote, {"q2-soloify": b"bin", "run": b"sh", "aplay.sh": b"sh", "q2app": b"app"}
    )
    (folder / ".quality").write_text("96")
    setup.export_card(folder, remote, None)
    assert (folder / "remote").stat().st_mode & 0o777 == 0o600
    assert (folder / "run").stat().st_mode & 0o777 == 0o755
    assert (folder / "q2app").stat().st_mode & 0o777 == 0o644
    assert (folder / ".quality").read_text() == "96"
    assert before == {p.name: p.read_bytes() for p in (data / "bridge").iterdir()}


def test_state_allowlist_and_credential_permissions(tmp_path, monkeypatch):
    wizard = setup.Wizard(tmp_path, "docker")
    key = tmp_path / "dragged key.txt"
    key.write_text("APISECRET")
    monkeypatch.setattr(wizard, "optional_path", lambda *a: key)
    wizard.credentials()
    assert wizard.key.read_text().strip() == "APISECRET"
    assert wizard.key.stat().st_mode & 0o777 == 0o600
    wizard.state = {"installation": "a" * 12, "token": "PRIVATE", "api_key": "APISECRET"}
    wizard.save()
    assert "PRIVATE" not in wizard.progress.read_text()
    assert "APISECRET" not in wizard.progress.read_text()


def test_existing_api_key_is_preserved_by_default(tmp_path, monkeypatch):
    wizard = setup.Wizard(tmp_path, "docker")
    setup.private_write(wizard.key, b"OLD")
    monkeypatch.setattr(setup, "ask_yes", lambda *a: False)
    wizard.credentials()
    assert wizard.key.read_bytes() == b"OLD"


def test_compose_is_pinned_stable_and_normal_networked(tmp_path):
    config = setup.compose_config("a" * 12, tmp_path / "key", 8123, True)
    service = config["services"]["soloify"]
    assert service["image"].endswith(":" + boot.VERSION)
    assert service["ports"] == ["8123:8123"]
    assert "network_mode" not in service
    assert service["restart"] == "unless-stopped"
    assert config["volumes"]["data"]["name"] == "soloify-aaaaaaaaaaaa-data"


def test_probe_failure_does_not_claim_pairing(tmp_path, monkeypatch, capsys):
    wizard = setup.Wizard(tmp_path, "docker")

    def failed(*args):
        raise RuntimeError("unreachable")

    monkeypatch.setattr(wizard, "python", failed)
    assert not wizard.pairing_probe()
    assert "probe failed" in capsys.readouterr().out


def test_probe_success(tmp_path, monkeypatch):
    wizard = setup.Wizard(tmp_path, "docker")

    def connect(script, port):
        with socket.create_connection(("127.0.0.1", int(port))):
            pass

    monkeypatch.setattr(wizard, "python", connect)
    assert wizard.pairing_probe()


async def test_tls_certificate_pin_success_and_mismatch(tmp_path):
    identity = server.Identity.load(tmp_path)

    async def end(reader, writer):
        writer.close()
        await writer.wait_closed()

    listener = await asyncio.start_server(end, "127.0.0.1", 0, ssl=identity.ssl_context())
    async with listener:
        port = listener.sockets[0].getsockname()[1]
        ok, _ = await asyncio.to_thread(setup.tls_check, "127.0.0.1", port, identity.pin)
        assert ok
        ok, message = await asyncio.to_thread(setup.tls_check, "127.0.0.1", port, "0" * 64)
        assert not ok and "pin mismatch" in message


def test_wan_never_claims_external_verification_from_home_check(tmp_path, monkeypatch):
    wizard = setup.Wizard(tmp_path, "docker")
    wizard.state = {"lan": "192.168.1.2", "port": 7443, "wan": "home.example", "wan_port": 8443}
    monkeypatch.setattr(setup, "tls_check", lambda *a, **kw: (True, "pin verified"))
    monkeypatch.setattr(setup, "ask_yes", lambda *a: False)
    wizard.verify("192.168.1.2 7443 TOKEN sha256=PIN\n")
    assert wizard.results["WAN verification"] == "WAN configured; external verification pending"
    assert not wizard.state["wan_external"]


def test_oauth_failure_can_skip_without_exposing_error(tmp_path, monkeypatch, capsys):
    wizard = setup.Wizard(tmp_path, "native")
    monkeypatch.setattr(
        setup, "ask_yes", lambda question, default=True: question.startswith("Authorize")
    )
    monkeypatch.setattr(wizard, "optional_path", lambda *a: None)
    monkeypatch.setattr(wizard, "prompt", lambda *a: "client")

    async def fail(*a, **kw):
        raise setup.spotify.APIError("offline", "TOKEN_SECRET")

    monkeypatch.setattr(setup.spotify, "login", fail)
    wizard.authorize()
    assert "TOKEN_SECRET" not in capsys.readouterr().out
    assert not (wizard.data / "oauth.json").exists()


def test_native_session_import_preserves_dotfiles_and_rejects_links(tmp_path):
    src = tmp_path / "source"
    src.mkdir()
    (src / "settings/Users/1-user").mkdir(parents=True)
    (src / "settings/Users/1-user/cached").write_text("PRIVATE")
    wizard = setup.Wizard(tmp_path / "install", "native")
    wizard.import_session(src)
    assert wizard.has_session()
    cached = wizard.data / "soloist-state/settings/Users/1-user/cached"
    assert cached.read_text() == "PRIVATE"
    assert cached.stat().st_mode & 0o777 == 0o600
    (src / "link").symlink_to(src / "settings")
    with pytest.raises(ValueError, match="symbolic"):
        wizard.import_session(src)


def test_unpaired_state_is_not_a_session(tmp_path):
    # Soloist creates these before pairing completes; credentials only appear under settings/.
    src = tmp_path / "source"
    (src / "cache").mkdir(parents=True)
    (src / ".device_id").write_text("DEVICE")
    (src / "cache/client_token").write_text("TOKEN")
    wizard = setup.Wizard(tmp_path / "install", "native")
    assert not setup.paired(src)
    with pytest.raises(ValueError, match="completed pairing"):
        wizard.import_session(src)
    assert not wizard.has_session()


def test_pairing_logs_only_firewall_port(tmp_path, capsys):
    wizard = setup.Wizard(tmp_path, "native")
    script = (
        "import time; print('SECRET ZeroConf port 54321',flush=True); "
        "print('API_KEY_SECRET',flush=True); time.sleep(.4)"
    )
    assert wizard.pair_process([sys.executable, "-c", script])
    output = capsys.readouterr().out
    assert "54321" in output
    assert "SECRET" not in output


def test_cancellation_stops_maintenance_container(tmp_path, monkeypatch):
    wizard = setup.Wizard(tmp_path, "docker")
    monkeypatch.setattr(wizard, "credentials", lambda: None)
    monkeypatch.setattr(wizard, "existing_storage", lambda: None)
    monkeypatch.setattr(setup, "ask_yes", lambda *a: True)

    def initialize():
        wizard.maintenance = True
        wizard.override.write_text("{}")
        raise KeyboardInterrupt

    monkeypatch.setattr(wizard, "initialize_docker", initialize)
    commands = []
    monkeypatch.setattr(wizard, "dc", lambda *a, **kw: commands.append(a))
    with pytest.raises(KeyboardInterrupt):
        wizard.run()
    assert commands == [("stop",)]
    assert not wizard.override.exists()


def test_configuration_only_rerun_does_not_pair_or_replace_identity(tmp_path, monkeypatch):
    wizard = setup.Wizard(tmp_path, "docker")
    wizard.state = {"configured": True, "installation": "b" * 12, "port": 7443}
    monkeypatch.setattr(wizard, "resources_present", lambda: True)
    monkeypatch.setattr(setup, "ask_yes", lambda *a: True)
    for name in ["credentials", "pair", "authorize", "ensure_build", "archive_import"]:
        monkeypatch.setattr(wizard, name, lambda: pytest.fail("must preserve existing pairing"))
    for name in ["locations", "start", "diagnostics"]:
        monkeypatch.setattr(wizard, name, lambda: None)
    monkeypatch.setattr(wizard, "remote", lambda: "private remote")
    monkeypatch.setattr(wizard, "export", lambda remote: None)
    monkeypatch.setattr(wizard, "pair_device", lambda remote: True)
    monkeypatch.setattr(wizard, "verify", lambda remote: None)
    assert wizard.run() == 0
    assert wizard.state["installation"] == "b" * 12


def mips_binary():
    data = bytearray(52)
    data[:6] = b"\x7fELF\x01\x01"
    struct.pack_into("<H", data, 18, 8)
    struct.pack_into("<I", data, 36, 0x70001400)
    return bytes(data)


def test_native_existing_data_import_preserves_identity_and_oauth(tmp_path, monkeypatch):
    source = tmp_path / "existing"
    identity = server.Identity.load(source)
    (source / "soloist-state/settings/Users/1-user").mkdir(parents=True)
    (source / "soloist-state/settings/Users/1-user/cached").write_text("PAIRED")
    (source / "oauth.json").write_text('{"refresh_token":"PRIVATE"}')
    wizard = setup.Wizard(tmp_path / "installation", "native")
    monkeypatch.setattr(wizard, "optional_path", lambda *a: source)
    monkeypatch.setattr(setup, "ask_yes", lambda *a: True)
    wizard.existing_storage()
    installed = server.Identity.load(wizard.data)
    assert installed.pin == identity.pin and installed.token == identity.token
    assert (wizard.data / "oauth.json").read_bytes() == (source / "oauth.json").read_bytes()
    assert (wizard.data / "soloist-state/settings/Users/1-user/cached").read_text() == "PAIRED"


def test_reusing_docker_volume_is_external_and_preserves_name(tmp_path):
    config = setup.compose_config("c" * 12, tmp_path / "key", 7443, True, "old_soloify-data")
    assert config["volumes"]["data"] == {"external": True, "name": "old_soloify-data"}


def test_resume_requires_actual_persistent_resources(tmp_path):
    wizard = setup.Wizard(tmp_path, "docker")
    wizard.state["configured"] = True
    assert not wizard.resources_present()


def test_installation_lock_rejects_concurrent_wizards(tmp_path):
    with setup.installation_lock(tmp_path):
        with pytest.raises(RuntimeError, match="Another setup"):
            with setup.installation_lock(tmp_path):
                pytest.fail("lock should prevent concurrent updates")


def test_native_service_uses_updated_port_and_restarts(tmp_path, monkeypatch):
    monkeypatch.setattr(setup.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "package folder"))
    wizard = setup.Wizard(tmp_path / "bridge folder", "native")
    wizard.state = {"installation": "d" * 12, "port": 8123, "auto": True, "native_unit": True}
    commands = []
    monkeypatch.setattr(wizard, "command", lambda cmd, **kw: commands.append(cmd))
    wizard.start()
    unit = (tmp_path / ".config/systemd/user" / wizard.unit_name()).read_text()
    assert "ExecStart=" in unit and "WorkingDirectory=" in unit
    assert ["systemctl", "--user", "restart", wizard.unit_name()] in commands
    assert "8123" in (wizard.root / "start.py").read_text()
    assert "API" not in unit


def test_installed_setup_redacts_invalid_import_errors(tmp_path, monkeypatch, capsys):
    probes(monkeypatch)

    def failed(self):
        raise ValueError("SECRET_IMPORT_CONTENT")

    monkeypatch.setattr(setup.Wizard, "run", failed)
    assert boot.main(["--root", str(tmp_path), "--selected", "docker"], installed=True) == 2
    assert "SECRET_IMPORT_CONTENT" not in capsys.readouterr().out


def test_card_export_failure_explains_how_to_retry(tmp_path, monkeypatch, capsys):
    destination = tmp_path / "existing-file"
    destination.touch()
    monkeypatch.setattr(setup, "ask_yes", lambda *a: False)
    monkeypatch.setattr("builtins.input", lambda _: str(destination))
    monkeypatch.setattr(setup.Wizard, "run", lambda self: self.export("private remote"))

    assert setup.main(["--root", str(tmp_path), "--method", "docker"]) == 2
    text = capsys.readouterr().out
    assert "Could not write the card export" in text
    assert "Check that the destination is writable" in text
    assert "private remote" not in text


class FakeDiscovery:
    made = []

    def __init__(self, host, port, **kw):
        self.host, self.port, self.running = host, port, False
        FakeDiscovery.made.append(self)

    def start(self):
        self.running = True
        return True

    def stop(self):
        self.running = False


def _pairing_wizard(tmp_path, monkeypatch, method, state, returncode=0):
    wizard = setup.Wizard(tmp_path, method)
    wizard.state = {"installation": "c" * 12, **state}
    FakeDiscovery.made = []
    runs = []

    def run(cmd, check=False):
        assert FakeDiscovery.made[-1].running  # the host answers broadcasts meanwhile
        runs.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode)

    monkeypatch.setattr(setup.pairing, "Discovery", FakeDiscovery)
    monkeypatch.setattr(setup.subprocess, "run", run)
    monkeypatch.setattr(setup, "tls_check", lambda *a, **kw: (True, "pin verified"))
    return wizard, runs


REMOTE = "192.0.2.10 7443 token sha256=" + "ab" * 32 + "\n"


def test_docker_pairing_runs_listener_on_host_and_exec_without_discovery(tmp_path, monkeypatch):
    wizard, runs = _pairing_wizard(
        tmp_path,
        monkeypatch,
        "docker",
        {"lan": "192.0.2.10", "port": 7443, "wan": "music.example.com", "wan_port": 8443},
    )
    assert wizard.pair_device(REMOTE)
    assert runs == [
        ["docker", "compose", "-f", str(wizard.compose), "exec", "-T", "-u", "soloify"]
        + ["soloify", "soloify", "pair-device", "--no-discovery", "--lan", "192.0.2.10"]
        + ["--port", "7443", "--wan", "music.example.com", "--wan-port", "8443"]
    ]
    (listener,) = FakeDiscovery.made
    assert (listener.host, listener.port, listener.running) == ("192.0.2.10", 7443, False)
    assert wizard.results["Device pairing"] == "paired (home and away)"


def test_native_pairing_home_only(tmp_path, monkeypatch):
    wizard, runs = _pairing_wizard(
        tmp_path, monkeypatch, "native", {"lan": "192.0.2.10", "port": 7443}
    )
    assert wizard.pair_device(REMOTE)
    assert runs[0][:5] == [sys.executable, "-m", "soloify.cli", "--data", str(wizard.data)]
    assert runs[0][5:] == ["pair-device", "--no-discovery", "--lan", "192.0.2.10", "--port", "7443"]
    assert wizard.results["Device pairing"] == "paired (home only)"


def test_windows_firewall_note(tmp_path, monkeypatch, capsys):
    wizard, _ = _pairing_wizard(
        tmp_path, monkeypatch, "docker", {"lan": "192.0.2.10", "port": 7443}
    )
    monkeypatch.setattr(setup.sys, "platform", "win32")
    wizard.pair_device(REMOTE)
    assert "Windows Firewall" in capsys.readouterr().out


@pytest.mark.parametrize(
    "pair,fallback,exported", [(True, True, True), (True, False, False), (False, None, True)]
)
def test_failed_pairing_offers_manual_export(tmp_path, monkeypatch, pair, fallback, exported):
    wizard, _ = _pairing_wizard(
        tmp_path, monkeypatch, "docker", {"lan": "192.0.2.10", "port": 7443}, returncode=1
    )
    answers = iter([pair, fallback])
    monkeypatch.setattr(setup, "ask_yes", lambda *a: next(answers))
    done = []
    monkeypatch.setattr(wizard, "export", lambda remote: done.append(remote))
    wizard.connect_device(REMOTE)
    assert done == ([REMOTE] if exported else [])
    if pair:
        assert wizard.results["Device pairing"].startswith("not paired")


def test_successful_pairing_skips_export(tmp_path, monkeypatch):
    wizard, _ = _pairing_wizard(
        tmp_path, monkeypatch, "native", {"lan": "192.0.2.10", "port": 7443}
    )
    monkeypatch.setattr(setup, "ask_yes", lambda *a: True)
    monkeypatch.setattr(wizard, "export", lambda remote: pytest.fail("paired: nothing to copy"))
    wizard.connect_device(REMOTE)
