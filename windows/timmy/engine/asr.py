"""Transcription backend for the worker: faster-whisper (CTranslate2) with the ivrit.ai model (DESIGN.md section 6).

* Model: config `asr_model` (default ivrit-ai/whisper-large-v3-turbo-ct2, the CT2 conversion of the model the Mac
  uses through MLX). Looked up in order: config `asr_model_path`, %LOCALAPPDATA%\\Timmy\\models\\<repo>, the Hugging
  Face cache, and only then downloaded (resumable) into the models folder. TIMMY_ASR_MODEL overrides the repo or
  path (CI uses Systran/faster-whisper-tiny for plumbing tests).
* Device: config `asr_device` auto | cpu | cuda. auto = CUDA only when ctranslate2 sees a GPU, the cuBLAS/cuDNN DLLs
  load and a 1 s test transcription works; otherwise CPU, int8, one thread per physical core (max 8), and the
  worker lowers its priority to BELOW_NORMAL.
* Profile (6.3): "fast" = the Mac's live behaviour (text partials every 1.2 s); "cpu" = no text partials, finished
  utterances are queued and transcribed together (up to 28 s per call) and split back into lines by the segment
  timestamps. Picked from the warm-up time (< 1 s = fast) unless config `asr_profile` says fast / cpu.
* segments_info() is mac/live.py tr() after the model call (same thresholds); the text filters themselves come
  from the synced timmy.engine._mac.live_shared.
"""
import os
import sys
import time
import zlib

import numpy as np

from timmy import paths
from timmy.engine._mac import live_shared as L

SR = 16000
DEFAULT_MODEL = "ivrit-ai/whisper-large-v3-turbo-ct2"
TINY_MODEL = "Systran/faster-whisper-tiny"
MERGE_MAX_S = 28.0        # one Whisper window is 30 s; keep a margin
MERGE_GAP_S = 0.6         # silence put between merged utterances so Whisper starts a new segment there
FAST_WARMUP_S = 1.0


# ---------------------------------------------------------------- mac/live.py tr(), after the model call

def segments_info(raw, partial=False):
    """(text, reliable, info) from Whisper segments, exactly the Mac tr() post-processing:
    keep segments with avg_logprob > -1.0 and compression_ratio < 2.4, clean() them, and call the result reliable
    only if nothing was dropped or deduped, no temperature fallback, all segments well inside the thresholds and it
    passes final_ok(partial=True). info feeds final_ok() for final lines."""
    raw = list(raw)
    segs = [s for s in raw if s.get("avg_logprob", 0) > -1.0 and s.get("compression_ratio", 1) < 2.4]
    text, changed = L.clean(" ".join(s["text"].strip() for s in segs).strip())
    nwords = len(text.split())
    cr_max = 2.0 if nwords < 20 else 2.35
    info = {"changed": changed,
            "min_lp": min([s.get("avg_logprob", 0) for s in segs], default=0.0),
            "max_temp": max([s.get("temperature", 0) or 0 for s in segs], default=0.0)}
    reliable = bool(text) and not changed and len(segs) == len(raw) and info["max_temp"] == 0 and all(
        s.get("compression_ratio", 1) < cr_max and s.get("avg_logprob", 0) > -0.8 for s in segs)
    if reliable and not L.final_ok(text, info, partial=True):
        reliable = False
    return text, reliable, info


# ---------------------------------------------------------------- merge / split (cpu profile)

def merge(pieces, gap_s=MERGE_GAP_S):
    """pieces = [float32 arrays] -> (one array with `gap_s` of silence between them, [(start_s, end_s)] per piece)."""
    gap = np.zeros(int(gap_s * SR), np.float32)
    parts, spans, pos = [], [], 0
    for i, a in enumerate(pieces):
        if i:
            parts.append(gap)
            pos += len(gap)
        parts.append(np.asarray(a, np.float32))
        spans.append((pos / SR, (pos + len(a)) / SR))
        pos += len(a)
    return (np.concatenate(parts) if parts else np.zeros(0, np.float32)), spans


