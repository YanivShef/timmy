#!/bin/bash
# מתקין את טימי, מקליט ומתמלל שיחות בעברית, על מק (Apple Silicon, macOS 15 ומעלה).
# שימוש:
#   ./install.sh --name "השם שלך" [--about "תיאור קצר של העסק"] [--people "דנה כהן,אבי לוי"]
#                [--output-dir "~/Documents/טימי - הקלטות"] [--venv /path/to/existing/venv]
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$HOME/Library/Application Support/Timmy"
APP="$HOME/Applications/טימי.app"
MODEL="mlx-community/ivrit-ai-whisper-large-v3-turbo-mlx"

NAME=""; ABOUT=""; PEOPLE=""; OUTDIR="$HOME/Documents/טימי - הקלטות"; VENV=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) NAME="$2"; shift 2;;
    --about) ABOUT="$2"; shift 2;;
    --people) PEOPLE="$2"; shift 2;;
    --output-dir) OUTDIR="$2"; shift 2;;
    --venv) VENV="$2"; shift 2;;
    *) echo "פרמטר לא מוכר: $1"; exit 1;;
  esac
done

step() { echo ""; echo "==> $1"; }
fail() { echo ""; echo "❌ $1"; exit 1; }

step "בודק את המחשב"
[[ "$(uname -m)" == "arm64" ]] || fail "צריך מק עם שבב Apple (M1 ומעלה). מקים עם אינטל לא נתמכים."
MAJOR=$(sw_vers -productVersion | cut -d. -f1)
[[ "$MAJOR" -ge 15 ]] || fail "צריך macOS 15 (Sequoia) ומעלה. כרגע: $(sw_vers -productVersion)"
FREE_GB=$(df -g "$HOME" | awk 'NR==2{print $4}')
[[ -n "$VENV" || "$FREE_GB" -ge 4 ]] || fail "צריך לפחות 4GB פנויים בדיסק (יש $FREE_GB GB)."
echo "✓ מק $(sw_vers -productVersion), שבב Apple, $FREE_GB GB פנויים"

step "בודק כלי פיתוח של אפל (בשביל לבנות את האפליקציה)"
if ! xcode-select -p >/dev/null 2>&1 || ! command -v swiftc >/dev/null; then
  xcode-select --install || true
  fail "נפתח חלון התקנה של Command Line Tools. אשר אותו, חכה שיסתיים, ואז הרץ שוב את ההתקנה."
fi
echo "✓ swiftc קיים"

step "בודק Homebrew, ffmpeg ו-Python"
BREW=""
for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [[ -x "$b" ]] && BREW="$b" && break; done
if [[ -z "$BREW" ]]; then
  fail "צריך Homebrew. התקנה: /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\" ואז להריץ שוב."
fi
BREW_PREFIX="$("$BREW" --prefix)"
[[ -x "$BREW_PREFIX/bin/ffmpeg" ]] || "$BREW" install ffmpeg
FFMPEG="$BREW_PREFIX/bin/ffmpeg"
echo "✓ ffmpeg: $FFMPEG"

mkdir -p "$APP_DIR"
if [[ -n "$VENV" ]]; then
  [[ -x "$VENV/bin/python" ]] || fail "לא נמצא python ב-$VENV"
  rm -rf "$APP_DIR/venv"; ln -s "$VENV" "$APP_DIR/venv"
  echo "✓ משתמש בסביבת פייתון קיימת: $VENV"
else
  PY=""
  for v in 3.12 3.11; do [[ -x "$BREW_PREFIX/bin/python$v" ]] && PY="$BREW_PREFIX/bin/python$v" && break; done
  if [[ -z "$PY" ]]; then "$BREW" install python@3.12; PY="$BREW_PREFIX/bin/python3.12"; fi
  if [[ ! -x "$APP_DIR/venv/bin/python" ]]; then
    "$PY" -m venv "$APP_DIR/venv"
  fi
  step "מתקין את מנוע התמלול (mlx-whisper)"
  "$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
  "$APP_DIR/venv/bin/pip" install --quiet "mlx-whisper==0.4.3" numpy
  echo "✓ mlx-whisper מותקן"
fi

