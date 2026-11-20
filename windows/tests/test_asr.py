"""engine.asr: Mac-identical post-processing, merge / split for the cpu profile, model lookup, device and profile.

The parity test runs the real `tr()` from mac/live.py (extracted with ast, mlx_whisper replaced by a stub that
returns the same segments) next to asr.segments_info() on a table of segment lists.
"""
import ast
import os
import time
import wave

import numpy as np
import pytest

from tests.conftest import FIXTURES, ROOT
from timmy.engine import asr as A
from timmy.engine._mac import live_shared as L

SR = 16000


def seg(text, lp=-0.2, cr=1.3, temp=0.0, start=0.0, end=1.0):
    return {"text": text, "avg_logprob": lp, "compression_ratio": cr, "temperature": temp, "start": start, "end": end}


def mac_tr():
    """mac/live.py tr() bound to a stub mlx_whisper, so it post-processes segments we give it."""
    src = (ROOT / "mac" / "live.py").read_text(encoding="utf-8")
    node = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "tr")
    code = "\n".join(src.splitlines()[node.lineno - 1:node.end_lineno])
    box = {}

    class Stub:
        @staticmethod
        def transcribe(a, **kw):
            box["kw"] = kw
            return {"segments": box["segs"]}

    ns = {"mlx_whisper": Stub, "MODEL": "stub", "clean": L.clean, "final_ok": L.final_ok}
    exec(code, ns)

    def run(segs, partial=False):
        box["segs"] = segs
        return ns["tr"](np.zeros(10, np.float32), partial=partial)
    return run, box


CASES = [
    [seg(" שלום, מה שלומך?")],
    [seg(" כן כן כן כן")],                                             # dedupe -> changed
    [seg(" אה")],                                                       # filler only
    [seg(" תודה רבה", lp=-0.6)],                                        # stock phrase, weak
    [seg(" חברי הכנסת")],                                               # NEVER
    [seg(" משפט ראשון"), seg(" שני", lp=-1.2)],                         # low logprob segment dropped
    [seg(" טקסט", cr=2.5)],                                             # compression ratio dropped
    [seg(" טקסט קצר", lp=-0.75)],                                       # short + weak
    [seg(" טקסט רגיל לגמרי", temp=0.2)],                                # temperature fallback
    [seg(" טקסט רגיל לגמרי", temp=0.4)],
    [seg(" " + "מילה " * 25, cr=2.2)],                                  # long: cr_max 2.35
    [seg(" שלום", lp=-0.85)],                                           # kept, but not reliable
    [seg("\u200f טקסט עם סימן כיוון \u200e")],                              # bidi marks removed
    [],
]


@pytest.mark.parametrize("i", range(len(CASES)))
@pytest.mark.parametrize("partial", [False, True])
def test_segments_info_is_mac_tr(i, partial):
    run, box = mac_tr()
    assert A.segments_info(CASES[i], partial=partial) == run(CASES[i], partial=partial)


def test_mac_temperatures_match_the_backend():
    """mac tr(): partials (0.0,), finals (0.0, 0.2) and language he; WhisperASR.segments() uses the same."""
    run, box = mac_tr()
    run([], partial=True)
    assert box["kw"]["temperature"] == (0.0,) and box["kw"]["language"] == "he"
    run([], partial=False)
    assert box["kw"]["temperature"] == (0.0, 0.2)
    assert box["kw"]["condition_on_previous_text"] is False


# ---------------------------------------------------------------- merge / split

def test_merge_and_split_round_trip():
    a, b, c = np.ones(SR * 2, np.float32), np.ones(SR, np.float32), np.ones(SR * 3, np.float32)
    audio, spans = A.merge([a, b, c])
    gap = A.MERGE_GAP_S
    assert len(audio) == len(a) + len(b) + len(c) + 2 * int(gap * SR)
    assert spans[0] == (0.0, 2.0)
    assert spans[1][0] == pytest.approx(2.0 + gap) and spans[2][1] == pytest.approx(6.0 + 2 * gap)
    segs = [seg(" אחת", start=0.1, end=1.9), seg(" שתיים", start=2.7, end=3.5), seg(" שלוש", start=4.3, end=5.0),
            seg(" ארבע", start=5.2, end=7.1)]
    g = A.split(segs, spans)
    assert [[s["text"] for s in x] for x in g] == [[" אחת"], [" שתיים"], [" שלוש", " ארבע"]]


