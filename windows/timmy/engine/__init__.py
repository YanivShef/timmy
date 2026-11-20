"""Recording / transcription / summary / PDF engine (headless, no Qt).

protocol.py   frame + JSON-lines protocol shared with mac/live.py (implemented)
worker.py     port of mac/live.py on faster-whisper: `python -m timmy.engine.worker <base> | --restyle <md>` (implemented)
asr.py        faster-whisper backend, CPU/CUDA selection, cpu/fast profiles, merge + split (implemented)
claude.py     find claude.exe / claude.cmd, run `claude -p` without a console window, kill the tree (implemented)
chrome_tabs.py active tab titles of the Chrome windows, for the summary prompt (implemented)
files.py      call file names (Mac + Windows rules) and the mp3 mix (implemented)
_mac/         code synced verbatim from mac/ by tools/sync_mac.py, with a drift test (build steps 1 to 3)
"""