step "מוריד את מודל העברית של ivrit.ai (בערך 1.6GB, פעם אחת בלבד)"
MODEL_CACHE="$HOME/.cache/huggingface/hub/models--${MODEL//\//--}"
[[ -d "$MODEL_CACHE" ]] || touch "$APP_DIR/.model_downloaded_by_timmy"   # רק אז ההסרה תמחק את המודל
"$APP_DIR/venv/bin/python" -c "from huggingface_hub import snapshot_download; snapshot_download('$MODEL')" \
  || fail "הורדת המודל נכשלה. בדוק חיבור לאינטרנט והרץ שוב."
echo "✓ המודל מוכן"

step "מעתיק את קבצי המקליט"
cp "$HERE/live.py" "$APP_DIR/live.py"

step "שומר הגדרות"
CLAUDE_PATH="$(command -v claude || true)"
for c in "$HOME/.claude/local/claude" "$HOME/.local/bin/claude" /opt/homebrew/bin/claude /usr/local/bin/claude; do
  [[ -z "$CLAUDE_PATH" && -x "$c" ]] && CLAUDE_PATH="$c"
done
mkdir -p "${OUTDIR/#\~/$HOME}"
NAME="$NAME" ABOUT="$ABOUT" PEOPLE="$PEOPLE" OUTDIR="$OUTDIR" CLAUDE_PATH="$CLAUDE_PATH" FFMPEG="$FFMPEG" \
CFG="$APP_DIR/config.json" "$APP_DIR/venv/bin/python" - <<'PY'
import json, os
p = os.environ["CFG"]
cfg = {}
if os.path.exists(p):
    try: cfg = json.load(open(p, encoding="utf-8"))
    except Exception: cfg = {}
def setv(k, v):
    if v: cfg[k] = v
setv("owner_name", os.environ["NAME"].strip())
setv("owner_description", os.environ["ABOUT"].strip())
setv("output_dir", os.environ["OUTDIR"].strip())
setv("claude_path", os.environ["CLAUDE_PATH"].strip())
setv("ffmpeg_path", os.environ["FFMPEG"].strip())
if os.environ["PEOPLE"].strip():
    cfg["people"] = [x.strip() for x in os.environ["PEOPLE"].replace("\n", ",").split(",") if x.strip()]
cfg.setdefault("owner_name", "אני"); cfg.setdefault("people", [])
cfg.setdefault("clients_dir", ""); cfg.setdefault("meetings_dir", "")
json.dump(cfg, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("✓ הגדרות נשמרו:", p)
print("  שם:", cfg["owner_name"], "| תיקייה:", cfg.get("output_dir"), "| Claude:", cfg.get("claude_path") or "לא נמצא (יהיה רק תמלול, בלי סיכום)")
PY

step "בונה את האפליקציה"
pkill -f "טימי.app/Contents/MacOS/CallRecorder" 2>/dev/null || true
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$HERE/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
cp "$HERE/timmy.png" "$APP/Contents/Resources/timmy.png"
swiftc -O "$HERE/Recorder.swift" -o "$APP/Contents/MacOS/CallRecorder" 2>&1 | grep -E "error" && fail "הבנייה נכשלה"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>com.timmy.recorder</string>
<key>CFBundleName</key><string>טימי</string>
<key>CFBundleExecutable</key><string>CallRecorder</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleIconFile</key><string>AppIcon</string>
<key>CFBundleShortVersionString</key><string>1.0</string>
<key>LSUIElement</key><true/>
<key>LSMinimumSystemVersion</key><string>15.0</string>
<key>NSScreenCaptureUsageDescription</key><string>כדי להקליט את הקול של השיחה בכרום</string>
<key>NSMicrophoneUsageDescription</key><string>כדי להקליט גם את הקול שלך בשיחה</string>
</dict></plist>
PLIST
codesign --force --deep -s - "$APP" >/dev/null 2>&1
echo "✓ האפליקציה מותקנת: $APP"

step "מפעיל"
open "$APP"
echo ""
echo "✅ ההתקנה הסתיימה."
echo "   טימי נפתח עכשיו עם חלון הגדרה קצר שמסדר את ההרשאות (קול של כרום + מיקרופון)."
echo "   אחר כך הוא מחכה למעלה באמצע המסך, ליד הנוטש. לחיצה = התחלת הקלטה."
echo "   ההקלטות והסיכומים נשמרים ב: $OUTDIR"
