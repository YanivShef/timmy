"""Chat logic shared by the chat windows, no Qt (build step 4, done).

looks.py       fast path for simple look requests (port of isSimpleLooks / colorNames / parseAppearance / applyLooks rules)
memory.py      <app dir>/memory.json {"notes": [...]}, max 40, same rules as the Mac
history.py     ChatMsg (the Mac JSON keys), chat-improve.json / chat-design.json / pdf/chat.json, the prompt history block
improve.py     "שפר את טימי" / "עצב את טימי": one `claude -p --model haiku` call -> JSON reply + actions, validated and applied
pdf_design.py  "עיצוב ה-PDF": pdf.py --design / --undo / --reset / --skill-setup, previews, logo, skill creation
skills.py      linked Claude skills (PDF design, writing voice), skill creation (writing voice, PDF design from a sample PDF)
"""
