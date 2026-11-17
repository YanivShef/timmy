#!/bin/bash
# מתקין את טימי, מקליט ומתמלל שיחות בעברית, על מק (Apple Silicon, macOS 15 ומעלה).
# בודק כל רכיב שצריך, ומוריד רק את מה שחסר. בלי סיסמת מנהל ובלי Homebrew.
#
# שימוש:
#   ./install.sh --name "השם שלך" [--about "תיאור קצר של העסק"] [--people "דנה כהן,אבי לוי"]
#                [--output-dir "~/Documents/טימי - הקלטות"] [--venv /path/to/existing/venv]
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$HOME/Library/Application Support/Timmy"
MODEL="mlx-community/ivrit-ai-whisper-large-v3-turbo-mlx"
PY_VERSION="3.12"

NAME=""; ABOUT=""; PEOPLE=""; OUTDIR="$HOME/Documents/טימי - הקלטות"; VENV=""; SHORTCUTS=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) NAME="$2"; shift 2;;
    --about) ABOUT="$2"; shift 2;;
    --people) PEOPLE="$2"; shift 2;;
    --output-dir) OUTDIR="$2"; shift 2;;
    --venv) VENV="$2"; shift 2;;
    --no-shortcuts) SHORTCUTS=0; shift;;
    *) echo "פרמטר לא מוכר: $1"; exit 1;;
  esac
done

step() { echo ""; echo "==> $1"; }
ok()   { echo "  ✓ $1"; }
get()  { echo "  ↓ $1"; }
fail() { echo ""; echo "❌ $1"; exit 1; }

# ---------------------------------------------------------------------------
step "בודק את המחשב"
[[ "$(uname -m)" == "arm64" ]] || fail "צריך מק עם שבב Apple (M1 ומעלה). מקים עם מעבד אינטל לא נתמכים."
MAJOR=$(sw_vers -productVersion | cut -d. -f1)
[[ "$MAJOR" -ge 15 ]] || fail "צריך macOS 15 (Sequoia) ומעלה. כרגע: $(sw_vers -productVersion). עדכון: הגדרות מערכת ← כללי ← עדכון תוכנה."
ok "מק $(sw_vers -productVersion) עם שבב Apple"
FREE_GB=$(df -g "$HOME" | awk 'NR==2{print $4}')
NEED_GB=4
[[ -d "$HOME/.cache/huggingface/hub/models--${MODEL//\//--}" ]] && NEED_GB=2
[[ -n "$VENV" ]] && NEED_GB=1
[[ "$FREE_GB" -ge "$NEED_GB" ]] || fail "צריך לפחות ${NEED_GB}GB פנויים בדיסק (יש ${FREE_GB}GB). פנה מקום והרץ שוב."
ok "${FREE_GB}GB פנויים בדיסק"
curl -sfI --max-time 15 https://huggingface.co >/dev/null || fail "אין חיבור לאינטרנט (צריך אותו להורדת מודל העברית). בדוק את החיבור והרץ שוב."
ok "יש חיבור לאינטרנט"

mkdir -p "$APP_DIR"

# ---------------------------------------------------------------------------
step "Python ומנוע התמלול"
UV=""
if [[ -n "$VENV" ]]; then
  [[ -x "$VENV/bin/python" ]] || fail "לא נמצא python ב-$VENV"
  rm -rf "$APP_DIR/venv"; ln -s "$VENV" "$APP_DIR/venv"
  ok "משתמש בסביבת Python קיימת: $VENV"
else
  # uv: מתקין Python ומנהל חבילות, לתוך תיקיית המשתמש, בלי סיסמה
  UV="$(command -v uv || true)"
  [[ -z "$UV" && -x "$HOME/.local/bin/uv" ]] && UV="$HOME/.local/bin/uv"
  if [[ -z "$UV" ]]; then
    get "מוריד את uv (מנהל Python קטן, בערך 20MB)"
    curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >/dev/null 2>&1 \
      || fail "ההורדה של uv נכשלה. בדוק אינטרנט והרץ שוב."
    UV="$HOME/.local/bin/uv"
    [[ -x "$UV" ]] || fail "uv לא הותקן כמו שצריך."
  fi
  ok "uv: $UV"
  if [[ -x "$APP_DIR/venv/bin/python" ]] && "$APP_DIR/venv/bin/python" -c "import mlx_whisper, numpy" 2>/dev/null; then
    ok "מנוע התמלול כבר מותקן"
  else
    get "מכין Python $PY_VERSION (אם אין במחשב, uv מוריד אותו)"
    rm -rf "$APP_DIR/venv"
    "$UV" venv --quiet --python "$PY_VERSION" "$APP_DIR/venv" || fail "יצירת סביבת Python נכשלה."
    get "מתקין את מנוע התמלול mlx-whisper (בערך 300MB)"
    "$UV" pip install --quiet --python "$APP_DIR/venv/bin/python" "mlx-whisper==0.4.3" numpy \
      || fail "ההתקנה של מנוע התמלול נכשלה. הרץ שוב (ההורדה ממשיכה)."
    ok "מנוע התמלול מותקן"
  fi
