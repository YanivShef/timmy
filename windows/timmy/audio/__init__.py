"""Audio capture on Windows (build step 1).

loopback.py   WASAPI process loopback of the chrome.exe process tree (comtypes, MTA thread)
mic.py        WASAPI shared capture of the default input, follows default-device changes
resample.py   any rate / channel layout -> 16 kHz mono float32 (soxr)
padder.py     wall-clock silence padding so both channels stay on one timeline
procs.py      find Chrome browser root PIDs (Toolhelp snapshot / psutil)
capture.py    `python -m timmy.audio.capture`: frames (engine.protocol) on stdout, JSON status on stderr
"""
