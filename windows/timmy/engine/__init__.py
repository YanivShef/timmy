"""Recording / transcription / summary / PDF engine (headless, no Qt).

protocol.py   frame + JSON-lines protocol shared with mac/live.py (implemented)
worker.py     port of mac/live.py on faster-whisper (build step 2)
asr.py        faster-whisper backend, CPU/CUDA selection, adaptive segmentation (build step 2)
claude.py     find claude.exe / claude.cmd, run `claude -p` without a console window, kill the tree (build step 2)
_mac/         code synced verbatim from mac/ by tools/sync_mac.py, with a drift test (build steps 1 to 3)
"""
