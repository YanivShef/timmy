"""Any WASAPI buffer layout -> 16 kHz mono float32, the worker's input format (DESIGN.md 5.3).

Format       what a capture stream delivers (rate, channels, container bits, float or int)
to_mono      interleaved bytes -> mono float32 in [-1, 1] (channel average)
Resampler    streaming soxr resampler (identity when the rate is already 16 kHz)
Converter    to_mono + Resampler for one stream; reset() after a discontinuity (new device, new stream)
"""
from dataclasses import dataclass

import numpy as np

SR = 16000


@dataclass(frozen=True)
class Format:
    rate: int
    channels: int
    bits: int            # container size per sample: 16, 24 or 32
    is_float: bool = False

    @property
    def block_align(self) -> int:
        return self.channels * self.bits // 8

    def __str__(self):
        return f"{self.rate} Hz, {self.channels} ch, {self.bits}-bit {'float' if self.is_float else 'int'}"

    def as_dict(self):
        return {"rate": self.rate, "channels": self.channels, "bits": self.bits, "float": self.is_float}


TARGET = Format(SR, 1, 32, True)


def to_mono(data, fmt: Format, frames: int | None = None) -> np.ndarray:
    """Interleaved PCM bytes -> mono float32. `frames` limits the length (extra trailing bytes are ignored)."""
    ch, bps = max(1, fmt.channels), fmt.bits // 8
    n = len(data) // (ch * bps) if frames is None else min(frames, len(data) // (ch * bps))
    if n <= 0:
        return np.zeros(0, np.float32)
    raw = memoryview(data)[: n * ch * bps]
    if fmt.is_float and bps == 4:
        x = np.frombuffer(raw, "<f4").astype(np.float32)
    elif fmt.is_float and bps == 8:
        x = np.frombuffer(raw, "<f8").astype(np.float32)
    elif bps == 2:
        x = np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0
    elif bps == 3:
        b = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = (v << 8) >> 8                                  # sign-extend 24 -> 32 bit
        x = v.astype(np.float32) / 8388608.0
    elif bps == 4:
        x = np.frombuffer(raw, "<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"unsupported sample format: {fmt}")
    if ch > 1:
        x = x.reshape(n, ch).mean(axis=1, dtype=np.float32)
    return np.ascontiguousarray(x, dtype=np.float32)


class Resampler:
    """Streaming resampler to 16 kHz. Keeps filter state between chunks, so chunk boundaries are inaudible."""

    def __init__(self, in_rate: int, out_rate: int = SR, quality: str = "HQ"):
        self.in_rate, self.out_rate, self.quality = int(in_rate), int(out_rate), quality
        self._stream = None
        if self.in_rate != self.out_rate:
            import soxr                                    # only needed when a device is not 16 kHz
            self._stream = soxr.ResampleStream(self.in_rate, self.out_rate, 1, dtype="float32", quality=quality)

    @property
    def identity(self) -> bool:
        return self._stream is None

    def process(self, x: np.ndarray, last: bool = False) -> np.ndarray:
        x = np.ascontiguousarray(x, dtype=np.float32).reshape(-1)
        if self._stream is None:
            return x
        if not len(x) and not last:
            return np.zeros(0, np.float32)
        return np.ascontiguousarray(self._stream.resample_chunk(x, last=last), dtype=np.float32).reshape(-1)

    def flush(self) -> np.ndarray:
        return self.process(np.zeros(0, np.float32), last=True)


class Converter:
    """One capture stream: raw buffers in its format -> 16 kHz mono float32."""

    def __init__(self, fmt: Format, out_rate: int = SR):
        self.fmt, self.out_rate = fmt, out_rate
        self.resampler = Resampler(fmt.rate, out_rate)

    def process(self, data, frames: int, silent: bool = False) -> np.ndarray:
        if silent or not data:
            mono = np.zeros(int(frames), np.float32)       # AUDCLNT_BUFFERFLAGS_SILENT: the buffer content is undefined
        else:
            mono = to_mono(data, self.fmt, frames)
        return self.resampler.process(mono)

    def flush(self) -> np.ndarray:
        return self.resampler.flush()

    def reset(self):
        self.resampler = Resampler(self.fmt.rate, self.out_rate)
