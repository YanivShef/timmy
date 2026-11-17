# מקליט שיחות בעברית: תמלול חי בשני ערוצים, כרום (הצד השני) + המיקרופון (אתה).
# stdin = פריימים: [1 בייט ערוץ 0=כרום 1=מיק][4 בייט מספר דגימות LE][float32 16kHz מונו]
# פולט JSON lines ל-stdout (partial/final/bullets/done), כותב <base>.txt עם "<שמך>:" / "צד שני:" ו-<base>.mp3 מעורבב
import sys, threading, time, json, wave, subprocess, os, struct, difflib
import numpy as np
SR = 16000
base = sys.argv[1]
# ---- הגדרות: ~/Library/Application Support/Timmy/config.json ----
APP_DIR = os.environ.get("TIMMY_APP_DIR") or os.path.expanduser("~/Library/Application Support/Timmy")
def load_config():
    try:
        with open(os.path.join(APP_DIR, "config.json"), encoding="utf-8") as f: return json.load(f)
    except Exception: return {}
CFG = load_config()
import shutil as _sh
FFMPEG = CFG.get("ffmpeg_path") or _sh.which("ffmpeg") or next((p for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg") if os.path.exists(p)), "ffmpeg")
OWNER = (CFG.get("owner_name") or "אני").strip()
OWNER_DESC = (CFG.get("owner_description") or "").strip()
OWNER_LINE = f"{OWNER} ({OWNER_DESC})" if OWNER_DESC else OWNER
NAMES = {0: "צד שני", 1: OWNER}

# ---- "שפר את טימי": הוראות אישיות של המשתמש לנקודות ולסיכום (config: custom_instructions) ----
CUSTOM = [x.strip() for x in (CFG.get("custom_instructions") or []) if isinstance(x, str) and x.strip()]
def custom_block(kind):
    if not CUSTOM: return ""
    return (f"\n\nהעדפות אישיות של {OWNER} ל{kind} (חובה לכבד; במקרה של סתירה הן גוברות על ההנחיות למעלה):\n"
            + "\n".join("- " + c for c in CUSTOM))
def _claude_path():
    import shutil, glob
    cands = [CFG.get("claude_path") or "", shutil.which("claude") or "",
             os.path.expanduser("~/.claude/local/claude"), os.path.expanduser("~/.local/bin/claude"),
             "/opt/homebrew/bin/claude", "/usr/local/bin/claude"] + sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin/claude")), reverse=True)
    return next((c for c in cands if c and os.path.exists(c)), "")
RESTYLE_PROMPT = """לפניך מסמך סיכום של שיחה (Markdown) ואת התמלול המלא של השיחה.
{owner} ביקש שהסיכומים שלו ייכתבו לפי ההוראות האלה:
{custom}

כתוב מחדש את מסמך הסיכום כך שיעמוד בהוראות. מותר לשנות מבנה, סדר, סעיפים, אורך, ניסוח ועיצוב (טבלאות, הדגשות, אימוג'י וכו') לפי ההוראות, ולהוסיף מידע מהתמלול אם ההוראות מבקשות.
כללים קבועים:
1. השורה הראשונה נשארת כותרת שמתחילה ב-"# ".
2. שורת התאריך, האורך ו"עם" נשארת בדיוק כמו שהיא.
3. השורה שמתחילה ב-"תמלול מלא:" נשארת בסוף בדיוק כמו שהיא.
4. אל תמציא שום עובדה שלא מופיעה בתמלול. בלי מקף ארוך.
החזר רק את המסמך המלא, בלי שום טקסט לפניו או אחריו.

המסמך הנוכחי:
{md}

התמלול:
{transcript}"""
def restyle(md_text, transcript_text):
    """מחיל את ההוראות האישיות על מסמך סיכום. אם משהו נכשל, מחזיר את המקור."""
    claude = _claude_path()
    if not CUSTOM or not claude: return md_text
    try:
        out = subprocess.run([claude, "-p", "--model", "sonnet"], capture_output=True, text=True, timeout=240, cwd="/tmp",
                             input=RESTYLE_PROMPT.format(owner=OWNER, custom="\n".join("- " + c for c in CUSTOM),
                                                         md=md_text, transcript=transcript_text[-60000:])).stdout.strip()
    except Exception:
        return md_text
    if out.startswith("```"): out = out.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    return out + "\n" if out.startswith("# ") and "תמלול מלא:" in out else md_text

# מצב "שפר סיכום קיים": live.py --restyle <קובץ סיכום.md>
if base == "--restyle":
    md_path = sys.argv[2]
    tr_path = md_path[:-3] + " - תמלול.txt"
    md_text = open(md_path, encoding="utf-8").read()
    tr_text = open(tr_path, encoding="utf-8").read() if os.path.exists(tr_path) else ""
    new_text = restyle(md_text, tr_text)
    if new_text != md_text:
        open(md_path, "w", encoding="utf-8").write(new_text)
        print(json.dumps({"type": "restyled", "txt": md_path, "changed": True}, ensure_ascii=False), flush=True)
    else:
        print(json.dumps({"type": "restyled", "txt": md_path, "changed": False}, ensure_ascii=False), flush=True)
    sys.exit(0)
disk_full = [False]
lock = threading.Lock(); chunks = {0: [], 1: []}; eof = [False]; total = {0: 0, 1: 0}; got = {0: False, 1: False}

def reader():
    ws = {}
    for ch in (0, 1):
        w = wave.open(f"{base}.ch{ch}.wav", "wb"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); ws[ch] = w
    f = sys.stdin.buffer
    def readn(n):
        b = b""
        while len(b) < n:
            d = f.read(n - len(b))
            if not d: return None
            b += d
        return b
    while True:
        h = readn(5)
        if h is None: break
        ch, n = h[0], struct.unpack("<I", h[1:])[0]
        d = readn(n * 4)
        if d is None: break
        if ch not in (0, 1): continue
        a = np.frombuffer(d, dtype=np.float32).copy()
        try:
            if not disk_full[0]: ws[ch].writeframes((np.clip(a, -1, 1) * 32767).astype(np.int16).tobytes())
        except OSError:
            # הדיסק התמלא: מפסיקים לכתוב אודיו, ממשיכים לתמלל, ומבקשים מהאפליקציה לעצור
            disk_full[0] = True
            print(json.dumps({"type": "diskfull"}), flush=True)
        with lock: chunks[ch].append(a); total[ch] += len(a); got[ch] = True
    for w in ws.values():
        try: w.close()
        except OSError: pass
    eof[0] = True
threading.Thread(target=reader, daemon=True).start()

def chrome_tabs():
    try:
        return subprocess.run(["osascript", "-e", 'tell application "Google Chrome" to get title of active tab of windows'],
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""
TABS = chrome_tabs()

def emit(o): print(json.dumps(o, ensure_ascii=False), flush=True)
emit({"type": "loading"})
import mlx_whisper
MODEL = "mlx-community/ivrit-ai-whisper-large-v3-turbo-mlx"
import re as _re
# מילות מילוי שתמיד נזרקות כשהן כל השורה ("אין" ו"אוקיי" הן תשובות אמיתיות, לא כאן)
FILLERS = {"אה", "אהה", "אממ", "אמ", "הממ", "ממ", "נו"}
# משפטי "מלאי" ש-Whisper ממציא על מוזיקה/רעש/שקט
STOCK = {"תודה רבה", "בבקשה", "סליחה", "תודה שצפיתם", "כתוביות"}
NEVER = ("היושב ראש", "היושב-ראש", "חברי הכנסת", "תודה שצפיתם", "כתוביות")
BIDI = _re.compile("[‎‏‪-‮⁦-⁩]")
def _norm(w): return _re.sub(r"[^\w]", "", w)
def dedupe(t):
    # לולאות חזרה של Whisper בתוך שורה: מילה/צמד 3+ פעמים ברצף, או ביטוי של 3+ מילים פעמיים -> נשאר פעם אחת (עם הפיסוק של העותק האחרון)
    words = t.split(); changed = False
    for n in list(range(1, 9)) + list(range(1, 9)):
        min_rep = 2 if n >= 3 else 3
        i = 0; out = []
        while i < len(words):
            if i + n * min_rep <= len(words):
                gram = [_norm(w) for w in words[i:i + n]]
                k = 1
                while i + n * (k + 1) <= len(words) and [_norm(w) for w in words[i + n * k:i + n * (k + 1)]] == gram and any(gram):
                    k += 1
                if k >= min_rep:
                    out += words[i + n * (k - 1):i + n * k]; i += n * k; changed = True; continue
            out.append(words[i]); i += 1
        words = out
    return " ".join(words), changed
def clean(t):
    t = BIDI.sub("", t).strip()
    t, changed = dedupe(t)
    core = [w for w in (_norm(x) for x in t.split()) if w]
    if not core or all(w in FILLERS for w in core): return "", changed
    return t, changed
def tr(a, partial=False):
    """מחזיר (טקסט, אמין, פרטים). בתמלול ביניים טקסט לא אמין לא מוצג (במקומו 'מתמלל');
    בשורה סופית פרטים משמשים לסינון הזיות (final_ok)."""
    # ביניים: בלי ניסיונות חוזרים; סופי: עד 0.2 (מעל זה final_ok זורק ממילא, אז ניסיונות נוספים רק מעכבים)
    kw = {"temperature": (0.0,) if partial else (0.0, 0.2)}
    r = mlx_whisper.transcribe(a, path_or_hf_repo=MODEL, language="he", condition_on_previous_text=False, verbose=None, **kw)
    raw = r["segments"]
    segs = [s for s in raw if s.get("avg_logprob", 0) > -1.0 and s.get("compression_ratio", 1) < 2.4]
    text, changed = clean(" ".join(s["text"].strip() for s in segs).strip())
    nwords = len(text.split())
    cr_max = 2.0 if nwords < 20 else 2.35
    info = {"changed": changed,
            "min_lp": min([s.get("avg_logprob", 0) for s in segs], default=0.0),
            "max_temp": max([s.get("temperature", 0) for s in segs], default=0.0)}
    reliable = bool(text) and not changed and len(segs) == len(raw) and info["max_temp"] == 0 and all(
        s.get("compression_ratio", 1) < cr_max and s.get("avg_logprob", 0) > -0.8 for s in segs)
    if reliable and not final_ok(text, info, partial=True): reliable = False
    return text, reliable, info
def final_ok(text, info, partial=False):
    """שורה סופית: זורקים הזיות טיפוסיות של Whisper על רעש/מוזיקה."""
    if not text: return False
    core = " ".join(w for w in (_norm(x) for x in text.split()) if w)
    if any(n in text for n in NEVER): return False
    if not partial and info["max_temp"] >= 0.4: return False
    n = len(core.split())
    if n <= 3 and info["min_lp"] <= -0.7: return False
    if info["changed"] and info["min_lp"] < -0.5: return False
    if core in STOCK and info["min_lp"] < -0.5: return False
    return True
tr(np.zeros(SR, np.float32)); emit({"type": "ready"})

FR = 1600; TH = {0: 0.006, 1: 0.01}
def speech_frames(a):
    n = len(a) // FR
    return np.sqrt((a[:n * FR].reshape(n, FR) ** 2).mean(1)) if n else np.zeros(0)
def ts(s): s = int(s); return f"{s//3600:02d}:{s%3600//60:02d}:{s%60:02d}"

# ---- בולטים חיים: אחרי 15 שניות ואז כל 30, דרך Claude Haiku ----
def find_claude():
    import shutil, glob
    cands = [CFG.get("claude_path") or "", shutil.which("claude") or "",
             os.path.expanduser("~/.claude/local/claude"), os.path.expanduser("~/.local/bin/claude"),
             "/opt/homebrew/bin/claude", "/usr/local/bin/claude"] + sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin/claude")), reverse=True)
    return next((c for c in cands if c and os.path.exists(c)), "")
CLAUDE = find_claude()   # בלי Claude Code: רק תמלול, בלי בולטים וסיכום
entries = []   # (start_sec, speaker, text)
finals = []    # שורות "דובר: טקסט" לפי סדר הזמן, לפרומפטים
bullets = []; blk = threading.Lock(); bullets_state = {"done_upto": 0}
PROMPT = """אתה רושם נקודות עיקריות משיחה בעברית בזמן אמת. התמלול אוטומטי ויכולות להיות בו טעויות, תבין מה התכוונו.
בתמלול "{owner}:" זה {owner_line}, בעל המחשב, ו"צד שני:" זה האדם שמדבר איתו בשיחה. ייחס כל דבר למי שאמר אותו. אם הצד השני מציג את עצמו בשם, השתמש בשם שלו (שים לב: בתמלול שמות לפעמים נדבקים למילה שלפניהם, למשל "זהדני" = "זה דני").

נקודות קיימות:
{bullets}

תמלול השיחה עד עכשיו:
{transcript}

החזר רשימה מעודכנת של עד 10 נקודות עיקריות: רק דברים מהותיים (מידע, רעיונות, החלטות, משימות, מספרים, הסכמות), לא סיכום של כל משפט ולא חילופי נימוסים. קצרות (עד 14 מילים), בעברית, בסדר כרונולוגי. אל תעיר על איכות התמלול. אם עדיין אין שום דבר מהותי, החזר נקודה אחת שמתארת על מה השיחה. שמור על נקודות קיימות שעדיין נכונות, אחד כפילויות והוסף את מה שחדש. בלי מקף ארוך. החזר רק שורות שמתחילות ב-"- " ושום טקסט אחר.{custom}"""

def update_bullets():
    with blk:
        n = len(finals)
        if n == bullets_state["done_upto"]: return
        transcript = "\n".join(finals)[-9000:]
        prompt = PROMPT.format(owner=OWNER, owner_line=OWNER_LINE, custom=custom_block("נקודות"), bullets="\n".join("- " + b for b in bullets) or "(אין עדיין)", transcript=transcript)
        try:
            out = subprocess.run([CLAUDE, "-p", "--model", "haiku"], input=prompt, capture_output=True, text=True,
                                 timeout=90, cwd="/tmp").stdout
        except Exception:
            return
        items = [l.strip()[2:].strip() for l in out.splitlines() if l.strip().startswith("- ")]
        if not items: return
        bullets[:] = items; bullets_state["done_upto"] = n
        with open(base + " נקודות.md", "w") as f:
            f.write("# נקודות עיקריות\n\n" + "\n".join("- " + b for b in items) + "\n")
        emit({"type": "bullets", "items": items})

def bullets_loop():
    time.sleep(15)
    while True:
        update_bullets()
        time.sleep(30)
threading.Thread(target=bullets_loop, daemon=True).start()

txt = open(base + ".txt", "a")

# היסטוריית האודיו של כרום (120 שניות אחרונות), לזיהוי הד לפי צליל
CH0_HIST = {"buf": np.zeros(0, np.float32), "start": 0}
def ch0_append(arrs):
    if not arrs: return
    b = np.concatenate([CH0_HIST["buf"]] + arrs)
    keep = 120 * SR
    if len(b) > keep:
        CH0_HIST["start"] += len(b) - keep; b = b[-keep:]
    CH0_HIST["buf"] = b
def _env(a, hop=320):
    n = len(a) // hop
    return np.sqrt((a[:n * hop].reshape(n, hop) ** 2).mean(1)) if n else np.zeros(0)
def echo_score(mic, mic_start):
    """כמה הקטע מהמיקרופון 'הולך יחד' עם כרום (0..1). הד מהרמקולים = המעטפת של המיק עוקבת אחרי כרום בהשהיה של עד חצי שנייה."""
    L = int(0.5 * SR); hop = 320
    s0 = mic_start - L - CH0_HIST["start"]; e0 = mic_start + len(mic) - CH0_HIST["start"]
    if s0 < 0 or e0 > len(CH0_HIST["buf"]) or len(mic) < hop * 10: return 0.0
    c = CH0_HIST["buf"][s0:e0]
    if np.sqrt((c ** 2).mean()) < TH[0]: return 0.0          # כרום שקט = לא הד
    em, ec = _env(mic), _env(c); Lf = L // hop; M = len(em)
    if len(ec) < M + Lf or em.std() < 1e-6: return 0.0
    best = 0.0
    for lag in range(0, Lf + 1):
        x = ec[Lf - lag: Lf - lag + M]
        if x.std() < 1e-6: continue
        best = max(best, float(np.corrcoef(em, x)[0, 1]))
    return best

def text_overlap(a, b):
    """איזה חלק מהטקסט a מופיע בתוך b (0..1)"""
    if not a or not b: return 0.0
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return sum(m.size for m in sm.get_matching_blocks()) / max(1, len(a))

def is_echo(text, at):
    # בלי אוזניות המיקרופון קולט את הצד השני מהרמקולים: שורה שלך שדומה למה שהצד השני אמר באותו זמן = הד
    for (t0, spk, tx) in entries[-12:]:
        if spk == 0 and abs(t0 - at) < 12 and difflib.SequenceMatcher(None, text, tx).ratio() > 0.55:
            return True
    return False

class Chan:
    def __init__(self, ch): self.ch = ch; self.pending = np.zeros(0, np.float32); self.start = 0; self.last_partial = 0.0; self.speaking = False
    def finalize(self, a, at):
        self.speaking = False
        if self.ch == 1 and echo_score(a, at) > 0.6:
            emit({"type": "partial", "text": "", "speaker": "me"}); return
        t, _, info = tr(a); sec = at / SR
        if t and not final_ok(t, info): t = ""
        if t and len(t.split()) <= 3 and info["min_lp"] < -0.65:   # רק חזרה "חלשה" נחשבת הזיה; "כן" אמיתי פעמיים נשאר
            key = " ".join(_norm(w) for w in t.split())
            if any(spk == self.ch and sec - t0 < 30 and " ".join(_norm(w) for w in tx.split()) == key for (t0, spk, tx) in entries[-6:]):
                t = ""   # אותה שורה קצרה שוב ושוב = הזיה
        if t and not (self.ch == 1 and is_echo(t, sec)):
            entries.append((sec, self.ch, t)); finals.append(f"{NAMES[self.ch]}: {t}")
            txt.write(f"[{ts(sec)}] {NAMES[self.ch]}: {t}\n"); txt.flush()
            emit({"type": "final", "text": t, "t": ts(sec), "speaker": "me" if self.ch == 1 else "other"})
        else:
            emit({"type": "partial", "text": "", "speaker": "me" if self.ch == 1 else "other"})
    def step(self, new, done):
        if new: self.pending = np.concatenate([self.pending] + new)
        rms = speech_frames(self.pending); sp = rms > TH[self.ch]
        if not sp.any():
            if len(self.pending) > SR:
                drop = len(self.pending) - int(0.3 * SR); self.pending = self.pending[drop:]; self.start += drop
            return
        cut = max(0, int(np.argmax(sp)) * FR - int(0.2 * SR))
        if cut:
            self.pending = self.pending[cut:]; self.start += cut; rms = speech_frames(self.pending); sp = rms > TH[self.ch]
        dur = len(self.pending) / SR
        if not self.speaking:
            self.speaking = True
            emit({"type": "partial", "text": "", "thinking": True, "speaker": "me" if self.ch == 1 else "other"})
        tail_silent = len(sp) >= 6 and not sp[-6:].any()
        if done or (dur >= 2 and tail_silent):
            self.finalize(self.pending, self.start); self.start += len(self.pending); self.pending = np.zeros(0, np.float32)
        elif dur >= 14:
            lo, hi = 9 * SR // FR, 14 * SR // FR
            split = (lo + int(np.argmin(rms[lo:hi]))) * FR
            self.finalize(self.pending[:split], self.start); self.start += split; self.pending = self.pending[split:]
        elif time.time() - self.last_partial >= 1.2 and dur >= 0.8:
            if self.ch == 1 and echo_score(self.pending, self.start) > 0.6:
                self.last_partial = time.time(); return
            t, ok, _ = tr(self.pending, partial=True)
            sp = "me" if self.ch == 1 else "other"
            if ok: emit({"type": "partial", "text": t, "speaker": sp})
            else: emit({"type": "partial", "text": "", "thinking": True, "speaker": sp})
            self.last_partial = time.time()

chans = {0: Chan(0), 1: Chan(1)}
while True:
    time.sleep(0.25)
    with lock:
        new = {c: chunks[c][:] for c in (0, 1)}
        for c in (0, 1): chunks[c].clear()
        done = eof[0] and not chunks[0] and not chunks[1]
    ch0_append(new[0])
    for c in (0, 1): chans[c].step(new[c], done)
    if done: break

txt.close()
emit({"type": "saving"})
update_bullets()
# קובץ התמלול הסופי: ממוין לפי זמן
entries.sort(key=lambda e: e[0])
# הד שהתפספס: שורה שלך שרובה מופיעה במה שהצד השני אמר באותו זמן (±15 שנ׳) נמחקת
def _echo_line(e):
    sec, spk, t = e
    if spk != 1: return False
    near = " ".join(x[2] for x in entries if x[1] == 0 and abs(x[0] - sec) < 15)
    return text_overlap(t, near) > 0.6
entries[:] = [e for e in entries if not _echo_line(e)]
def write_transcript(path, other_name):
    with open(path, "w") as f:
        for (sec, spk, t) in entries:
            f.write(f"[{ts(sec)}] {NAMES[1] if spk == 1 else other_name}: {t}\n")
write_transcript(base + ".txt", NAMES[0])
# אודיו: מערבב את שני הערוצים ל-mp3 אחד
ins = [f"{base}.ch{c}.wav" for c in (0, 1) if got[c]]
if ins:
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y"]
    for i in ins: cmd += ["-i", i]
    if len(ins) == 2: cmd += ["-filter_complex", "amix=inputs=2:duration=longest:normalize=0"]
    subprocess.run(cmd + ["-b:a", "64k", base + ".mp3"])
if os.path.exists(base + ".mp3"):
    for c in (0, 1):
        if os.path.exists(f"{base}.ch{c}.wav") and not os.environ.get("LIVE_KEEP_WAV"): os.remove(f"{base}.ch{c}.wav")
if not entries:
    for ext in (".mp3", ".ch0.wav", ".ch1.wav", " נקודות.md"):
        try: os.remove(base + ext)
        except OSError: pass
    emit({"type": "done", "txt": base + ".txt"}); sys.exit(0)

# ---- סיכום + כותרת בסוף, ושינוי שמות הקבצים לפי הכותרת ----
# רשימת האנשים המוכרים (לזיהוי עם מי השיחה): מההגדרות, מקובץ שמות, ומתיקיית פתקי לקוחות (אופציונלי)
CLIENTS_DIR = os.path.expanduser(CFG.get("clients_dir") or "")
MEETINGS_DIR = os.path.expanduser(CFG.get("meetings_dir") or "")
import re
people = [p.strip() for p in CFG.get("people", []) if p and p.strip()]
try:
    if CFG.get("people_file"):
        with open(os.path.expanduser(CFG["people_file"]), encoding="utf-8") as f:
            people += [l.strip() for l in f if l.strip()]
except Exception: pass
notes = []
try:
    for f in (os.listdir(CLIENTS_DIR) if CLIENTS_DIR else []):
        stem = f[:-3] if f.endswith(".md") else f
        if f.endswith(".md") or os.path.isdir(os.path.join(CLIENTS_DIR, f)):
            if not re.match(r"^[^\w\u0590-\u05FF]", stem): notes.append(stem)
except Exception: pass
people = list(dict.fromkeys(people + [n.split(" - ")[0] for n in notes]))

SUMMARY_PROMPT = """לפניך תמלול אוטומטי של שיחה או הרצאה בעברית (ייתכנו טעויות תמלול, תבין מה התכוונו).
"{owner}:" זה {owner_line}, בעל המחשב, "צד שני:" זה מי שמדבר איתו דרך כרום. ייחס כל דבר למי שאמר אותו.

כותרות הטאבים הפעילים בכרום בזמן ההקלטה: {tabs}

רשימת הלקוחות והאנשים המוכרים של {owner}:
{people}

התמלול:
{transcript}

כתוב:
1. עם מי השיחה (הצד השני, לא {owner}): אם זה אחד מהאנשים ברשימה (לפי שם שהוזכר, כותרת הטאב או ההקשר), החזר את השם בדיוק כמו שהוא כתוב ברשימה (שים לב: בתמלול שמות לפעמים נדבקים למילה שלפניהם, למשל "זהדני" = "זה דני", ושם פרטי לבד מספיק אם הוא מתאים לאדם אחד ברשימה וההקשר מתאים). אחרת, אם מוזכר שם של האדם המדבר, החזר אותו. אם אי אפשר לדעת, החזר "לא ידוע". אל תנחש.
2. כותרת קצרה (3 עד 7 מילים) שמתארת על מה השיחה ומתאימה כשם קובץ, בלי שם האדם.
3. סיכום של 3 עד 6 משפטים: על מה דובר, מה היה חשוב, מה סוכם.
4. משימות או צעדים להמשך, רק אם הוזכרו, וכל משימה עם מי אחראי עליה ({owner} או הצד השני בשמו). אם לא הוזכרו, השאר ריק.
בלי מקף ארוך. החזר בדיוק בפורמט הזה ושום דבר אחר:
עם: ...
כותרת: ...
סיכום:
...
משימות:
- ..."""
title, summary, tasks, person = "", "", [], ""
try:
    out = subprocess.run([CLAUDE, "-p", "--model", "sonnet"], input=SUMMARY_PROMPT.format(owner=OWNER, owner_line=OWNER_LINE, transcript="\n".join(f"{NAMES[sp]}: {t}" for _, sp, t in entries), tabs=TABS or "(אין)", people="\n".join(people) or "(אין)"),
                         capture_output=True, text=True, timeout=180, cwd="/tmp").stdout
    sec = None; body = []
    for l in out.splitlines():
        st = l.strip()
        if st.startswith("עם:"): person = st.split(":", 1)[1].strip().strip('"*'); continue
        if st.startswith("כותרת:"): title = st.split(":", 1)[1].strip().strip('"*'); continue
        if st.startswith("סיכום:"): sec = "s"; rest = st.split(":", 1)[1].strip(); body += [rest] if rest else []; continue
        if st.startswith("משימות:"): sec = "t"; continue
        if sec == "s" and st: body.append(st)
        if sec == "t" and st.startswith("- ") and st[2:].strip() not in ("", "אין", "..."): tasks.append(st[2:].strip())
    summary = "\n\n".join(body)
except Exception:
    pass

if person in ("", "לא ידוע", "..."): person = ""
# קישור לפתק הלקוח (Obsidian), אם יש תיקיית פתקים
link = ""
if person:
    match = next((n for n in notes if n == person), None) or next((n for n in notes if n.startswith(person) or person.startswith(n.split(" - ")[0])), None)
    link = f"[[{match or person}]]" if CLIENTS_DIR else person
safe = re.sub(r'[\\/:*?"<>|\n]', "", title).strip()[:60] or "שיחה בכרום"
d = os.path.dirname(base); stamp = os.path.basename(base).replace(" כרום", "")
clean_person = re.sub(r'[\\/:*?"<>|]', "", person).strip()
new = os.path.join(d, f"{stamp[:16]} {clean_person + ' - ' if clean_person else ''}{safe}")
def mv(a, b):
    if os.path.exists(a): os.rename(a, b)
# בתמלול הסופי: "צד שני" מוחלף בשם הפרטי של האדם, אם זוהה
write_transcript(base + ".txt", person.split()[0] if person else NAMES[0])
mv(base + ".txt", new + " - תמלול.txt")
mv(base + ".mp3", new + ".mp3")
if os.path.exists(base + " נקודות.md"): os.remove(base + " נקודות.md")
L = int(max(total.values()) / SR)
md = [f"# {title or safe}", "", f"**תאריך:** {stamp[:10]} {stamp[11:16].replace('-', ':')} · **אורך:** {L//60}:{L%60:02d} דק׳" + (f" · **עם:** {link}" if link else ""), ""]
if summary: md += ["## סיכום", "", summary, ""]
if bullets: md += ["## נקודות עיקריות", ""] + ["- " + b for b in bullets] + [""]
if tasks: md += ["## משימות", ""] + ["- [ ] " + t for t in tasks] + [""]
md += [f"תמלול מלא: {os.path.basename(new)} - תמלול.txt", ""]
md_text = "\n".join(md)
if CUSTOM:   # "שפר את טימי": הסיכום נכתב מחדש לפי ההוראות האישיות
    emit({"type": "saving"})
    md_text = restyle(md_text, "\n".join(f"{NAMES[sp]}: {t}" for _, sp, t in entries))
with open(new + ".md", "w") as f: f.write(md_text)
# שיחה עם אדם מזוהה: עותק לתיקיית הפגישות (אם הוגדרה)
if person and MEETINGS_DIR and os.path.isdir(MEETINGS_DIR) and not os.environ.get("LIVE_NO_VAULT"):
    with open(os.path.join(MEETINGS_DIR, os.path.basename(new) + ".md"), "w") as f: f.write(md_text)
emit({"type": "done", "txt": new + ".md", "title": title})
