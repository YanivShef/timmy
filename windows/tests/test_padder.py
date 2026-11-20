"""padder.py: one wall-clock timeline per channel, silence where nothing played, real audio never dropped."""
import numpy as np

from timmy.audio.padder import SR, Feed, SourceClock, Timeline, level
from timmy.audio.resample import Converter, Format


def ones(n, v=0.5):
    return np.full(n, v, np.float32)


def test_take_pads_silence_when_nothing_arrived():
    tl = Timeline()
    assert tl.take(0) is None
    a = tl.take(1600)
    assert len(a) == 1600 and not a.any()
    assert tl.written == 1600 and tl.take(1600) is None


def test_add_take_positions_and_mixing():
    tl = Timeline()
    tl.add(100, ones(50, 0.25))
    tl.add(120, ones(50, 0.5))                      # a second Chrome process playing at the same time: summed
    out = tl.take(200)
    assert not out[:100].any()
    assert np.allclose(out[100:120], 0.25) and np.allclose(out[120:150], 0.75) and np.allclose(out[150:170], 0.5)
    assert not out[170:].any()
    assert tl.end == 200


def test_mix_is_clipped():
    tl = Timeline()
    tl.add(0, ones(10, 0.8))
    tl.add(0, ones(10, 0.8))
    assert np.all(tl.take(10) == 1.0)


def test_late_audio_is_shifted_not_dropped():
    tl = Timeline()
    tl.take(1000)
    pos = tl.add(500, ones(300))
    assert pos == 1000 and tl.late_samples == 500
    out = tl.take(1300)
    assert np.allclose(out, 0.5)


def test_absurd_future_position_is_capped():
    tl = Timeline()
    pos = tl.add(10 ** 12, ones(10))
    assert pos == 0


def test_source_clock_contiguous_gap_and_no_backwards():
    c = SourceClock(t0=100.0)
    assert c.place(100.0, 160) == 0
    assert c.place(100.0 + 160 / SR + 0.05, 160) == 160          # 50 ms jitter: stays contiguous
    assert c.place(100.0 + 0.5, 160) == 8000                      # 0.5 s with nothing: a gap
    assert c.gaps == 1
    assert c.place(100.0 + 0.2, 160) == 8160                      # a timestamp in the past never overlaps our audio
    c.reset()
    assert c.place(103.0, 10) == 3 * SR


def test_feed_mic_switch_keeps_timeline():
    """Mic at 48 kHz for 1 s, device gone 0.7 s, new device at 16 kHz: the second part starts at 1.7 s."""
    tl = Timeline()
    feed = Feed(tl, t0=0.0, converter=Converter(Format(48000, 2, 16, False)))
    pkt = 480                                                      # 10 ms
    sig = (np.ones((pkt, 2)) * 0.5 * 32767).astype("<i2").tobytes()
    for i in range(100):
        feed.push(i * 0.01, sig, pkt)
    feed.end_stream()
    feed.set_converter(Converter(Format(16000, 1, 32, True)))
    for i in range(50):
        feed.push(1.7 + i * 0.01, ones(160, -0.5).tobytes(), 160)
    out = tl.take(int(2.5 * SR))
    first = out[: int(0.98 * SR)]
    assert np.mean(np.abs(first - 0.5) < 0.02) > 0.97              # resampled 48 kHz part, level preserved
    gap = out[int(1.02 * SR): int(1.69 * SR)]
    assert not gap.any()
    second = out[int(1.7 * SR): int(2.2 * SR)]
    assert np.allclose(second, -0.5)
    assert not out[int(2.21 * SR):].any()


def test_feed_never_drops_samples_under_jitter():
    """Packets arrive with random timing jitter (< tolerance): every sample is written, in order, contiguous."""
    rng = np.random.default_rng(3)
    tl = Timeline()
    feed = Feed(tl, t0=0.0)
    src = rng.standard_normal(SR * 3).astype(np.float32) * 0.1
    pos = 0
    while pos < len(src):
        n = int(rng.integers(80, 800))
        t = pos / SR + float(rng.uniform(-0.05, 0.15))
        feed.push_samples(t, src[pos:pos + n])
        pos += n
    out = tl.take(tl.end)
    start = int(np.flatnonzero(out)[0])
    assert np.array_equal(out[start:start + len(src)], src)
    assert feed.clock.gaps == 0


def test_two_channels_stay_aligned_when_one_is_quiet():
    """Chrome delivers nothing for 2 s (process loopback while Chrome is silent): both channels still advance."""
    tls = {0: Timeline(), 1: Timeline()}
    f0, f1 = Feed(tls[0], 0.0), Feed(tls[1], 0.0)
    for i in range(300):                         # 3 s of mic
        f1.push_samples(i * 0.01, ones(160, 0.1))
    for i in range(100):                         # Chrome: 0..1 s, then nothing, then 3..3.5 s
        f0.push_samples(i * 0.01, ones(160, 0.2))
    for i in range(50):
        f0.push_samples(3.0 + i * 0.01, ones(160, 0.2))
    upto = int(3.5 * SR)
    a, b = tls[0].take(upto), tls[1].take(upto)
    assert len(a) == len(b) == upto
    assert not a[int(1.01 * SR): int(2.99 * SR)].any()
    assert np.allclose(a[3 * SR: int(3.49 * SR)], 0.2)


def test_level_mapping():
    assert level(0.0) == 0.0
    assert level(1.0) == 1.0
    assert 0.3 < level(0.03) < 0.5
