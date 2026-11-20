import io
import json
import re
import struct

import numpy as np
import pytest

from timmy.engine import protocol as P


def test_frame_layout_is_byte_exact():
    b = P.encode_frame(1, np.array([0.5, -1.0], np.float32))
    assert b[0] == 1
    assert struct.unpack("<I", b[1:5])[0] == 2
    assert np.frombuffer(b[5:], "<f4").tolist() == [0.5, -1.0]
    assert len(b) == 5 + 2 * 4


def test_roundtrip_stream_and_unknown_channel_skipped():
    a = np.linspace(-1, 1, 1600, dtype=np.float32)
    data = P.encode_frame(0, a) + P.encode_frame(7, a[:10]) + P.encode_frame(1, a[::2])
    got = list(P.read_frames(io.BytesIO(data)))
    assert [c for c, _ in got] == [0, 1]
    assert np.array_equal(got[0][1], a) and np.array_equal(got[1][1], a[::2])


def test_truncated_tail_stops_cleanly():
    data = P.encode_frame(0, np.ones(100, np.float32))
    assert list(P.read_frames(io.BytesIO(data[:-3]))) == []


def test_incremental_decoder_any_chunking():
    rng = np.random.default_rng(0)
    frames = [(int(rng.integers(0, 2)), rng.standard_normal(int(rng.integers(1, 800))).astype(np.float32)) for _ in range(40)]
    blob = b"".join(P.encode_frame(c, a) for c, a in frames)
    dec, got, i = P.FrameDecoder(), [], 0
    while i < len(blob):
        step = int(rng.integers(1, 97))
        got += dec.feed(blob[i:i + step])
        i += step
    assert dec.pending_bytes == 0
    assert len(got) == len(frames)
    for (c1, a1), (c2, a2) in zip(frames, got):
        assert c1 == c2 and np.array_equal(a1, a2)


def test_decoder_rejects_absurd_frame():
    with pytest.raises(ValueError):
        P.FrameDecoder().feed(struct.pack("<BI", 0, P.MAX_SAMPLES + 1))


def test_events_parse_and_noise_ignored():
    assert P.parse_event('{"type": "final", "text": "שלום", "t": "00:00:01", "speaker": "me"}')["text"] == "שלום"
    assert P.parse_event("UserWarning: something") is None
    assert P.parse_event('{"type": "nope"}') is None
    buf = io.StringIO()
    P.emit(buf, {"type": "ready"})
    assert json.loads(buf.getvalue()) == {"type": "ready"}


def test_matches_mac_worker(repo_root):
    """Drift guard: the Mac worker must still speak the same protocol (16 kHz, 1-byte channel + LE uint32 count)."""
    src = (repo_root / "mac" / "live.py").read_text(encoding="utf-8")
    assert re.search(r"^SR = 16000$", src, re.M)
    assert 'struct.unpack("<I", h[1:])' in src and "readn(5)" in src and "np.float32" in src
    for t in ("loading", "ready", "partial", "final", "bullets", "saving", "done", "diskfull", "restyled"):
        assert f'"type": "{t}"' in src, t
