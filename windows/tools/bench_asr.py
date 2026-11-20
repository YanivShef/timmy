"""CPU / GPU speed of the ivrit.ai CT2 model on this machine (CI: workflow_dispatch with asr_bench=true).

python tools/bench_asr.py [--model ivrit-ai/whisper-large-v3-turbo-ct2] [--device cpu] [--threads N] [--out file.json]
Prints and writes one JSON object: load time, and seconds per transcribe() call for 2 s / 6 s / 14 s / 28 s inputs.
Whisper always encodes a 30 s window, so the cost per call is almost flat: this is what sizes live segmentation.
"""
import argparse
import json
import os
import platform
import time
import wave
from pathlib import Path

import numpy as np

FIX = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "he_short.wav"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="ivrit-ai/whisper-large-v3-turbo-ct2")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--compute", default="int8")
    ap.add_argument("--threads", type=int, default=0)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    from faster_whisper import WhisperModel

    with wave.open(str(FIX)) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
    threads = a.threads or os.cpu_count() or 4
    t = time.time()
    m = WhisperModel(a.model, device=a.device, compute_type=a.compute, cpu_threads=threads)
    res = {"model": a.model, "device": a.device, "compute": a.compute, "threads": threads,
           "cpu": platform.processor(), "cpus": os.cpu_count(), "load_s": round(time.time() - t, 2), "runs": []}

    def run(audio, label):
        t0 = time.time()
        segs, _ = m.transcribe(audio, language="he", beam_size=1, condition_on_previous_text=False, temperature=0.0)
        text = " ".join(s.text.strip() for s in segs)
        res["runs"].append({"label": label, "audio_s": round(len(audio) / 16000, 1), "took_s": round(time.time() - t0, 2),
                            "text": text[:160]})

    run(x, "warmup")
    long = np.concatenate([x] * 6)
    for secs in (2, 6, 14, 28):
        run(long[:16000 * secs], f"{secs}s")
    s = json.dumps(res, ensure_ascii=False, indent=1)
    print(s)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(s, encoding="utf-8")


if __name__ == "__main__":
    main()
