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
    with pytest.raises(RuntimeError, match="no usable Soloist replacement"):
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


@pytest.mark.parametrize("age,eligible", [(59, False), (60, True), (61, True)])
async def test_renewal_uses_build_age_not_download_time(tmp_path, downloads, age, eligible):
    served, urls = downloads
    solo = _solo(tmp_path)
    soloist.unpack(_tarball(_days_ago(age)), solo.home)
    served.append(_tarball(_days_ago(0)))
    assert await solo.ensure() is eligible
    assert bool(urls) is eligible
    status = await solo.status()
    assert status["days_left"] == (90 if eligible else 31)
    assert status["automatic_renewal"].startswith("on bridge startup")


@pytest.mark.parametrize("candidate", [b"broken", _tarball("20261399"), _tarball("29990101")])
async def test_malformed_candidates_leave_installed_files_untouched(tmp_path, downloads, candidate):
    served, _ = downloads
    solo = _solo(tmp_path)
    soloist.unpack(_tarball(_days_ago(60)), solo.home)
    before = {p.name: p.read_bytes() for p in solo.home.iterdir()}
    served.append(candidate)
    assert not await solo.ensure()
    assert {p.name: p.read_bytes() for p in solo.home.iterdir()} == before


@pytest.mark.parametrize("age", [60, 61, 90, 91])
async def test_identical_older_expired_builds_are_not_renewals(tmp_path, downloads, age, caplog):
    served, _ = downloads
    solo = _solo(tmp_path)
    soloist.unpack(_tarball(_days_ago(60)), solo.home)
    before = solo.binary.read_bytes()
    served.append(_tarball(_days_ago(age)))
    assert not await solo.ensure()
    assert solo.binary.read_bytes() == before
    assert await solo.days_left() == 30
    assert "30 days left; retry in 24h" in caplog.text


async def test_stages_during_playback_and_activates_without_touching_credentials(
    tmp_path, downloads
):
    from types import SimpleNamespace

    served, _ = downloads
    solo = _solo(tmp_path)
    soloist.unpack(_tarball(_days_ago(60)), solo.home)
    credentials = [
        tmp_path / "key",
        tmp_path / "oauth.json",
        tmp_path / "bridge/token",
        tmp_path / "bridge/key.pem",
        tmp_path / "soloist-state/.credentials",
    ]
    for path in credentials:
        path.parent.mkdir(exist_ok=True)
        path.write_text("private")
        path.chmod(0o600)
    before = solo.binary.read_bytes()
    solo.proc = SimpleNamespace(returncode=None)
    served.append(_tarball(_days_ago(0)))
    assert await solo.ensure()
    assert solo.stale and solo.pending.exists()
    assert solo.binary.read_bytes() == before
    solo.activate()
    assert await solo.days_left() == 90
    for path in credentials:
        assert path.read_text() == "private"
        assert path.stat().st_mode & 0o777 == 0o600


async def test_daily_retry_and_idle_restart(tmp_path, downloads, monkeypatch):
    from types import SimpleNamespace

    served, urls = downloads
    solo = _solo(tmp_path)
    soloist.unpack(_tarball(_days_ago(60)), solo.home)
    old = solo.binary.read_bytes()
    steps, terminated = [], []
    solo.proc = SimpleNamespace(returncode=None, terminate=lambda: terminated.append(True))
    idle = iter([False, True])

    async def clock(delay):
        steps.append(delay)
        if len(steps) == 1:
            # First daily check: offline. The following daily check gets a newer build.
            return
        if len(steps) == 2:
            served.append(_tarball(_days_ago(0)))
            return
        if delay == 60:
            assert solo.binary.read_bytes() == old
            return
        raise asyncio.CancelledError

    monkeypatch.setattr(soloist.asyncio, "sleep", clock)
    with pytest.raises(asyncio.CancelledError):
        await solo._refresh(lambda: next(idle))
    assert len(urls) == 2
    assert steps == [86400, 86400, 60, 86400]
    assert terminated == [True]
    assert await solo.days_left() == 90


async def test_startup_checks_before_starting_child(tmp_path, monkeypatch):
    solo = _solo(tmp_path)
    events = []

    async def check(force=False):
        events.append("check")

    async def once():
        events.append("child")
        raise asyncio.CancelledError

    monkeypatch.setattr(solo, "ensure", check)
    monkeypatch.setattr(solo, "_once", once)
    with pytest.raises(asyncio.CancelledError):
        await solo.run()
    assert events == ["check", "child"]


async def test_expired_exit_reports_no_replacement(tmp_path, downloads, monkeypatch):
    served, _ = downloads
    served += [_tarball(_days_ago(1), exit_code=soloist.EXPIRED)]
    (tmp_path / "key").write_text("private")
    solo = _solo(tmp_path)

    async def no_pipewire():
        return False

    monkeypatch.setattr(soloist, "pipewire", no_pipewire)
    with pytest.raises(RuntimeError, match="expired; no usable replacement"):
        await solo.run()


async def test_expired_startup_does_not_run_old_binary(tmp_path, downloads):
    solo = _solo(tmp_path)
    soloist.unpack(_tarball(_days_ago(90)), solo.home)
    with pytest.raises(RuntimeError, match="no usable Soloist replacement"):
        await solo.ensure()


async def test_container_path_binary_is_not_treated_as_nix_launcher(
    tmp_path, downloads, monkeypatch
):
    binary = tmp_path / "soloist/soloist"
    soloist.unpack(_tarball(_days_ago(60)), binary.parent)
    monkeypatch.setattr(soloist.shutil, "which", lambda name: str(binary))
    solo = soloist.Soloist(tmp_path, tmp_path / "key")
    served, _ = downloads
    served.append(_tarball(_days_ago(0)))
    assert await solo.ensure()
    assert await solo.days_left() == 90


async def test_valid_staged_replacement_survives_offline_restart(tmp_path, downloads):
    from types import SimpleNamespace

    served, urls = downloads
    solo = _solo(tmp_path)
    soloist.unpack(_tarball(_days_ago(60)), solo.home)
    solo.proc = SimpleNamespace(returncode=None)
    served.append(_tarball(_days_ago(0)))
    assert await solo.ensure()
    restarted = _solo(tmp_path)
    assert (await restarted.status())["activation_pending"]
    assert await restarted.ensure()
    assert await restarted.days_left() == 90
    assert len(urls) == 1  # use the validated staged copy, even when downloads are offline