def split(segs, spans):
    """Give every segment to the piece its midpoint falls in (or the nearest piece). -> [[seg, ...] per piece]."""
    out = [[] for _ in spans]
    if not spans:
        return out
    for s in segs:
        mid = (float(s.get("start", 0)) + float(s.get("end", 0))) / 2
        best, dist = 0, None
        for i, (a, b) in enumerate(spans):
            d = 0.0 if a <= mid <= b else min(abs(mid - a), abs(mid - b))
            if dist is None or d < dist:
                best, dist = i, d
        out[best].append(s)
    if len(spans) > 1:
        # Whisper reports compression_ratio per 30 s window, so in a merged call every piece would get the ratio of
        # all of them together: two people saying the same sentence (or one repeated "כן, בסדר") would push it over
        # the Mac's 2.4 limit and drop every line of the window. Each piece gets the ratio of its own text instead
        # (the same formula faster-whisper uses). avg_logprob and temperature stay per window.
        for i, grp in enumerate(out):
            if grp:
                cr = compression_ratio(" ".join(s["text"].strip() for s in grp))
                out[i] = [dict(s, compression_ratio=cr) for s in grp]
    return out


def compression_ratio(text: str) -> float:
    b = (text or "").encode("utf-8")
    return len(b) / len(zlib.compress(b)) if b else 0.0


def merge_budget(lengths, max_s=MERGE_MAX_S, gap_s=MERGE_GAP_S) -> int:
    """How many of the queued pieces (lengths in samples, oldest first) fit into one call. Always at least 1."""
    total, n = 0.0, 0
    for i, ln in enumerate(lengths):
        add = ln / SR + (gap_s if i else 0.0)
        if n and total + add > max_s:
            break
        total += add
        n += 1
    return max(1, n)


# ---------------------------------------------------------------- model files

MODEL_FILES_REQUIRED = ("model.bin", "config.json")


def model_ready(path) -> bool:
    return bool(path) and os.path.isdir(path) and all(
        os.path.isfile(os.path.join(path, f)) and os.path.getsize(os.path.join(path, f)) > 0 for f in MODEL_FILES_REQUIRED)


def models_dir():
    return paths.app_dir() / "models"


def local_dir_for(repo: str):
    return models_dir() / repo.replace("/", "__")


def model_repo(cfg: dict) -> str:
    return (os.environ.get("TIMMY_ASR_MODEL") or str(cfg.get("asr_model") or "") or DEFAULT_MODEL).strip()


def resolve_model(cfg: dict, download: bool = True) -> str:
    """Local folder with the CT2 model. Raises FileNotFoundError when it is missing and download=False."""
    p = str(cfg.get("asr_model_path") or "").strip()
    if p and model_ready(p):
        return p
    repo = model_repo(cfg)
    if model_ready(repo):                 # TIMMY_ASR_MODEL / asr_model may also be a folder
        return repo
    d = local_dir_for(repo)
    if model_ready(str(d)):
        return str(d)
    try:
        from huggingface_hub import snapshot_download
        c = snapshot_download(repo, local_files_only=True)
        if model_ready(c):
            return c
    except Exception:
        pass
    if not download:
        raise FileNotFoundError(f"model {repo} is not downloaded")
    from huggingface_hub import snapshot_download
    d.mkdir(parents=True, exist_ok=True)
    out = snapshot_download(repo, local_dir=str(d))   # resumes partial files, checks every file against the hub
    if not model_ready(out):
        raise FileNotFoundError(f"model {repo} download incomplete")
    return out


# ---------------------------------------------------------------- device

def cuda_dlls_ok() -> bool:
    """On Windows ctranslate2 aborts the whole process (no Python exception) when cuBLAS / cuDNN are missing."""
    if sys.platform != "win32":
        return True
    import ctypes
    for dll in ("cublas64_12.dll", "cudnn_ops64_9.dll"):
        try:
            ctypes.WinDLL(dll)
        except OSError:
            return False
    return True


def cuda_available() -> bool:
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0 and cuda_dlls_ok()
    except Exception:
        return False