fi
"$APP_DIR/venv/bin/python" -c "import mlx_whisper, numpy, huggingface_hub" 2>/dev/null || fail "מנוע התמלול לא נטען. הרץ שוב את ההתקנה."

# ---------------------------------------------------------------------------
step "ffmpeg (לשמירת ההקלטה כ-mp3)"
FFMPEG=""
for f in "$(command -v ffmpeg || true)" /opt/homebrew/bin/ffmpeg /usr/local/bin/ffmpeg; do
  [[ -n "$f" && -x "$f" ]] && FFMPEG="$f" && break
done
if [[ -n "${TIMMY_TEST_NO_FFMPEG:-}" ]]; then FFMPEG=""; fi   # לבדיקות: מדמה מחשב בלי ffmpeg
if [[ -z "$FFMPEG" ]]; then
  if ! "$APP_DIR/venv/bin/python" -c "import imageio_ffmpeg" 2>/dev/null; then
    get "מוריד ffmpeg (בערך 30MB)"
    if [[ -n "$UV" ]]; then "$UV" pip install --quiet --python "$APP_DIR/venv/bin/python" imageio-ffmpeg
    else "$APP_DIR/venv/bin/python" -m pip install --quiet imageio-ffmpeg; fi
  fi
  FFMPEG="$("$APP_DIR/venv/bin/python" -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())")"
fi
[[ -x "$FFMPEG" ]] || fail "ffmpeg לא נמצא ולא הותקן."
ok "ffmpeg: $FFMPEG"

# ---------------------------------------------------------------------------
step "מודל העברית של ivrit.ai (בערך 1.6GB, פעם אחת בלבד)"
MODEL_CACHE="$HOME/.cache/huggingface/hub/models--${MODEL//\//--}"
if [[ -d "$MODEL_CACHE" ]]; then
  ok "המודל כבר במחשב, בודק שהוא שלם"
else
  touch "$APP_DIR/.model_downloaded_by_timmy"   # רק אז ההסרה תמחק את המודל
  get "מוריד את המודל, זה החלק הארוך"
fi
"$APP_DIR/venv/bin/python" -c "from huggingface_hub import snapshot_download; snapshot_download('$MODEL')" >/dev/null 2>&1 \
  || fail "הורדת המודל נכשלה. בדוק חיבור לאינטרנט והרץ שוב (ההורדה ממשיכה מאיפה שעצרה)."
ok "המודל מוכן"

# ---------------------------------------------------------------------------
step "Claude Code (בשביל הסיכומים והנקודות)"
CLAUDE_PATH="$(command -v claude || true)"
for c in "$HOME/.claude/local/claude" "$HOME/.local/bin/claude" /opt/homebrew/bin/claude /usr/local/bin/claude; do
  [[ -z "$CLAUDE_PATH" && -x "$c" ]] && CLAUDE_PATH="$c"
