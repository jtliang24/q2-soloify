import pytest

from soloify import __version__, cli


def test_version(capsys):
    with pytest.raises(SystemExit) as done:
        cli.main(["--version"])
    assert done.value.code == 0
    assert capsys.readouterr().out.strip() == f"soloify {__version__}"


def test_unimplemented_command_fails():
    assert cli.main(["serve"]) == 2
