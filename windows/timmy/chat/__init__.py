"""Chat logic shared by the chat windows, no Qt (build step 4).

looks.py      fast path for simple look requests (port of isSimpleLooks / colorNames / applyLooks rules)
memory.py     <app dir>/memory.json {"notes": [...]}, max 40, same rules as the Mac
history.py    chat-improve.json / chat-design.json and the prompt history block
improve.py    one `claude -p --model haiku` call -> JSON reply + actions, validated
skills.py     linked Claude skills (PDF design, writing voice) and skill creation
"""