def physical_threads(cap: int = 8) -> int:
    try:
        import psutil
        n = psutil.cpu_count(logical=False)
    except Exception:
        n = None
    if not n:
        n = max(1, (os.cpu_count() or 4) // 2)
    return max(1, min(cap, int(n)))


def pick_profile(cfg: dict, device: str, warmup_s: float) -> str:
    want = str(os.environ.get("TIMMY_ASR_PROFILE") or cfg.get("asr_profile") or "auto").strip().lower()
    if want in ("fast", "cpu"):
        return want
    return "fast" if device == "cuda" or warmup_s < FAST_WARMUP_S else "cpu"


# ---------------------------------------------------------------- backends

def _seg_dict(s):
    return {"text": s.text, "start": float(s.start), "end": float(s.end), "avg_logprob": float(s.avg_logprob),
            "compression_ratio": float(s.compression_ratio), "no_speech_prob": float(s.no_speech_prob),
            "temperature": float(getattr(s, "temperature", 0.0) or 0.0)}


class WhisperASR:
    name = "faster-whisper"

    def __init__(self, model_path: str, device: str = "auto", compute_type: str = "auto", threads: int | None = None):
        from faster_whisper import WhisperModel
        want = (device or "auto").lower()
        if want == "auto":
            want = "cuda" if cuda_available() else "cpu"
        self.threads = threads or physical_threads()
        self.model = None
        if want == "cuda":
            try:
                ct = "float16" if compute_type in ("", "auto", None) else compute_type
                self.model = WhisperModel(model_path, device="cuda", compute_type=ct)
                self._run(np.zeros(SR, np.float32), (0.0,), True)       # cuDNN problems show up only at first use
                self.device, self.compute_type = "cuda", ct
            except Exception:
                self.model = None
        if self.model is None:
            ct = "int8" if compute_type in ("", "auto", None, "float16") else compute_type
            self.model = WhisperModel(model_path, device="cpu", compute_type=ct, cpu_threads=self.threads)
            self.device, self.compute_type = "cpu", ct

    def _run(self, a, temperature, without_timestamps):
        segs, _info = self.model.transcribe(np.asarray(a, np.float32), language="he", beam_size=1, temperature=list(temperature),
                                            condition_on_previous_text=False, vad_filter=False,
                                            without_timestamps=without_timestamps)
        return [_seg_dict(s) for s in segs]

    def segments(self, a, partial=False, timestamps=False):
        # mac tr(): partials one pass (0.0), finals at most one retry (0.0, 0.2): final_ok drops >= 0.4 anyway
        return self._run(a, (0.0,) if partial else (0.0, 0.2), not timestamps)


DEFAULT_FAKE_TEXT = "שלום, אני רוצה לקבוע פגישה ביום שלישי|בסדר גמור, אני אשלח לך את ההצעה עד מחר"


class FakeASR:
    """Test backend (TIMMY_ASR_BACKEND=fake): one segment per stretch of sound. Texts from TIMMY_FAKE_ASR_TEXT,
    "|"-separated and used in turn (default: two different Hebrew sentences, so two speakers never look like an
    echo of each other); optional TIMMY_FAKE_ASR_DELAY seconds per call to imitate a slow CPU. Never used by the app."""
    name = "fake"
    device = "cpu"
    compute_type = "none"
    threads = 1

    def __init__(self, text=None, delay=None):
        raw = text if text is not None else os.environ.get("TIMMY_FAKE_ASR_TEXT") or DEFAULT_FAKE_TEXT
        self.texts = [t.strip() for t in raw.split("|") if t.strip()] or [DEFAULT_FAKE_TEXT]
        self.n = 0
        self.delay = float(delay if delay is not None else os.environ.get("TIMMY_FAKE_ASR_DELAY", "0") or 0)
        self.calls = []

    def segments(self, a, partial=False, timestamps=False):
        a = np.asarray(a, np.float32)
        self.calls.append((len(a), partial, timestamps))
        if self.delay:
            time.sleep(self.delay)
        hop = 1600
        n = len(a) // hop
        if not n:
            return []
        loud = np.sqrt((a[:n * hop].reshape(n, hop) ** 2).mean(1)) > 0.005
        out, i = [], 0
        while i < n:
            if not loud[i]:
                i += 1
                continue
            j = i
            while j < n and (loud[j] or (j + 3 < n and loud[j + 1:j + 4].any())):
                j += 1
            out.append({"text": " " + self.texts[self.n % len(self.texts)], "start": i * hop / SR, "end": j * hop / SR, "avg_logprob": -0.2,
                        "compression_ratio": 1.2, "no_speech_prob": 0.0, "temperature": 0.0})
            if not partial:            # a live partial previews the text the final line will get
                self.n += 1
            i = j
        if not timestamps and len(out) > 1:
            out = [dict(out[0], end=out[-1]["end"], text=" ".join(s["text"].strip() for s in out))]
        return out


def make_asr(cfg: dict):
    if (os.environ.get("TIMMY_ASR_BACKEND") or "").lower() == "fake":
        return FakeASR()
    return WhisperASR(resolve_model(cfg), str(cfg.get("asr_device") or "auto"), str(cfg.get("asr_compute_type") or "auto"),
                      int(cfg.get("asr_threads") or 0) or None)

