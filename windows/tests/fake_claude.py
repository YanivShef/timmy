"""Stand-in for the Claude Code CLI (`claude -p --model m`, prompt on stdin) in tests. Never calls a model.

Answers by prompt kind (the prompts come from mac/live.py through live_shared):
  key points -> two "- " lines, summary -> the SUMMARY_PROMPT format, restyle -> the document with one line added.
Every call is appended to $FAKE_CLAUDE_LOG as one JSON line (argv, cwd, kind, prompt) so tests can check the flags,
the working folder and that prompts went through stdin. $FAKE_CLAUDE_SLEEP delays the answer (timeout tests),
$FAKE_CLAUDE_PERSON / $FAKE_CLAUDE_TITLE change the summary answer.
"""
import json
import os
import sys
import time


def main():
    prompt = sys.stdin.buffer.read().decode("utf-8")
    if "אתה רושם נקודות עיקריות" in prompt:
        kind = "bullets"
    elif "כתוב מחדש את מסמך הסיכום" in prompt:
        kind = "restyle"
    elif "עם מי השיחה" in prompt:
        kind = "summary"
    else:
        kind = "other"
    log = os.environ.get("FAKE_CLAUDE_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "kind": kind, "prompt": prompt, "pid": os.getpid()},
                               ensure_ascii=False) + "\n")
    delay = float(os.environ.get("FAKE_CLAUDE_SLEEP") or 0)
    if delay:
        time.sleep(delay)
    if kind == "bullets":
        out = "- הצד השני רוצה לקבוע פגישה ביום שלישי\n- יש לדבר על הצעת המחיר לאתר"
    elif kind == "summary":
        person = os.environ.get("FAKE_CLAUDE_PERSON", "לא ידוע")
        title = os.environ.get("FAKE_CLAUDE_TITLE", "קביעת פגישה על הצעת מחיר")
        out = (f"עם: {person}\nכותרת: {title}\nסיכום:\nהצד השני ביקש לקבוע פגישה ביום שלישי בעשר בבוקר.\n"
               "הנושא הוא הצעת המחיר לאתר.\nמשימות:\n- לשלוח זימון לפגישה (אני)")
    elif kind == "restyle":
        doc = prompt.split("המסמך הנוכחי:\n", 1)[1].split("\n\nהתמלול:\n", 1)[0]
        lines = doc.split("\n")
        out = "\n".join(lines[:1] + ["", "**נכתב מחדש לפי ההוראות**"] + lines[1:]).strip()
    else:
        out = "מחובר"
    sys.stdout.buffer.write((out + "\n").encode("utf-8"))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
