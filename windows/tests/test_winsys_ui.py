"""Windows system pieces of build step 3: screen-share hiding (WDA_EXCLUDEFROMCAPTURE), launch at login
(HKCU Run key) and the single instance."""
import json
import os
import subprocess
import sys

import pytest

from tests.conftest import WIN, wait_until
from timmy.winsys import affinity, startup


def test_affinity_is_a_noop_off_windows():
    if sys.platform == "win32":
        pytest.skip("Windows has the real thing")
    assert affinity.set_hidden(1234, True) is False and affinity.get(1234) is None and not affinity.supported()


@pytest.mark.windows_only
def test_affinity_hides_window_from_capture(out_dir):
    """A red window with the affinity and a blue one without: the affinity reads back as 0x11, and when this runner
    has an interactive desktop, the screenshot has the blue window but not the red one."""
    shot = out_dir / "screens" / "affinity-desktop.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONPATH": str(WIN)}
    env.pop("QT_QPA_PLATFORM", None)
    r = subprocess.run([sys.executable, str(WIN / "tests" / "affinity_probe.py"), str(shot)], capture_output=True, text=True,
                       timeout=120, env=env, cwd=WIN)
    print(r.stdout, r.stderr[-2000:])
    res = json.loads(r.stdout.strip().splitlines()[-1])
    assert res["set_ok"] is True, res
    assert res["red_affinity"] == affinity.WDA_EXCLUDEFROMCAPTURE and res["blue_affinity"] == affinity.WDA_NONE
    assert res["red_affinity_after_show"] == affinity.WDA_NONE
    if res["blue_px"] < 50:
        pytest.skip(f"no interactive desktop to screenshot on this runner: {res}")
    assert res["red_px"] < res["blue_px"] / 20, res


def test_startup_command_line(tmp_path):
    exe = tmp_path / "venv" / "Scripts" / "python.exe"
    assert startup.command_line(str(exe)) == f'"{tmp_path / "venv" / "Scripts" / "pythonw.exe"}" -m timmy --background'
    assert startup.interpreter("/x/pythonw.exe") == "/x/pythonw.exe"


@pytest.mark.windows_only
def test_startup_registry_roundtrip():
    name = f"TimmyTest{os.getpid()}"            # never the user's real "Timmy" value
    try:
        assert startup.get(name) is None
        assert startup.enable(name, r'"C:\x\pythonw.exe" -m timmy --background')
        assert startup.get(name) == r'"C:\x\pythonw.exe" -m timmy --background' and startup.is_enabled(name)
        assert startup.disable(name) and startup.get(name) is None
        assert startup.disable(name)               # already gone: still fine
    finally:
        startup.disable(name)


_ALIVE = []


def test_single_instance(qapp):
    """The second launch is another process (as in real use): it sends "show" and waits for the running Timmy's "ok",
    while this one runs its event loop. A second instance in this process sees the first one too."""
    import subprocess
    import sys

    from tests.conftest import WIN
    from timmy.winsys.instance import SingleInstance, send_to_running
    name = f"timmy-test-instance-{os.getpid()}"
    first = SingleInstance(name)
    _ALIVE.append(first)                   # the app keeps its one instance for the whole run; never freed mid loop here
    got = []
    first.message.connect(got.append)
    try:
        assert first.listen() is True
        code = f"from timmy.winsys.instance import send_to_running; print(send_to_running('show', {name!r}))"
        p = subprocess.Popen([sys.executable, "-c", code], cwd=WIN, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             env={**os.environ, "PYTHONPATH": str(WIN)})
        assert wait_until(qapp, lambda: got == ["show"] and p.poll() is not None, 20), (got, p.poll())
        out, err = p.communicate(timeout=10)
        assert out.strip() == "True", err
        second = SingleInstance(name)
        _ALIVE.append(second)
        assert second.listen() is False    # (in-process: no "ok" can come back while this thread waits; still True)
    finally:
        first.close()
    assert send_to_running("show", name, timeout_ms=300) is False