def test_split_gives_a_segment_in_a_gap_to_the_nearest_piece():
    spans = [(0.0, 2.0), (2.6, 4.0)]
    g = A.split([seg(" א", start=1.9, end=2.2), seg(" ב", start=2.4, end=2.5)], spans)
    assert [len(x) for x in g] == [1, 1]


def test_split_uses_each_pieces_own_compression_ratio():
    """Two identical sentences in one window push Whisper's window-level ratio over 2.4; per piece it is normal."""
    text = " שלום, אני רוצה לקבוע פגישה ביום שלישי בשעה 10 בבוקר כדי לדבר על הצעת המחיר לאתר."
    spans = [(0.0, 6.0), (6.6, 12.6)]
    segs = [seg(text, cr=2.51, start=0.5, end=5.5), seg(text, cr=2.51, start=7.0, end=12.0)]
    groups = A.split(segs, spans)
    for g in groups:
        assert g[0]["compression_ratio"] == pytest.approx(A.compression_ratio(text.strip()))
        assert g[0]["compression_ratio"] < 2.0
        assert A.segments_info(g)[0] == text.strip()
    assert segs[0]["compression_ratio"] == 2.51          # the input is not modified
    # a single piece keeps the window value (the Mac path)
    assert A.split(segs[:1], spans[:1])[0][0]["compression_ratio"] == 2.51


def test_merge_budget():
    s = SR
    assert A.merge_budget([5 * s, 5 * s, 5 * s]) == 3
    assert A.merge_budget([14 * s, 14 * s]) == 1              # 14 + 0.6 + 14 > 28
    assert A.merge_budget([13 * s, 14 * s]) == 2              # 27.6
    assert A.merge_budget([40 * s]) == 1                      # never 0
    assert A.merge_budget([10 * s] * 5) == 2


# ---------------------------------------------------------------- profile, device, model files

def test_pick_profile(monkeypatch):
    monkeypatch.delenv("TIMMY_ASR_PROFILE", raising=False)
    assert A.pick_profile({}, "cpu", 3.2) == "cpu"
    assert A.pick_profile({}, "cpu", 0.4) == "fast"
    assert A.pick_profile({}, "cuda", 3.0) == "fast"
    assert A.pick_profile({"asr_profile": "cpu"}, "cuda", 0.1) == "cpu"
    assert A.pick_profile({"asr_profile": "fast"}, "cpu", 9.0) == "fast"
    monkeypatch.setenv("TIMMY_ASR_PROFILE", "cpu")
    assert A.pick_profile({"asr_profile": "fast"}, "cpu", 0.1) == "cpu"


def test_physical_threads():
    n = A.physical_threads()
    assert 1 <= n <= 8
    assert A.physical_threads(cap=1) == 1


def test_model_lookup_order(app_dir, tmp_path, monkeypatch):
    monkeypatch.delenv("TIMMY_ASR_MODEL", raising=False)
    own = tmp_path / "my-model"
    own.mkdir()
    (own / "model.bin").write_bytes(b"x")
    (own / "config.json").write_text("{}")
    assert A.resolve_model({"asr_model_path": str(own)}) == str(own)
    # the models folder of the app
    d = A.local_dir_for("someone/some-model-ct2")
    assert d == app_dir / "models" / "someone__some-model-ct2"
    d.mkdir(parents=True)
    (d / "model.bin").write_bytes(b"x")
    (d / "config.json").write_text("{}")
    assert A.resolve_model({"asr_model": "someone/some-model-ct2"}, download=False) == str(d)
    # env override (CI: the tiny model), also as a folder
    monkeypatch.setenv("TIMMY_ASR_MODEL", str(own))
    assert A.resolve_model({"asr_model": "someone/some-model-ct2"}, download=False) == str(own)


