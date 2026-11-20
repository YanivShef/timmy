"""resample.py: every buffer layout WASAPI can hand us -> 16 kHz mono float32."""
import numpy as np
import pytest

from timmy.audio.resample import SR, Converter, Format, Resampler, to_mono


def tone(rate, seconds=1.0, freq=440.0, amp=0.5):
    t = np.arange(int(rate * seconds)) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def encode(mono, fmt: Format):
    """mono float -> interleaved bytes in fmt (all channels equal)."""
    x = np.repeat(mono[:, None], fmt.channels, axis=1).reshape(-1)
    if fmt.is_float:
        return x.astype("<f4").tobytes()
    if fmt.bits == 16:
        return np.round(x * 32767).astype("<i2").tobytes()
    if fmt.bits == 32:
        return np.round(x.astype(np.float64) * 2147483647).astype("<i4").tobytes()
    if fmt.bits == 24:
        v = np.round(x.astype(np.float64) * 8388607).astype(np.int32)
        b = np.stack([v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF], axis=1).astype(np.uint8)
        return b.tobytes()
    raise ValueError(fmt)


def peak_freq(a, rate):
    spec = np.abs(np.fft.rfft(a * np.hanning(len(a))))
    return np.argmax(spec) * rate / len(a)


@pytest.mark.parametrize("bits,is_float", [(16, False), (24, False), (32, False), (32, True)])
@pytest.mark.parametrize("channels", [1, 2, 6, 8])
def test_to_mono_all_layouts(bits, is_float, channels):
    fmt = Format(48000, channels, bits, is_float)
    src = tone(48000, 0.05)
    got = to_mono(encode(src, fmt), fmt)
    assert got.dtype == np.float32 and len(got) == len(src)
    assert np.max(np.abs(got - src)) < 2e-4


def test_to_mono_averages_channels_and_respects_frames():
    fmt = Format(16000, 2, 16, False)
    left, right = np.full(100, 0.5, np.float32), np.full(100, -0.25, np.float32)
    data = np.round(np.stack([left, right], 1).reshape(-1) * 32767).astype("<i2").tobytes()
    got = to_mono(data, fmt, frames=60)
    assert len(got) == 60 and np.allclose(got, 0.125, atol=1e-4)
    assert len(to_mono(b"", fmt)) == 0
    assert len(to_mono(data[:3], fmt)) == 0          # less than one frame


def test_24_bit_negative_values_sign_extend():
    fmt = Format(16000, 1, 24, False)
    got = to_mono(bytes([0x00, 0x00, 0x80, 0xFF, 0xFF, 0x7F]), fmt)   # -8388608, +8388607
    assert got[0] == -1.0 and abs(got[1] - 1.0) < 1e-6


@pytest.mark.parametrize("rate", [8000, 11025, 16000, 22050, 32000, 44100, 48000, 88200, 96000])
def test_resampler_rates_length_and_pitch(rate):
    src = tone(rate, 2.0, freq=1000.0)
    r = Resampler(rate)
    out = np.concatenate([r.process(src[i:i + 441]) for i in range(0, len(src), 441)] + [r.flush()])
    assert abs(len(out) - 2 * SR) <= 2, len(out)
    mid = out[SR // 2: SR // 2 + 8192]
    assert abs(peak_freq(mid, SR) - 1000.0) < 5
    assert 0.45 < np.max(np.abs(mid)) < 0.55
    assert r.identity == (rate == SR)


def test_resampler_chunking_does_not_matter():
    src = tone(44100, 1.0, freq=300)
    one = Resampler(44100)
    a = np.concatenate([one.process(src), one.flush()])
    many = Resampler(44100)
    rng = np.random.default_rng(1)
    parts, i = [], 0
    while i < len(src):
        n = int(rng.integers(1, 2000))
        parts.append(many.process(src[i:i + n]))
        i += n
    b = np.concatenate(parts + [many.flush()])
    assert len(a) == len(b)
    assert np.max(np.abs(a - b)) < 1e-4


def test_downsampling_removes_content_above_8k():
    """An 11 kHz tone at 48 kHz must not alias into the 16 kHz output (Whisper would hear a fake 5 kHz tone)."""
    out = Resampler(48000).process(tone(48000, 1.0, freq=11000.0))
    assert np.sqrt(np.mean(out[2000:] ** 2)) < 0.01


def test_converter_silent_flag_and_reset():
    fmt = Format(48000, 2, 16, False)
    c = Converter(fmt)
    data = encode(tone(48000, 0.1), fmt)
    a = c.process(data, 4800)
    s = c.process(b"garbage", 4800, silent=True)
    assert len(s) > 0 and np.all(s == 0) is not None
    tail = c.flush()
    assert abs(len(a) + len(s) + len(tail) - 3200) <= 2
    c.reset()
    assert len(c.process(data, 4800)) > 0


def test_identity_converter_is_exact():
    fmt = Format(16000, 1, 32, True)
    src = tone(16000, 0.2)
    c = Converter(fmt)
    assert np.array_equal(c.process(src.tobytes(), len(src)), src)
    assert len(c.flush()) == 0
