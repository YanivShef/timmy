"""The audio frame protocol and the worker's JSON-lines events: byte-for-byte the same as mac/live.py.

Frame (capture -> worker stdin):
    [1 byte channel: 0 = Chrome (other side), 1 = mic (owner)]
    [4 bytes little-endian uint32: sample count n]
    [n * float32 little-endian samples, 16 kHz mono]
Unknown channels are skipped (live.py does the same). EOF ends the call.

Events (worker stdout): one JSON object per line, field "type" in EVENT_TYPES.
"""
import json
import struct

import numpy as np

SR = 16000
CH_CHROME = 0
CH_MIC = 1
HEADER = struct.Struct("<BI")  # channel, sample count
MAX_SAMPLES = SR * 60           # sanity cap per frame (a frame is normally 10 to 100 ms)

EVENT_TYPES = frozenset({
    "loading", "ready", "partial", "final", "bullets", "saving", "done", "diskfull", "restyled",
})


def encode_frame(channel: int, samples) -> bytes:
    a = np.ascontiguousarray(samples, dtype="<f4").reshape(-1)
    return HEADER.pack(int(channel) & 0xFF, a.size) + a.tobytes()


class FrameDecoder:
    """Incremental decoder: feed() any byte chunks, get back complete (channel, float32 array) frames."""

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data: bytes):
        self._buf += data
        out = []
        while len(self._buf) >= HEADER.size:
            ch, n = HEADER.unpack_from(self._buf, 0)
            if n > MAX_SAMPLES:
                raise ValueError(f"frame too large: {n} samples")
            end = HEADER.size + n * 4
            if len(self._buf) < end:
                break
            a = np.frombuffer(bytes(self._buf[HEADER.size:end]), dtype="<f4").astype(np.float32)
            del self._buf[:end]
            if ch in (CH_CHROME, CH_MIC):
                out.append((ch, a))
        return out

    @property
    def pending_bytes(self) -> int:
        return len(self._buf)


def read_frames(stream):
    """Blocking generator over a binary stream (like live.py's reader thread). Stops at EOF or a truncated frame."""

    def readn(n):
        b = b""
        while len(b) < n:
            d = stream.read(n - len(b))
            if not d:
                return None
            b += d
        return b

    while True:
        h = readn(HEADER.size)
        if h is None:
            return
        ch, n = HEADER.unpack(h)
        d = readn(n * 4)
        if d is None:
            return
        if ch not in (CH_CHROME, CH_MIC):
            continue
        yield ch, np.frombuffer(d, dtype="<f4").astype(np.float32)


def parse_event(line: str):
    """One stdout line -> dict, or None for non-JSON noise (warnings from libraries)."""
    line = (line or "").strip()
    if not line.startswith("{"):
        return None
    try:
        o = json.loads(line)
    except ValueError:
        return None
    return o if isinstance(o, dict) and o.get("type") in EVENT_TYPES else None


def emit(stream, obj: dict) -> None:
    stream.write(json.dumps(obj, ensure_ascii=False) + "\n")
    stream.flush()