def test_missing_model_without_download(app_dir, monkeypatch):
    monkeypatch.delenv("TIMMY_ASR_MODEL", raising=False)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    with pytest.raises(FileNotFoundError):
        A.resolve_model({"asr_model": "nobody/not-a-real-model-ct2"}, download=False)


def test_default_model_is_ivrit_turbo_ct2(monkeypatch):
    monkeypatch.delenv("TIMMY_ASR_MODEL", raising=False)
    assert A.model_repo({}) == "ivrit-ai/whisper-large-v3-turbo-ct2"
    from timmy import config
    assert config.DEFAULTS["asr_model"] == A.DEFAULT_MODEL


def test_fake_backend_finds_speech():
    f = A.FakeASR(text="שלום")
    a = np.concatenate([np.zeros(SR), 0.1 * np.ones(SR), np.zeros(SR), 0.1 * np.ones(SR // 2)]).astype(np.float32)
    segs = f.segments(a, timestamps=True)
    assert [(round(s["start"], 1), round(s["end"], 1)) for s in segs] == [(1.0, 2.0), (3.0, 3.5)]
    assert len(f.segments(a)) == 1
    assert f.segments(np.zeros(SR, np.float32)) == []


# ---------------------------------------------------------------- real faster-whisper

def fixture(name="he_short.wav"):
    with wave.open(str(FIXTURES / name)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768


def _have(mod):
    try:
        __import__(mod)
        return True
    except ImportError:
        return False


def _cached(repo):
    try:
        from huggingface_hub import snapshot_download
        return A.model_ready(snapshot_download(repo, local_files_only=True))
    except Exception:
        return False


@pytest.mark.asr
@pytest.mark.skipif(not _have("faster_whisper"), reason="faster-whisper not installed (extra: asr)")
def test_tiny_model_runs_on_cpu(app_dir, monkeypatch):
    """Plumbing with the 75 MB tiny model (downloaded on first use): load, int8 on CPU, segments with timestamps."""
    if not _cached(A.TINY_MODEL) and os.environ.get("TIMMY_TEST_OFFLINE"):
        pytest.skip("tiny model not cached and offline")
    m = A.WhisperASR(A.resolve_model({"asr_model": A.TINY_MODEL}), device="cpu", threads=2)
    assert (m.device, m.compute_type) == ("cpu", "int8")
    t0 = time.perf_counter()
    segs = m.segments(fixture(), timestamps=True)
    assert time.perf_counter() - t0 < 60
    assert segs, "the tiny model returned nothing for 6 s of speech"
    for s in segs:
        assert set(s) >= {"text", "start", "end", "avg_logprob", "compression_ratio", "temperature"}
        assert 0 <= s["start"] <= s["end"] <= 7.0
    assert m.segments(np.zeros(SR, np.float32), partial=True) is not None


@pytest.mark.asr
@pytest.mark.slow
@pytest.mark.skipif(not _have("faster_whisper"), reason="faster-whisper not installed (extra: asr)")
def test_real_model_transcribes_the_fixture(app_dir):
    """The ivrit.ai turbo CT2 model on the fixture (CI job asr-real; locally when the model is in the HF cache)."""
    if not (_cached(A.DEFAULT_MODEL) or os.environ.get("TIMMY_TEST_REAL_MODEL")):
        pytest.skip("real model not available (set TIMMY_TEST_REAL_MODEL=1 to download 1.6 GB)")
    m = A.WhisperASR(A.resolve_model({}), device="cpu")
    text, reliable, info = A.segments_info(m.segments(fixture()))
    for word in ("שלום", "פגישה", "שלישי", "בבוקר", "הצעת המחיר"):
        assert word in text, text
    assert L.final_ok(text, info)
    # the cpu profile path: two utterances merged into one call and split back by the timestamps
    audio, spans = A.merge([fixture(), fixture("he_reply.wav")])
    groups = A.split(m.segments(audio, timestamps=True), spans)
    first, second = (A.segments_info(g)[0] for g in groups)
    assert "פגישה" in first and "הצעת" in first
    assert "בסדר גמור" in second and "מחר" in second
