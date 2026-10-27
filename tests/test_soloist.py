import asyncio
import datetime
import io
import tarfile

import pytest

from soloify import soloist


def _tarball(date: str, exit_code: int = 0) -> bytes:
    """A stand-in build: --version says it was built on date; otherwise it exits exit_code."""
    script = (
        "#!/bin/sh\n"
        f'[ "$1" = --version ] && echo "soloist 1.3.10.16 build 1 ({date}) (g0) (linux/x86_64)"'
        f" && exit 0\nexit {exit_code}\n"
    ).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in (("./soloist", script), ("./CHANGELOG.md", b"## Release v1.3.10\n")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _days_ago(n: int) -> str:
    return (datetime.date.today() - datetime.timedelta(days=n)).strftime("%Y%m%d")


@pytest.fixture
def downloads(monkeypatch):
    """What the next downloads serve, and the URLs asked for."""
    served = []
    urls = []

    def download(url):
        urls.append(url)
        if not served:
            raise OSError("offline")
        return served.pop(0)

    monkeypatch.setattr(soloist, "_download", download)
    monkeypatch.setattr(soloist, "target", lambda: "x86_64")
    return served, urls


def _solo(tmp_path):
    return soloist.Soloist(tmp_path, tmp_path / "key", launcher="")


def test_build_date_and_target():
    v = "soloist 1.3.10.16 build 1793016086 (20261026) (g19412df6ea) (linux/x86_64)"
    assert soloist.build_date(v) == datetime.date(2026, 10, 26)
    assert soloist.build_date("nonsense") is None
    assert soloist.TARGETS["aarch64"] == "arm64"


async def test_fetches_when_missing(tmp_path, downloads):
    served, urls = downloads
    served.append(_tarball(_days_ago(1)))
    solo = _solo(tmp_path)
    assert await solo.ensure()
    assert urls == [soloist.URL.format("x86_64")]
    assert solo.binary.stat().st_mode & 0o111
    assert (tmp_path / "soloist/CHANGELOG.md").exists()
    assert await solo.days_left() == 89
    # Fresh enough: no download
    assert not await solo.ensure()
    assert len(urls) == 1


async def test_refreshes_close_to_expiry_and_keeps_the_old_build_offline(tmp_path, downloads):
    served, urls = downloads
    served.append(_tarball(_days_ago(80)))
    solo = _solo(tmp_path)
    await solo.ensure()
    assert await solo.days_left() == 10
    assert not await solo.ensure()  # offline: the old build stays
    assert await solo.days_left() == 10
    served.append(_tarball(_days_ago(0)))
    assert await solo.ensure()
    assert await solo.days_left() == 90


async def test_missing_and_offline_fails(tmp_path, downloads):
    with pytest.raises(OSError):
        await _solo(tmp_path).ensure()


async def test_an_expired_build_is_fetched_again_at_once(tmp_path, downloads, monkeypatch):
    served, urls = downloads
    served += [_tarball(_days_ago(1), exit_code=soloist.EXPIRED), _tarball(_days_ago(0))]
    (tmp_path / "key").write_text("k\n")
    solo = _solo(tmp_path)

    async def no_pipewire():
        return False

    monkeypatch.setattr(soloist, "pipewire", no_pipewire)
    runs = []
    once = solo._once

    async def counted():
        runs.append(await solo.version())
        if len(runs) == 2:
            raise asyncio.CancelledError
        return await once()

    monkeypatch.setattr(solo, "_once", counted)
    with pytest.raises(asyncio.CancelledError):
        await solo.run()
    assert len(urls) == 2
    assert runs[1] != runs[0]
