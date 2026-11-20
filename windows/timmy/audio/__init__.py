"""Audio capture on Windows (build step 1, DESIGN.md 5).

wasapi.py     raw WASAPI / MMDevice COM via ctypes (vtable calls, the agile activation handler, Toolhelp32)
loopback.py   WASAPI process loopback of the chrome.exe process tree (MTA thread per Chrome root) + ChromeWatcher
mic.py        WASAPI shared capture of the default input, follows default-device changes
resample.py   any rate / channel layout -> 16 kHz mono float32 (soxr)
padder.py     wall-clock timeline per channel so both channels stay aligned (silence where nothing played)
procs.py      find Chrome browser root PIDs (Toolhelp snapshot / psutil)
fake.py       WAV-file sources for tests (same path as real streams)
capture.py    `python -m timmy.audio.capture`: frames (engine.protocol) on stdout, JSON status on stderr
"""
