"""winsys.disk: the Mac's free-space rules (500 MB to start, 200 MB to keep recording, decimal MB)."""
import shutil

from timmy.winsys import disk


def test_constants_match_the_mac():
    assert disk.MB == 1_000_000
    assert disk.MIN_START_BYTES == 500 * disk.MB
    assert disk.MIN_KEEP_BYTES == 200 * disk.MB
    assert disk.CHECK_EVERY_S == 20


def test_fake_free_space(monkeypatch, tmp_path):
    monkeypatch.setenv("TIMMY_FAKE_FREE_MB", "499")
    assert disk.free_bytes(tmp_path) == 499 * disk.MB
    assert not disk.can_start(tmp_path)
    assert not disk.must_stop(tmp_path)
    monkeypatch.setenv("TIMMY_FAKE_FREE_MB", "500")
    assert disk.can_start(tmp_path)
    monkeypatch.setenv("TIMMY_FAKE_FREE_MB", "199")
    assert disk.must_stop(tmp_path)


def test_real_free_space_of_a_folder_that_does_not_exist_yet(monkeypatch, tmp_path):
    """The recordings folder may not exist before the first call: measure its nearest existing parent."""
    monkeypatch.delenv("TIMMY_FAKE_FREE_MB", raising=False)
    missing = tmp_path / "טימי - הקלטות" / "sub"
    assert disk.existing_parent(missing) == str(tmp_path)
    free = disk.free_bytes(missing)
    assert abs(free - shutil.disk_usage(tmp_path).free) < 200 * disk.MB


def test_bad_fake_value_is_ignored(monkeypatch, tmp_path):
    monkeypatch.setenv("TIMMY_FAKE_FREE_MB", "lots")
    assert disk.free_bytes(tmp_path) > 0
