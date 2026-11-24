import stat

import pytest

from soloify import __version__, cli, server


def test_version(capsys):
    with pytest.raises(SystemExit) as done:
        cli.main(["--version"])
    assert done.value.code == 0
    assert capsys.readouterr().out.strip() == f"soloify {__version__}"


def test_login_requires_client_id(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    assert cli.main(["--data", str(tmp_path), "login"]) == 2
    assert "--client-id" in capsys.readouterr().err


def test_remote_lists_lan_then_wan(tmp_path, capsys):
    args = ["--data", str(tmp_path), "remote", "--lan", "192.168.1.5", "--wan", "home.example"]
    assert cli.main(args) == 0
    identity = server.Identity.load(tmp_path)
    tail = f"7443 {identity.token} sha256={identity.pin}"
    assert capsys.readouterr().out.splitlines() == [f"192.168.1.5 {tail}", f"home.example {tail}"]


@pytest.mark.parametrize("port", ["0", "65536", "-1"])
def test_invalid_wan_port_does_not_create_identity(tmp_path, port):
    with pytest.raises(SystemExit) as done:
        cli.main(["--data", str(tmp_path), "remote", "--wan-port", port])
    assert done.value.code == 2
    assert not (tmp_path / "bridge").exists()


def test_remote_rotate_revokes(tmp_path, capsys):
    identity = server.Identity.load(tmp_path)
    old = identity.token
    assert cli.main(["--data", str(tmp_path), "remote", "--lan", "192.0.2.10", "--rotate"]) == 0
    out = capsys.readouterr()
    assert identity.token != old and len(identity.token) == 32
    assert out.out.split()[2] == identity.token
    assert "old one" in out.err
    assert stat.S_IMODE((tmp_path / "bridge" / "token").stat().st_mode) == 0o600
