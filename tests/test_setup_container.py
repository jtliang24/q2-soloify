"""Opt-in real Docker smoke test using disposable storage and fake credentials only.

SOLOIFY_SETUP_TEST_IMAGE=soloify-setup-test:0.0.1 pytest tests/test_setup_container.py
The image must already be built; this test never contacts Spotify or uses a real session.
"""

import json
import os
import socket
import time
import uuid

import pytest

from soloify import setup

IMAGE = os.environ.get("SOLOIFY_SETUP_TEST_IMAGE")
pytestmark = pytest.mark.skipif(
    not IMAGE, reason="set SOLOIFY_SETUP_TEST_IMAGE to a local test image"
)


def test_pairing_storage_oauth_transfer_and_normal_network(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "IMAGE", IMAGE)
    install_id = uuid.uuid4().hex[:12]
    wizard = setup.Wizard(tmp_path, "docker")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    wizard.state = {
        "installation": install_id,
        "port": port,
        "auto": False,
        "lan": "127.0.0.1",
        "wan": "home.example",
        "wan_port": 8443,
    }
    setup.private_write(wizard.key, b"FAKE-SETUP-TEST-KEY\n")
    # Random project/volume names ensure cleanup cannot select any existing installation.
    assert (
        wizard.command(
            [
                "docker",
                "volume",
                "ls",
                "--filter",
                f"name=soloify-{install_id}-data",
                "--format",
                "{{.Name}}",
            ]
        )
        == ""
    )
    try:
        wizard.initialize_docker()
        assert (
            wizard.python("from pathlib import Path; print(Path('/data/soloist/soloist').exists())")
            == "False"
        )
        assert wizard.pairing_probe()
        session = tmp_path / "session"
        session.mkdir()
        (session / "settings/Users/1-user").mkdir(parents=True)
        (session / "settings/Users/1-user/cached").write_text("FAKE-SESSION")
        wizard.import_session(session)
        assert wizard.has_session()
        oauth = tmp_path / "oauth.json"
        oauth.write_text(json.dumps({"client_id": "FAKE-CLIENT", "refresh_token": "FAKE-TOKEN"}))
        monkeypatch.setattr(setup, "ask_yes", lambda *a: True)
        monkeypatch.setattr(wizard, "optional_path", lambda *a: oauth)
        wizard.authorize()
        assert wizard.execute("stat", "-c", "%a", "/data/oauth.json") == "600"
        before = wizard.remote()
        assert wizard.resources_present()
        config = setup.compose_config(install_id, wizard.key, port, False)
        # A test audio source and maintenance bypass avoid any Soloist download/credentials.
        config["services"]["soloify"]["command"] += ["--source", "test"]
        config["services"]["soloify"]["environment"] = {"SOLOIFY_SETUP_MAINTENANCE": "1"}
        setup.private_write(wizard.compose, json.dumps(config).encode())
        wizard.dc("up", "-d")
        container = wizard.dc("ps", "-q")
        network = wizard.command(
            ["docker", "inspect", "--format", "{{.HostConfig.NetworkMode}}", container]
        )
        assert network != "host"
        assert wizard.remote() == before
        pin = before.splitlines()[0].split()[3].removeprefix("sha256=")
        for _ in range(20):
            ok, detail = setup.tls_check("127.0.0.1", port, pin)
            if ok:
                break
            time.sleep(0.2)
        assert ok, detail
    finally:
        # Only this test's random project and disposable volume are removed.
        wizard.dc("down", "--volumes", "--remove-orphans")
