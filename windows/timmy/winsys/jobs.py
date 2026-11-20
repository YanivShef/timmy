"""Child processes that never show a console window and can be killed with everything they started.

The Windows twin of the Mac code's `start_new_session=True` + `os.killpg(...)`: every child is started suspended,
put into a Job Object (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE) and only then resumed, so even a grandchild it starts
in its first microsecond (npm's claude.cmd -> node.exe -> ...) belongs to the job. `Job.kill()` =
TerminateJobObject = the whole tree at once. Closing the job handle (or this process dying) kills it too.

Off Windows (dev machines, the Mac CI of this repo) the same API uses a new session + killpg, so the callers and
the tests are identical on every OS.
"""
import os
import signal
import subprocess
import sys
import tempfile
import threading

IS_WIN = sys.platform == "win32"

CREATE_SUSPENDED = 0x00000004
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000

JobObjectExtendedLimitInformation = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


def no_window() -> dict:
    """Popen kwargs that keep a console window from flashing up (pythonw has no console, so every console child
    would otherwise get a new one)."""
    return {"creationflags": CREATE_NO_WINDOW} if IS_WIN else {}


if IS_WIN:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")

    class _BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class _IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                   "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BASIC), ("IoInfo", _IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.SetInformationJobObject.restype = wintypes.BOOL
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _k32.AssignProcessToJobObject.restype = wintypes.BOOL
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.TerminateJobObject.restype = wintypes.BOOL
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.GetCurrentProcess.restype = wintypes.HANDLE
    _k32.SetPriorityClass.restype = wintypes.BOOL
    _k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.GetExitCodeProcess.restype = wintypes.BOOL
    _k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _ntdll.NtResumeProcess.restype = ctypes.c_long
    _ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]


class Job:
    """A group of child processes that dies together.

    with Job() as job:
        p = job.popen([...], stdin=PIPE, stdout=PIPE)
        ...
        job.kill()          # p and everything p started
    """

    def __init__(self):
        self._procs = []
        self._lock = threading.Lock()
        self.handle = None
        if IS_WIN:
            h = _k32.CreateJobObjectW(None, None)
            if not h:
                raise ctypes.WinError(ctypes.get_last_error())
            info = _EXTENDED()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not _k32.SetInformationJobObject(h, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)):
                err = ctypes.get_last_error()
                _k32.CloseHandle(h)
                raise ctypes.WinError(err)
            self.handle = h

    def popen(self, cmd, **kw) -> subprocess.Popen:
        if IS_WIN:
            flags = kw.pop("creationflags", 0) | CREATE_NO_WINDOW | CREATE_SUSPENDED
            p = subprocess.Popen(cmd, creationflags=flags, **kw)
            try:
                if not _k32.AssignProcessToJobObject(self.handle, int(p._handle)):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                # resume in every case: a child left suspended would hang its caller forever
                _ntdll.NtResumeProcess(int(p._handle))
        else:
            kw.setdefault("start_new_session", True)
            p = subprocess.Popen(cmd, **kw)
        with self._lock:
            self._procs.append(p)
        return p

    def kill(self) -> None:
        if IS_WIN:
            if self.handle:
                _k32.TerminateJobObject(self.handle, 1)
        else:
            with self._lock:
                procs = list(self._procs)
            # a Windows job also holds what its members started in jobs of their own (the worker runs claude in a
            # nested job); here those are other sessions, so collect every descendant first, then kill
            pids = set()
            for p in procs:
                pids.add(p.pid)
                try:
                    import psutil
                    pids.update(c.pid for c in psutil.Process(p.pid).children(recursive=True))
                except Exception:
                    pass
            for p in procs:
                try:
                    os.killpg(p.pid, signal.SIGKILL)   # the child is its own session leader: pgid == pid
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
        with self._lock:
            procs = list(self._procs)
        for p in procs:
            try:
                p.kill()
            except OSError:
                pass

    def close(self) -> None:
        """Windows: closing the last handle kills whatever is still in the job (KILL_ON_JOB_CLOSE)."""
        if IS_WIN and self.handle:
            _k32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.kill()
        self.close()
        return False

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def run(cmd, input_text: str = "", timeout: float = 60, cwd=None, env=None):
    """Run `cmd` with `input_text` (UTF-8) on stdin, no console window, in its own Job.
    Returns (returncode, stdout text). On timeout the whole tree is killed and (None, partial stdout) comes back.
    Never raises for a child that cannot start: (None, "")."""
    try:
        job = Job()
    except OSError:
        return None, ""
    try:
        try:
            p = job.popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          cwd=cwd if cwd is not None else tempfile.gettempdir(), env=env)
        except OSError:
            return None, ""
        try:
            out, _ = p.communicate(input=(input_text or "").encode("utf-8"), timeout=timeout)
        except subprocess.TimeoutExpired:
            job.kill()
            try:
                out, _ = p.communicate(timeout=5)
            except Exception:
                out = b""
            return None, (out or b"").decode("utf-8", "replace")
        return p.returncode, (out or b"").decode("utf-8", "replace")
    finally:
        job.kill()     # grandchildren left behind by a child that already exited
        job.close()


def lower_priority() -> bool:
    """This process to BELOW_NORMAL (Windows), so CPU transcription never makes the call itself stutter."""
    if not IS_WIN:
        return False
    return bool(_k32.SetPriorityClass(_k32.GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS))


def pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if IS_WIN:
        h = _k32.OpenProcess(0x1000, False, int(pid))        # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = wintypes.DWORD()
            return bool(_k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259   # STILL_ACTIVE
        finally:
            _k32.CloseHandle(h)
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    # a zombie (exited, not reaped yet) still answers kill(0): ask ps whether it is really alive
    try:
        st = subprocess.run(["ps", "-o", "stat=", "-p", str(int(pid))], capture_output=True, text=True, timeout=5).stdout.strip()
        return bool(st) and not st.startswith("Z")
    except Exception:
        return True
