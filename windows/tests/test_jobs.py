"""winsys.jobs: children without console windows, killed together with everything they started.

On Windows this is the Job Object path (CREATE_SUSPENDED -> AssignProcessToJobObject -> NtResumeProcess,
TerminateJobObject); elsewhere the same API uses a new session + killpg. The tests are the same on both.
"""
import subprocess
import sys
import time

import pytest

from timmy.winsys import jobs

PY = sys.executable
# a child that starts a grandchild, prints the grandchild's pid and then waits
TREE = ("import subprocess, sys, time; "
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']); "
        "print(g.pid, flush=True); time.sleep(120)")


def wait_dead(pid, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if not jobs.pid_alive(pid):
            return True
        time.sleep(0.1)
    return False


def test_run_passes_stdin_as_utf8_and_returns_stdout():
    code = "import sys; d = sys.stdin.buffer.read().decode('utf-8'); sys.stdout.buffer.write(d[::-1].encode('utf-8'))"
    rc, out = jobs.run([PY, "-c", code], "שלום עולם", timeout=30)
    assert rc == 0
    assert out == "םלוע םולש"


def test_run_default_cwd_is_the_temp_folder(tmp_path):
    import os
    import tempfile
    rc, out = jobs.run([PY, "-c", "import os; print(os.getcwd())"], "", timeout=30)
    assert rc == 0
    assert os.path.realpath(out.strip()) == os.path.realpath(tempfile.gettempdir())


def test_run_nonzero_exit_still_returns_stdout():
    rc, out = jobs.run([PY, "-c", "print('partial'); raise SystemExit(3)"], "", timeout=30)
    assert rc == 3 and out.strip() == "partial"


def test_run_missing_program_does_not_raise(tmp_path):
    assert jobs.run([str(tmp_path / "no-such-program.exe")], "x", timeout=5) == (None, "")


def test_run_timeout_kills_the_whole_tree():
    """Like `claude -p` (claude.cmd -> node.exe) hanging: the timeout must also kill the grandchild."""
    t0 = time.time()
    # the child prints the grandchild pid first, so the partial stdout tells us whom to check
    rc, out = jobs.run([PY, "-c", TREE], "", timeout=3)
    assert rc is None
    assert time.time() - t0 < 30
    gpid = int(out.split()[0])
    assert wait_dead(gpid), f"grandchild {gpid} survived the timeout"


def test_job_kill_kills_children_and_grandchildren():
    with jobs.Job() as job:
        p = job.popen([PY, "-c", TREE], stdout=subprocess.PIPE)
        gpid = int(p.stdout.readline())
        assert jobs.pid_alive(p.pid) and jobs.pid_alive(gpid)
        job.kill()
        p.wait(10)
        assert wait_dead(gpid)


def test_job_exit_kills_leftovers():
    """Leaving the `with` block (the UI quitting, a cancelled call) kills what is still running."""
    with jobs.Job() as job:
        p = job.popen([PY, "-c", TREE], stdout=subprocess.PIPE)
        gpid = int(p.stdout.readline())
    p.wait(10)
    assert wait_dead(gpid)


def test_no_window_flags():
    if sys.platform == "win32":
        assert jobs.no_window() == {"creationflags": jobs.CREATE_NO_WINDOW}
    else:
        assert jobs.no_window() == {}


def test_pid_alive():
    import os
    assert jobs.pid_alive(os.getpid())
    assert not jobs.pid_alive(0)
    p = subprocess.Popen([PY, "-c", "pass"])
    p.wait()
    assert wait_dead(p.pid, 5)


@pytest.mark.windows_only
def test_windows_job_handle_and_priority():
    job = jobs.Job()
    assert job.handle
    p = job.popen([PY, "-c", "import time; time.sleep(30)"])
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32")
        k32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        inside = wintypes.BOOL()
        assert k32.IsProcessInJob(int(p._handle), job.handle, ctypes.byref(inside))
        assert inside.value, "child is not in the job"
    finally:
        job.kill()
        job.close()
    p.wait(10)
    # BELOW_NORMAL priority (the worker on CPU): in a child, so the test runner keeps its own priority
    code = ("from timmy.winsys import jobs; import ctypes; assert jobs.lower_priority(); "
            "print(ctypes.windll.kernel32.GetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess()))")
    from tests.conftest import WIN
    r = subprocess.run([PY, "-c", code], cwd=WIN, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert int(r.stdout.strip()) == jobs.BELOW_NORMAL_PRIORITY_CLASS