done
if [[ -z "$CLAUDE_PATH" ]]; then
  for c in "$HOME"/.nvm/versions/node/*/bin/claude; do [[ -x "$c" ]] && CLAUDE_PATH="$c"; done
fi
if [[ -n "$CLAUDE_PATH" ]]; then ok "Claude Code: $CLAUDE_PATH"
else echo "  ! Claude Code לא נמצא. טימי יתמלל, אבל בלי סיכומים ונקודות."; fi

# ---------------------------------------------------------------------------
step "מעתיק את קבצי טימי ושומר הגדרות"
cp "$HERE/live.py" "$APP_DIR/live.py"
mkdir -p "${OUTDIR/#\~/$HOME}"
NAME="$NAME" ABOUT="$ABOUT" PEOPLE="$PEOPLE" OUTDIR="$OUTDIR" CLAUDE_PATH="$CLAUDE_PATH" FFMPEG="$FFMPEG" \
CFG="$APP_DIR/config.json" "$APP_DIR/venv/bin/python" - <<'PY'
import json, os
p = os.environ["CFG"]
cfg = {}
if os.path.exists(p):
    try: cfg = json.load(open(p, encoding="utf-8-sig"))
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
print("  ✓ שם:", cfg["owner_name"], "| תיקייה:", cfg.get("output_dir"))
PY

# ---------------------------------------------------------------------------
step "מתקין את האפליקציה"
# תיקיית היישומים הרגילה (מופיע ב-Launchpad ובחיפוש); אם אין הרשאת כתיבה, תיקיית היישומים של המשתמש
if [[ -w /Applications ]]; then APP="/Applications/טימי.app"; else mkdir -p "$HOME/Applications"; APP="$HOME/Applications/טימי.app"; fi
pkill -f "טימי.app/Contents/MacOS/CallRecorder" 2>/dev/null || true
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
# בונים מהקוד אם יש כלי פיתוח של אפל; אחרת משתמשים בגרסה המוכנה מהריפו (לא צריך Xcode)
if [[ -z "${TIMMY_TEST_NO_SWIFTC:-}" ]] && xcode-select -p >/dev/null 2>&1 && command -v swiftc >/dev/null 2>&1 \
   && swiftc -O -target arm64-apple-macos15.0 "$HERE/Recorder.swift" -o "$APP/Contents/MacOS/CallRecorder" 2>/dev/null; then
  ok "נבנה מהקוד"
else
  cp "$HERE/bin/CallRecorder" "$APP/Contents/MacOS/CallRecorder"
  chmod +x "$APP/Contents/MacOS/CallRecorder"
  ok "גרסה מוכנה (בלי צורך בכלי פיתוח)"
fi
cp "$HERE/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
cp "$HERE/timmy.png" "$APP/Contents/Resources/timmy.png"
cp "$HERE/creator.png" "$APP/Contents/Resources/creator.png"
cp "$HERE/claude.png" "$APP/Contents/Resources/claude.png"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>com.timmy.recorder</string>
<key>CFBundleName</key><string>טימי</string>
<key>CFBundleDisplayName</key><string>טימי</string>
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
xattr -cr "$APP" 2>/dev/null || true
codesign --force --deep -s - "$APP" >/dev/null 2>&1 || fail "חתימת האפליקציה נכשלה."
ok "מותקן: $APP"

# ---------------------------------------------------------------------------
if [[ "$SHORTCUTS" == "1" ]]; then
  step "אייקון ב-Dock ובשולחן העבודה"
  # Dock: רק אם עוד לא שם
  if ! defaults read com.apple.dock persistent-apps 2>/dev/null | grep -q "com.timmy.recorder\|טימי.app"; then
    APP_URL="$(/usr/bin/python3 -c 'import sys,urllib.parse; print("file://" + urllib.parse.quote(sys.argv[1]) + "/")' "$APP")"
    defaults write com.apple.dock persistent-apps -array-add "<dict><key>tile-data</key><dict><key>file-data</key><dict><key>_CFURLString</key><string>$APP_URL</string><key>_CFURLStringType</key><integer>15</integer></dict></dict></dict>" \
      && killall Dock 2>/dev/null || true
  fi
  ok "נוסף ל-Dock"
  # קיצור דרך בשולחן העבודה
  # קישור סימבולי (לא צריך הרשאה לשלוט ב-Finder); מוצג עם האייקון של טימי
  [[ -e "$HOME/Desktop/טימי" ]] || ln -s "$APP" "$HOME/Desktop/טימי" 2>/dev/null || true
  ok "קיצור דרך בשולחן העבודה"
fi

# ---------------------------------------------------------------------------
step "בדיקה אחרונה"
"$APP_DIR/venv/bin/python" -c "import ast; ast.parse(open('$APP_DIR/live.py').read())" || fail "קובץ המנוע פגום."
[[ -x "$APP/Contents/MacOS/CallRecorder" ]] || fail "האפליקציה לא הותקנה."
ok "הכל מוכן"

[[ -n "${TIMMY_TEST_NO_OPEN:-}" ]] || open "$APP"
echo ""
echo "✅ ההתקנה הסתיימה."
echo "   טימי נפתח עכשיו עם חלון הגדרה קצר: הרשאות (קול של כרום + מיקרופון) ובחירת תיקייה."
echo "   אחר כך הוא מחכה למעלה באמצע המסך, ליד הנוטש. לחיצה = התחלת הקלטה."
