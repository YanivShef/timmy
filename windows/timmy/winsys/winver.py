"""Windows version gates.

Process loopback (capture only Chrome's audio) and WDA_EXCLUDEFROMCAPTURE (hide Timmy from screen sharing)
both need Windows 10 version 2004 (build 19041) or newer. Microsoft's reference page for
AUDIOCLIENT_ACTIVATION_PARAMS lists build 20348 (the SDK where the header first shipped), but the runtime
feature works from 19041 (it is what OBS's application audio capture requires). The capture code still
treats any activation failure as "unsupported" at runtime, so this gate is only the first check.
"""
import sys

MIN_BUILD = 19041          # Windows 10 2004
WIN11_BUILD = 22000


def current_build() -> int | None:
    if sys.platform != "win32":
        return None
    try:
        return int(sys.getwindowsversion().build)
    except Exception:
        return None


def loopback_supported(build: int | None = None) -> bool:
    b = current_build() if build is None else build
    return bool(b and b >= MIN_BUILD)


def exclude_from_capture_supported(build: int | None = None) -> bool:
    b = current_build() if build is None else build
    return bool(b and b >= MIN_BUILD)


def describe(build: int | None) -> str:
    """Short Hebrew label for onboarding / installer messages."""
    if not build:
        return "לא ווינדוס"
    if build >= WIN11_BUILD:
        return "Windows 11"
    if build >= MIN_BUILD:
        return "Windows 10 (2004 ומעלה)"
    return "Windows 10 ישן מדי (צריך עדכון 2004 ומעלה)"
