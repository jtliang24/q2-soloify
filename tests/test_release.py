"""Release contents, executable architecture, version consistency and checksums."""

import hashlib
import importlib.util
import io
import zipfile
from pathlib import Path

import pytest

from soloify import __version__, setup
from test_setup import mips_binary, probes

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release", ROOT / "tools/release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


@pytest.fixture
def wheel(tmp_path):
    path = tmp_path / f"soloify-{__version__}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"soloify-{__version__}.dist-info/METADATA", f"Version: {__version__}\n")
        for name in ("__init__.py", "setup_bootstrap.py"):
            archive.write(ROOT / "bridge/soloify" / name, f"soloify/{name}")
    return path


def test_bootstrap_is_standalone_and_matches_source():
    assert (ROOT / "soloify-setup.py").read_bytes() == release.bootstrap()
    # An import with no installed soloify dependencies is sufficient for detection.
    assert b"from soloify" not in release.bootstrap()


def test_complete_release_has_only_allowed_artifacts_and_checksums(tmp_path, wheel):
    relay = tmp_path / "q2-soloify"
    relay.write_bytes(mips_binary())
    out = tmp_path / "release"
    release.prepare(out, wheel, relay=relay)
    assert len(list(out.glob("*.zip"))) == 1
    for line in (out / "SHA256SUMS").read_text().splitlines():
        checksum, name = line.split()
        assert hashlib.sha256((out / name).read_bytes()).hexdigest() == checksum
    files = setup.card_files((out / f"soloify-card-{__version__}.zip").read_bytes())
    assert set(files) == {"q2-soloify", "run", "aplay.sh"}
    assert files["q2-soloify"] == relay.read_bytes()
    with zipfile.ZipFile(out / f"soloify-card-{__version__}.zip") as card:
        assert card.getinfo(".soloify/q2-soloify").external_attr >> 16 & 0o111 == 0o111
        assert not any("remote" in n or "key" in n or "oauth" in n for n in card.namelist())


def test_release_rejects_host_binary_and_mismatching_wheel(tmp_path, wheel):
    relay = tmp_path / "host"
    relay.write_bytes(b"\x7fELF\x02\x01" + b"\0" * 64)
    with pytest.raises(ValueError, match="MIPS"):
        release.prepare(tmp_path / "out", wheel, relay=relay)
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("secret.key", "PRIVATE")
    with pytest.raises(ValueError, match="Unexpected wheel"):
        release.versions(wheel)


def test_card_package_rejects_secret_and_version_mismatch():
    for version, extra in [("old", False), (__version__, True)]:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as card:
            for name, content in {
                "VERSION": version,
                "q2-soloify": mips_binary(),
                "run": b"sh",
                "aplay.sh": b"sh",
            }.items():
                card.writestr(f".soloify/{name}", content)
            if extra:
                card.writestr(".soloify/remote", "PRIVATE")
        with pytest.raises(ValueError):
            setup.card_files(buf.getvalue())


def test_asset_checksum_failure_explains_how_to_retry(tmp_path, monkeypatch, capsys):
    from soloify import setup_bootstrap as boot

    artifact = b"downloaded artifact"

    class Response(io.BytesIO):
        pass

    def download(url, **kw):
        return Response(
            ("0" * 64 + "  test.whl\n").encode() if url.endswith("SHA256SUMS") else artifact
        )

    monkeypatch.setattr(boot.urllib.request, "urlopen", download)
    probes(monkeypatch)
    monkeypatch.setattr(boot, "provision", lambda *a: boot.release_asset("test.whl"))

    assert boot.main(["--root", str(tmp_path), "--selected", "docker"]) == 2
    text = capsys.readouterr().out
    assert "checksum mismatch for test.whl" in text
    assert "Rerun setup to download a fresh copy" in text
    assert "downloaded artifact" not in text
