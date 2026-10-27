import pytest

from soloify import __version__, cli, server


def test_version(capsys):
    with pytest.raises(SystemExit) as done:
        cli.main(["--version"])
    assert done.value.code == 0
    assert capsys.readouterr().out.strip() == f"soloify {__version__}"


def test_unimplemented_command_fails(tmp_path):
    assert cli.main(["--data", str(tmp_path), "login"]) == 2


def test_remote_lists_lan_then_wan(tmp_path, capsys):
    args = ["--data", str(tmp_path), "remote", "--lan", "192.168.1.5", "--wan", "home.example"]
    assert cli.main(args) == 0
    identity = server.Identity.load(tmp_path)
    tail = f"7443 {identity.token} sha256={identity.pin}"
    assert capsys.readouterr().out.splitlines() == [f"192.168.1.5 {tail}", f"home.example {tail}"]
