#!/bin/bash
# מסיר את טימי. ההקלטות והסיכומים נשארים.
pkill -f "טימי.app/Contents/MacOS/CallRecorder" 2>/dev/null || true
rm -rf "$HOME/Applications/טימי.app"
# המודל נמחק רק אם טימי הוא שהוריד אותו (אם הוא היה במחשב קודם, כלי אחר משתמש בו)
if [[ -f "$HOME/Library/Application Support/Timmy/.model_downloaded_by_timmy" ]]; then
  rm -rf "$HOME/.cache/huggingface/hub/models--mlx-community--ivrit-ai-whisper-large-v3-turbo-mlx"
fi
rm -rf "$HOME/Library/Application Support/Timmy"
defaults delete com.timmy.recorder 2>/dev/null || true
tccutil reset ScreenCapture com.timmy.recorder >/dev/null 2>&1 || true
tccutil reset Microphone com.timmy.recorder >/dev/null 2>&1 || true
echo "✓ טימי הוסר. ההקלטות נשארו ב-~/Documents/טימי - הקלטות"
