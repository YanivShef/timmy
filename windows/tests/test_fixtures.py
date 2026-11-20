import io
import wave
from pathlib import Path

import numpy as np
import pytest

from timmy.engine import protocol as P


@pytest.mark.parametrize("name", ["he_short.wav", "he_reply.wav"])
def test_hebrew_fixture_is_16k_mono(name):
    """Synthetic TTS voice (macOS "Carmit"), no real person. he_short.wav: the other side asks for a meeting;
    he_reply.wav: the owner answers (the mic side in the worker tests)."""
    with wave.open(str(Path(__file__).parent / "fixtures" / name)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
        a = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
    assert 3.0 < len(a) / 16000 < 10.0
    assert float(np.sqrt((a ** 2).mean())) > 0.01
    # the worker input: the same audio framed in 100 ms frames, read back intact
    blob = b"".join(P.encode_frame(P.CH_CHROME, a[i:i + 1600]) for i in range(0, len(a), 1600))
    back = np.concatenate([x for _, x in P.read_frames(io.BytesIO(blob))])
    assert np.array_equal(back, a)
