"""Small Windows system helpers (ctypes / winreg), each importable on any OS.

winver.py     version gates for process loopback and WDA_EXCLUDEFROMCAPTURE (implemented)
affinity.py   SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE) for every Timmy window (build step 3)
startup.py    launch at login: HKCU\\...\\CurrentVersion\\Run value "Timmy" (build step 3)
disk.py       free space checks (500 MB to start, 200 MB to keep recording, same as the Mac) (implemented)
jobs.py       Job Objects: children without console windows, killed with their whole tree (implemented)
instance.py   single instance + "open again = show השיחות שלי" via a named local socket (build step 3)
"""
