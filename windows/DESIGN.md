# Timmy for Windows: design

Status: all 4 build steps (audio capture, worker and engine, core UI, integrations / chats / onboarding / installer)
implemented and tested off Windows; waiting for the first Windows CI run (see below). Implementation split into 4 build
steps (section 13); 13.4 lists what step 4 left open (images in a call's PDF).
Branch: `windows` (from `main`). Everything Windows lives under `windows/` plus the CI workflow.

**CI is not active yet.** The workflow is committed as `windows/ci/windows.yml` because the GitHub token on the dev
machine lacks the `workflow` scope (GitHub rejects pushes that add `.github/workflows/*` without it). One-time fix by
the repo owner: `gh auth refresh -h github.com -s workflow`, then move the file to `.github/workflows/windows.yml`
(exact commands at the top of the file). Until then the build steps can only be verified locally (non-Windows tests).
Still the case after build steps 1 to 4 (checked 2026-11-21: the token scopes are `admin:org, gist, repo`), so the
`windows_only` tests (real COM / WASAPI / Toolhelp / Job Objects / EnumWindows / display affinity / the Run key /
Credential Manager / install.ps1 on the runner) and the `installer` job have **never run yet**. Everything else runs on any OS and passes; see 13.1 to 13.3 for exactly what is and is
not proven.
The Mac app (`mac/`) is not touched by this work.

## 0. Prior art: the `windows-beta` branch

A local, never-pushed branch `windows-beta` (3 commits, base `b7de57b`, about 12k lines) already contains a first Windows
attempt: a C# `timmy-capture.exe` (process loopback + mic, same frame protocol), a faster-whisper worker
(`live_core.py`, `asr.py`), PySide6 UI (`ui.py`, `widgets.py`, `calls_window.py`, `onboarding.py`), a Python Greenolink
client, chat logic, PyInstaller + Inno Setup packaging and 121 tests. Its own README says it was **never run on a
Windows machine** and it had no CI. It also predates the last 5 Mac commits (light themes, memory, linked skills,
screen-share hiding, skill creation, images in the PDF).

Decision: the new branch starts clean from `main` with the layout below, and each build step **may harvest** code from
`windows-beta` (`git show windows-beta:windows/app/<file>`) when it fits, but nothing counts as done until it passes
in CI on `windows-latest`. Useful pieces to look at first: `capture/Interop.cs` (COM vtable layouts, already matching the
Microsoft sample), `app/greenolink.py`, `app/chatlogic.py`, `app/asr.py` (model download with resume/size check),
`app/platform_utils.py`.

## 1. Goals and constraints

* Feature parity with the Mac app (list in section 4), Hebrew RTL UI, never the long dash character.
* Target: Windows 10 22H2 (build 19045) and Windows 11, x64. Hard minimum build 19041 (Windows 10 2004), see 5.1.
* Non-technical users: no admin rights, no console windows, no terminal steps. Installs per user into
  `%LOCALAPPDATA%\Timmy`.
* Same `config.json` keys and the same worker protocol as the Mac, so INSTALL.md style instructions ("Claude, change
  the accent color") work identically and the UI layer stays thin.
* Public repo: no secrets, no real names/phones/emails in code or fixtures (a test scans `windows/`).

## 2. Process architecture

```
 timmy UI process (pythonw -m timmy, PySide6, STA main thread)
   |  spawns per call, CREATE_NO_WINDOW, all children in one Job Object (kill-on-close)
   |
   +--> capture process   python -m timmy.audio.capture --out-fd ...
   |      MTA thread: WASAPI process loopback of chrome.exe tree  -> channel 0
   |      MTA thread: WASAPI default mic (follows default device)  -> channel 1
   |      resample -> 16 kHz mono float32, wall-clock silence padding
   |      stdout: frames  [1B ch][u32 LE n][n x f32 LE]   ==== OS pipe ====+
   |      stderr: JSON status lines (levels, device switch, chrome lost)   |
   |                                                                       v
   +--> worker process    python -m timmy.engine.worker "<base path>"   (stdin = the pipe above)
   |      faster-whisper ivrit.ai CT2, echo handling, bullets via claude -p, summary/title/person/tasks,
   |      rename files, restyle. stdout: JSON lines (loading/ready/partial/final/bullets/saving/done/diskfull)
   |
   +--> claude -p ...     (bullets, summary, PDF content, chats, WhatsApp drafts) - from worker / pdf engine / UI
   +--> pdf engine        python -m timmy.engine.pdf ... (vendored mac/pdf.py), HTML -> PDF in the UI via QtWebEngine
```

Why three processes: COM audio threads, the CPU-heavy CTranslate2 inference and the Qt GUI never share a GIL or a COM
apartment; a crash in capture or ASR cannot take the UI down; and the worker keeps the exact stdin/stdout contract of
`mac/live.py`, so it is testable headless by piping a WAV through it. The capture stdout is connected straight to the
worker stdin (the UI does not copy audio), the UI reads only the small JSON streams. Stop = UI writes `stop` to the
capture stdin, capture flushes and exits, worker sees EOF and saves (the Mac flow). Cancel = kill the Job, delete
`<base>*` files.

## 3. Module layout

```
windows/
  DESIGN.md                this file
  pyproject.toml           deps as extras: audio / asr / ui / system / dev ; pytest + ruff config
  install.ps1              per-user installer, PowerShell 5.1, UTF-8 with BOM, CRLF (.gitattributes)   [step 4, done]
  uninstall.ps1            removes app, venv, model, shortcuts, Run key, WhatsApp tokens; recordings stay  [step 4, done]
  timmy/
    __main__.py            entry: python -m timmy [--background | --show-calls] runs the app; --report / --version  [step 3, done]
    paths.py               %LOCALAPPDATA%\Timmy (TIMMY_APP_DIR override), Documents\טימי - הקלטות     [done]
    onboard.py             onboarding checks without Qt: mic privacy + test capture, Chrome, Claude Code
                           detect / auth status / login in the background, Git for Windows, folder        [step 4, done]
    install_helper.py      the Python half of install.ps1: config / model / icon / startup / claude / cleanup [step 4, done]
    config.py              config.json load/save (utf-8-sig, atomic, keep unknown keys, never overwrite output_dir) [done]
    engine/
      protocol.py          frame codec + JSON-lines events, byte-identical to mac/live.py           [done]
      worker.py            port of mac/live.py (same CLI: <base> | --restyle <md>)                  [step 2, done]
      asr.py               faster-whisper backend, device pick, cpu/fast profiles, merge + split     [step 2, done]
      claude.py            find claude.exe/claude.cmd, run without console, Job Object kill          [step 2, done]
      chrome_tabs.py       active tab titles of Chrome windows (EnumWindows), replaces osascript     [step 2, done]
      files.py             call file names (Mac + Windows rules, MAX_PATH), mp3 mix via ffmpeg       [step 2, done]
      pdf.py               python -m timmy.engine.pdf: runs the vendored mac/pdf.py as a script      [step 3, done]
      pdf_win.py           what the patched pdf.py calls: Claude finder, run() in a Job, flock shim,
                           skill interpreters / PATH                                                  [step 3, done]
      procrun.py           run_process / run_claude with CancelToken + Job (the Mac runProcess / runClaude),
                           last_json_object, split_sections                                          [step 4, done]
      _mac/                files generated by tools/sync_mac.py from mac/ (never edited by hand)    [steps 1-3]
    audio/
      wasapi.py            raw ctypes COM: vtable calls, agile activation handler, Toolhelp32       [step 1, done]
      loopback.py          process loopback per Chrome root + ChromeWatcher (attach / re-attach)    [step 1, done]
      mic.py               default-input capture, default-device follow, invalidation recovery      [step 1, done]
      resample.py          soxr streaming resampler to 16 kHz mono float32                          [step 1, done]
      padder.py            wall-clock timeline per channel (Timeline, SourceClock, Feed)            [step 1, done]
      procs.py             Chrome browser root PIDs (Toolhelp32 snapshot, psutil fallback)          [step 1, done]
      fake.py              WAV-file sources (tests, demos), same path as real streams               [step 1, done]
      capture.py           the capture process (__main__), status JSON on stderr                    [step 1, done]
    winsys/
      winver.py            version gates (19041)                                                    [done]
      affinity.py          SetWindowDisplayAffinity WDA_EXCLUDEFROMCAPTURE (+ read back)             [step 3, done]
      startup.py           HKCU Run key "Timmy" = "<venv>\pythonw.exe" -m timmy --background         [step 3, done]
      disk.py              free-space checks (500 MB start / 200 MB keep, same as Mac)              [step 2, done]
      jobs.py              Job Object helper (the Windows twin of killpg / start_new_session)       [step 2, done]
      instance.py          single instance via QLocalServer; second launch opens "השיחות שלי"       [step 3, done]
    ui/                    PySide6 Widgets (see 7)                                                    [steps 3-4]
      app.py               QApplication, tray + menu, ShareHider event filter, launch at login        [step 3, done]
      theme.py             port of Theme (contrast rules, light backgrounds) + the Qt stylesheet       [step 3, done]
      paint.py, island.py  the pill / status / recording card (QPainter ports of the SwiftUI views)   [step 3, done]
      recorder.py          the call controller: capture + worker in one Job, events -> card state     [step 3, done]
      calls.py             calls model: list, parse, search, rename with rollback (no Qt)            [step 3, done]
      markdown.py          summary Markdown -> Qt rich text (port of parseMarkdown)                   [step 3, done]
      calls_window.py      "השיחות שלי"                                                               [step 3, done]
      pdf_render.py        engine -> Timmy line -> page footer -> QtWebEngine printToPdf              [step 3, done]
      shell.py             Explorer /select, open file / folder (TIMMY_DRY_OPEN log for tests)        [step 3, done]
      bridge.py            spawn(fn, done) on a thread + notify() from any thread -> Qt main thread    [step 4, done]
      chat_window.py       "שפר את טימי" / "עצב את טימי" (+ "מה אני זוכר") and "עיצוב ה-PDF"            [step 4, done]
      send_whatsapp.py     "שליחה בוואטסאפ": connect / choose / draft / review / confirm / pick / sent  [step 4, done]
      onboarding.py        "הרשאות והגדרה (טימי)": 6 steps                                              [step 4, done]
      assets/              timmy.png, creator.png (copies of mac/*.png)
    chat/                  looks fast path, memory.json, chat history, improve/design calls, PDF design chat,
                           linked skills + skill creation                                            [step 4, done]
    integrations/          secrets (keyring, chunked), greenolink (OAuth PKCE + MCP), whatsapp (send flow) [step 4, done]
  tests/                   pytest; fixtures/he_short.wav (6.3 s) + he_reply.wav (4.0 s): synthetic TTS voice
                           (macOS "Carmit"), 16 kHz mono, no real person; fake_claude.py; worker_feed.py
  tools/
    bench_asr.py           model speed on the current machine (CI job asr-bench)                    [done]
    sync_mac.py            regenerate timmy/engine/_mac/ from mac/ (+ drift test)                   [step 1, done]
```

UI toolkit: **PySide6 Widgets**, not QML. Reasons: `QWidget.grab()` gives deterministic offscreen screenshots for CI,
custom painting of the pill (eyes, mouth wave) is simple `QPainter` code ported from the SwiftUI shapes, and RTL is
`app.setLayoutDirection(Qt.RightToLeft)`. Full `PySide6` (not `-Essentials`) because QtWebEngine is in the Addons.

## 4. Feature parity map

| Feature (Mac) | Mac implementation | Windows implementation | Risk / note |
|---|---|---|---|
| One-click record, Chrome audio + mic only, separate channels | ScreenCaptureKit app filter + AVAudioEngine, frames into live.py | WASAPI process loopback (chrome.exe tree) + WASAPI mic in `timmy.audio.capture`, same frames | High: process loopback through raw ctypes COM is the main technical risk until it runs on Windows (5.1). Fallback: harvest the C# helper from windows-beta |
| Live Hebrew transcript card, speaker tags | live.py partial/final events, SwiftUI card | Same events from worker.py, `ui/card.py` | Medium: CPU speed limits live partials (6.3) |
| Live key points | claude -p haiku every 30 s | Same code path (shared prompt) | Low |
| Cancel (two-step) | "למחוק?" confirm, kill + delete | Same UX; kill Job Object, delete `<base>*` | Low |
| Hide live transcript (compact mode) | `hide_live_transcript` | Same key, card collapses to the pill with timer | Low |
| Mic switch mid-call | default-input listener, pad silence | Poll default capture endpoint id every 500 ms + `AUDCLNT_E_DEVICE_INVALIDATED`; reopen, pad gap | Medium: Bluetooth headsets switch profile (HFP) and both endpoints change |
| Summary / title / person / tasks, rename | live.py end of call | Same prompts (synced from mac/live.py), same file names | Low. File names: strip `\/:*?"<>|`, also trailing dots/spaces, reserved names (CON, NUL...), keep path < 260 |
| "השיחות שלי" (list, search, summary, rename, PDF, transcript, open folder) | SwiftUI window | `ui/calls_window.py`; markdown via a small md->HTML (reuse the vendored pdf.py md_to_html) in `QTextBrowser`; "open folder" = `explorer /select,` | Low |
| Branded client PDF + PDF design chat | pdf.py (Claude HTML) + WKWebView print | Vendored pdf.py unchanged in logic, QtWebEngine `printToPdf` A4 | Medium: footer + page numbers per page (8) |
| Images in the PDF, video frames from Drive (latest Mac commit) | Recorder.swift + pdf.py | pdf.py part comes for free; picking/dragging images in step 4; video frames via imageio-ffmpeg; Drive for desktop path is `G:\` / `%USERPROFILE%\My Drive` | Medium, lowest priority of step 4 |
| WhatsApp via Greenolink | OAuth 2.1 PKCE loopback + MCP HTTP, Keychain | Same flow in Python (`http.server` on 127.0.0.1:random for the callback, `urllib`/`requests` for MCP), keyring | Low. Credential Manager blob limit (9) |
| Draft -> confirm recipient -> send, never without click | SwiftUI overlay | Qt overlay dialog; the send call only exists in the click handler, a test asserts no send without it | Low |
| "שפר את טימי" / "עצב את טימי" chats, context, memory.json, linked skills | Recorder.swift chat logic | `timmy/chat/*` port with the same JSON action schema and prompt text; memory.json shared format | Medium: lots of Hebrew heuristics (isSimpleLooks etc.) to port 1:1, covered by table-driven tests |
| Themes incl. light backgrounds | Theme.apply, contrast rules | `ui/theme.py` port (luminance, accentOn, eye contrast) + Qt stylesheets regenerated live | Low |
| Hidden from screen sharing | `sharingType = .none` | `SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE=0x11)` on every top-level window incl. menus/tooltips (7.3) | Low on 19041+; on older builds the call fails and we leave it off (WDA_MONITOR would show a black box) |
| Onboarding (permissions, folder, Claude Code check/login) | SwiftUI, TCC prompts | Mic privacy check (registry consent store + a 1 s test capture), Chrome check, Claude Code find + `auth status --json` + `auth login --claudeai` in background, Git for Windows check (9), folder + free space | Medium: Windows has no prompt for desktop-app mic access, only the Settings switch "Let desktop apps access your microphone" |
| Launch at login | SMAppService | `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\Timmy = "<venv>\pythonw.exe" -m timmy --background` | Low |
| Disk-space checks | 500 MB start / 200 MB keep, every 20 s | `shutil.disk_usage` on the output dir, same constants; worker already emits `diskfull` on OSError | Low |
| Tray / menu (no notch) | right-click on the pill | `QSystemTrayIcon` + the same `QMenu` on pill right-click: השיחות שלי, פתח תיקייה, שפר את טימי, עצב את טימי, הרשאות והגדרה, יציאה | Low. Win11 hides new tray icons under ^, onboarding says so |

## 5. Audio capture in detail

### 5.1 Process loopback (Chrome only)

API (verified against Microsoft's `ApplicationLoopback` sample, `LoopbackCapture.cpp`, and the
`AUDIOCLIENT_ACTIVATION_PARAMS` reference):

```c
AUDIOCLIENT_ACTIVATION_PARAMS p = {0};
p.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK;            // = 1
p.ProcessLoopbackParams.TargetProcessId = chromeBrowserPid;
p.ProcessLoopbackParams.ProcessLoopbackMode = PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE;  // = 0
PROPVARIANT v = {0}; v.vt = VT_BLOB; v.blob.cbSize = sizeof(p); v.blob.pBlobData = (BYTE*)&p;     // 12-byte blob
ActivateAudioInterfaceAsync(L"VAD\\Process_Loopback",                       // VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK
                            &IID_IAudioClient, &v, completionHandler, &asyncOp);
// completionHandler->ActivateCompleted(op): op->GetActivateResult(&hr, &unk) -> IAudioClient
client->Initialize(AUDCLNT_SHAREMODE_SHARED,
                   AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM
                   | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY,
                   0, 0, &fmt /* we choose it */, NULL);
client->SetEventHandle(evt); client->GetService(IID_IAudioCaptureClient, &cap); client->Start();
loop: WaitForSingleObject(evt, 100); while (cap->GetNextPacketSize(&n), n) { cap->GetBuffer(...); ...; cap->ReleaseBuffer(frames); }
```

Implementation in Python (`timmy/audio/wasapi.py` + `loopback.py`), **plain ctypes, no comtypes** (decided in build
step 1, see "Decision" below):

* Every thread that touches audio objects calls `com_init()` (`CoInitializeEx(NULL, COINIT_MULTITHREADED)`) and
  `com_uninit()`; this happens only in the capture process. Each COM method is one vtable slot called through
  `WINFUNCTYPE` with the declared signature (`vcall(ptr, slot, restype, argtypes, ...)`, slot numbers listed in the
  module docstring: `IAudioClient` 1CB9AD4C-..., `IAudioCaptureClient` C8ADBD64-..., `IMMDeviceEnumerator`,
  `IMMDevice`, `IPropertyStore`, `IActivateAudioInterfaceAsyncOperation` 72A22D78-...). HRESULTs are `int32`, checked
  by hand, so success codes like `AUDCLNT_S_BUFFER_EMPTY` pass.
* The completion handler (`ActivationHandler`) is a `PyComObject`: a 4-slot vtable of ctypes callbacks
  (QueryInterface / AddRef / Release / ActivateCompleted) that answers `IUnknown`,
  `IActivateAudioInterfaceCompletionHandler` and `IAgileObject` (94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90); without an
  agile handler activation fails with `E_ILLEGAL_METHOD_CALL`. ActivateCompleted calls `GetActivateResult`, stores
  the HRESULT and the interface, and sets a `threading.Event`; the caller waits up to 5 s. Handlers stay referenced
  (`_KEEP`) until Windows released them, so a late Release never hits freed memory.
* **Decision (end of step 1): plain ctypes**, not comtypes and not (yet) the C# helper. Reasons: the code maps 1:1 to
  the Microsoft sample; no apartment/`coinit_flags` side effects or generated modules; no finalizers releasing COM
  pointers on random threads; and the whole call machinery is testable on any OS, because the tests build fake COM
  objects with the same `PyComObject` class (`tests/fakecom.py`: fake IAudioClient / IAudioCaptureClient /
  IMMDeviceEnumerator / IMMDevice / IPropertyStore / async operation). That covers argument marshalling,
  out-pointers, 64-bit values and callbacks, but not Windows' own behavior: the `windows_only` tests
  (`tests/test_wasapi_windows.py`) do that on the runner. If they show a problem ctypes cannot solve, the C# helper
  from `windows-beta` is still the fallback (same frame protocol, so nothing else changes).
* `ActivateAudioInterfaceAsync` is called through `ctypes.WinDLL("Mmdevapi")` with the PROPVARIANT built by hand
  (x64 layout: `vt` at 0, `cbSize` at 8, `pBlobData` at 16, total 24 bytes).
* Format: do not rely on `GetMixFormat` (not implemented for the process-loopback client in practice; the sample also
  sets its own). Try in order with AUTOCONVERTPCM: float32 16 kHz mono (no resampling needed), PCM16 48 kHz stereo,
  PCM16 44.1 kHz stereo (the sample's format), each on a fresh activation. Whatever succeeds is resampled by
  `resample.py` when needed. Shared mode, event callback, 1 s buffer (a late Python thread loses nothing),
  periodicity 0. Packet time = the device's `u64QPCPosition` (100 ns QPC units, same scale as `padder.clock()`) when
  it is sane, otherwise `now - duration`.
* Silence: when Chrome renders nothing, the stream yields silent packets (`AUDCLNT_BUFFERFLAGS_SILENT`) or none; the
  padder (5.3) covers both.
* Which PID: Chrome plays audio from its audio-service utility process (`chrome.exe --type=utility
  --utility-sub-type=audio.mojom.AudioService`), a child of the browser process, so we target **every chrome.exe whose
  parent is not chrome.exe** (one per running profile directory / instance) with INCLUDE_TARGET_PROCESS_TREE.
  Several roots = several clients, summed and clipped into channel 0.
  Chrome not running at start: the card says "כרום סגור" (Mac wording) but recording still starts with the mic; a
  watcher rescans every 2 s and attaches when Chrome appears or restarts (root PID changed).
* Version gate: the reference page lists "Windows 10 Build 20348" (the SDK header), but the feature works from Windows
  10 2004 / build 19041 (OBS's application audio capture has the same minimum). `winver.MIN_BUILD = 19041`; any
  activation HRESULT failure is reported as `LOOPBACK_UNSUPPORTED` with the HRESULT on stderr, and the onboarding
  says to update Windows. CI runners are Server 2022/2025 (20348+), so activation itself is testable there.
* Fallback if the ctypes layer proves unreliable on real Windows: build the C# helper from `windows-beta`
  (`dotnet publish` single-file, about 70 MB) in CI and ship it; the frame protocol is identical, so nothing else
  changes.
* Status events (stderr): `{"event": "chrome", "state": "attached" | "closed" | "unsupported" | "error"}` from the
  watcher (the card shows "כרום סגור" on closed), `{"event": "chrome_stream", ...}` per stream (format, tried
  formats, HRESULTs). A failed stream is retried after 5 s; activation failures `E_NOTIMPL`, `E_INVALIDARG`,
  `REGDB_E_CLASSNOTREG`, `E_NOINTERFACE`, `E_ILLEGAL_METHOD_CALL` mean "unsupported" and stop the retries.

### 5.2 Microphone

Refinement of the plan: the mic also uses **WASAPI shared mode through the same ctypes layer**, not sounddevice.
Reason: PortAudio keeps the device list from `Pa_Initialize`, so following a new default device (headset plugged in or
disconnected) needs a full library re-init; with WASAPI we just reopen the new default endpoint.
`IMMDeviceEnumerator::GetDefaultAudioEndpoint(eCapture, eConsole)`; same format negotiation as 5.1; poll the
default endpoint id every 500 ms; on change or `AUDCLNT_E_DEVICE_INVALIDATED` (0x88890004) close, reopen, and let the
padder fill the gap. No device at all: channel 1 is padded silence and the card shows "בלי מיקרופון".
Formats: 16 kHz mono float with AUTOCONVERTPCM first, then the device mix format, then common fallbacks. Status
events: `{"event": "mic", "state": "open" | "switched" | "lost" | "none" | "denied" | "error", "name", "id", ...}`;
`denied` (E_ACCESSDENIED at Activate) is the privacy switch, retried every 3 s.
`sounddevice` stays as a dependency only for the onboarding level meter fallback and dev tools.
Mic privacy: if `HKCU\Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone\
NonPackaged` is `Deny`, or the test capture is all zeros, onboarding opens `ms-settings:privacy-microphone`.

### 5.3 Resampling and padding

* `resample.py`: interleaved int16/float32, any channel count -> mono average -> `soxr.ResampleStream(in_rate, 16000,
  1, dtype="float32", quality="HQ")`; identity when the device already gives 16 kHz mono float32.
* `padder.py` (as built): one `Timeline` per channel. Every source (`Feed` = converter + `SourceClock`) places its
  16 kHz samples at a position derived from its packet timestamps: contiguous while the stream flows, a jump forward
  (zeros in between) when a packet comes more than 0.25 s later than the audio so far explains (Chrome silent,
  device switch, Chrome restart). Output is timed from the stream's anchor plus samples produced, not from the
  packet that flushed it out of soxr (soxr releases output in bursts 30 to 40 ms late). Several sources on one
  channel (two Chrome instances) are summed and clipped. The writer takes everything up to `now - 0.3 s` (jitter
  buffer) every 100 ms; what nobody delivered is silence. Real audio is never dropped: a packet later than its slot
  is placed right after the written part (counted as `late_samples` in the `stopped` event). At stop both channels
  are flushed to the same final sample. This keeps `[hh:mm:ss]` timestamps and the echo detector (which compares the
  two channels by position) right across device switches and Chrome restarts.
* Frames of about 100 ms (1600 samples) per channel are written with `protocol.encode_frame` to stdout.

## 6. Transcription

### 6.1 Model

`ivrit-ai/whisper-large-v3-turbo-ct2` (verified on the Hugging Face API, 2026-11-20: files `config.json`, `model.bin`
1,617,884,968 bytes, `preprocessor_config.json`, `tokenizer.json`, `vocabulary.json`; about 58k downloads/month).
It is the CTranslate2 conversion of the same ivrit.ai turbo model the Mac uses through MLX
(`mlx-community/ivrit-ai-whisper-large-v3-turbo-mlx`), so transcripts match. `ivrit-ai/whisper-large-v3-ct2` (non
turbo) also exists but is far too slow on CPU. Config key `asr_model` keeps it configurable; tests use
`Systran/faster-whisper-tiny` (75 MB) for plumbing and the real model only in the on-demand job.
`faster-whisper==1.2.1`, `ctranslate2==4.9.0`. Compute type: `int8` on CPU (weights quantized at load time),
`float16` on CUDA. Download with `huggingface_hub.snapshot_download` into `%LOCALAPPDATA%\Timmy\models` with resume
and a size check of `model.bin` (logic exists in `windows-beta:windows/app/asr.py`).

### 6.2 Measured speed (int8, beam 1, `language="he"`)

| Machine | Threads | load | 2 s audio | 6.3 s audio | 14 s audio |
|---|---|---|---|---|---|
| Apple M2 Max (CPU only, for reference) | 8 | 1.2 s | 3.0 s | 3.2 s | 3.6 s |
| Apple M2 Max (CPU only) | 4 | 2.4 s | 4.9 s | 5.2 s | 5.5 s |
| GitHub `windows-latest` (4 vCPU) | see the `asr-bench` job results below | | | | |

Transcription of the fixture was exact ("שלום, אני רוצה לקבוע פגישה ביום שלישי בשעה 10 בבוקר כדי לדבר על הצעת
המחיר לאתר."). The key fact: **cost per call is almost flat**, because Whisper always encodes a padded 30 s window
(32 encoder layers; the turbo decoder is only 4 layers). A typical 4 to 6 core Windows laptop will need roughly
5 to 10 s per call.

### 6.3 Live policy (the one behavior that differs from the Mac)

The Mac re-transcribes the pending utterance every 1.2 s for partials and finalizes at 2 s + silence or 14 s. On a
CPU that costs 5 to 10 s per call this would fall behind within a minute. `asr.py` picks a profile at startup from a
1 s warm-up timing (and from `asr_device`):

* **fast** (CUDA, or warm-up call < 1.0 s): the Mac parameters unchanged (partials every 1.2 s, max 14 s).
* **cpu**: no text partials (the card shows the "מתמלל…" thinking state per speaker, exactly the Mac's unreliable
  partial state); one ASR thread with a queue; when it becomes free it takes **everything ready** for a channel up to
  28 s and transcribes it in one call, then splits the result back into lines by the segment timestamps faster-whisper
  returns. Short turns are therefore merged when the machine is busy and stay separate when it is idle. Both channels
  ready at once: one `BatchedInferencePipeline` call with batch 2 (cheaper than 2 calls). Worst case RTF is about
  10 s / 28 s per channel, so it always keeps up; the price is that a line appears 5 to 15 s after it was said.
* The hallucination filters (`dedupe`, `final_ok`, `segments_info` thresholds) are the Mac ones, synced from
  mac/live.py. faster-whisper segments expose the same fields (`avg_logprob`, `compression_ratio`, `temperature`).
* Echo handling: identical (`echo_score`, `is_echo`, end-of-call `_echo_line`), synced code.
* As built in step 2 (refinements of the above):
  * no `BatchedInferencePipeline`: it batches VAD chunks of *one* audio, not two channels. One ASR thread takes the
    channel whose oldest queued utterance is the earliest (lines come out roughly in time order, so the text echo
    check still sees the other side's line first) and merges up to 28 s of that channel (`asr.merge_budget`,
    0.6 s of silence between pieces), timestamps on; `asr.split` gives each segment to the piece its midpoint is in.
  * Whisper reports `compression_ratio` per 30 s window, so in a merged call each piece gets the ratio of **its own
    text** (the faster-whisper formula); otherwise two similar sentences in one window (measured: 2.51 for the fixture
    twice) push the whole window over the Mac's 2.4 and every line of it is dropped. `avg_logprob` / temperature stay
    per window. A single piece keeps the window values (= the Mac path).
  * The sound echo check (`echo_score`) runs **before** queueing, while the 120 s Chrome history still covers the
    utterance. The text echo check runs when the line is made; when the ASR thread happens to make the mic line
    before the other side's line, the end-of-call `_echo_line` filter still removes it from the transcript.
  * End of call with a long backlog (stopped while the model was still loading, measured 5 to 18 s load on CPU):
    the remainder is cut into the usual <= 14 s pieces first, instead of one long utterance as on the Mac (which
    then loses per-line timestamps and, past 30 s, is split by Whisper itself).
  * The worker logs its choice to stderr: `{"event": "asr", "backend", "device", "compute_type", "threads",
    "warmup_s", "profile", "claude"}`. Config `asr_profile` (fast | cpu | auto) and `asr_threads` override.
* CUDA: used only if `ctranslate2.get_cuda_device_count() > 0` **and** a 1 s test transcription succeeds (missing
  cuBLAS/cuDNN DLLs otherwise crash at first use). The installer adds `nvidia-cublas-cu12` + `nvidia-cudnn-cu12`
  wheels only when `nvidia-smi` exists and there is space (about 1.5 GB more). Otherwise CPU, `cpu_threads` = physical
  cores, process priority BELOW_NORMAL so the call itself (Chrome) never stutters.

## 7. UI

### 7.1 Pill and card

Frameless `QWidget` with `Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus`,
`WA_TranslucentBackground`, `WA_ShowWithoutActivating` (no taskbar button, never steals focus from the call). Rest
state: a small black rounded pill centered at the top of the screen that holds the cursor at start (default primary),
y = 0, with the eyes + mouth wave painted with QPainter (port of the "Dynamic Island" face; the notch is simply the
pill). Click = start. Recording: animates (QPropertyAnimation on geometry) down into the card: timer, stop, eye
(hide transcript), x (cancel, two-step "למחוק?"), live lines with speaker tags, key points column. Per-monitor DPI v2
is Qt 6's default; positions are recomputed on `screenChanged` and display changes.

As built in step 3 (`ui/island.py`, `ui/paint.py`, `ui/recorder.py`):
* No notch on Windows, so the black strip between the eyes is 72 px wide (the Mac notch is about 180) and the pill is
  30 px high: rest = 180 x 30 at y = 0, centered on the screen under the cursor; hover grows the wings by 6 px, makes
  the eyes big and shows the mouth below the strip. The window has fixed transparent margins for the shadow (26 px
  sides, 40 px below), so the pill never moves while it animates. Window sizes: pill 452 x 86, card 668 x 258,
  hidden transcript 668 x 78.
* The header strip is laid out left to right like the Mac HStack (`setLayoutDirection(LeftToRight)`), Hebrew strings
  get an RLM; header controls (stop, eye, x / "למחוק?") are painted and hit-tested from the rectangles of the last
  paint (the Mac `tapRects`, with the same 6 px tolerance). Body = child widgets: key points (220 px) and the live
  transcript, both rich-text labels in scroll areas that stick to the bottom, with the Mac colors and speaker tags.
* Animations (blink every 2.8 to 5.5 s, mouth wave, pulsing dot, spinner, fade-in) run on one 30 fps timer that only
  runs while something moves; `animations: false` stops them (the Mac rule).
* Recorder controller = the Mac `Recorder`: phases idle / recording / finishing / message (flash 2.5 s), two-step
  cancel (arm 4 s, confirm only after 0.5 s), disk checks (`disk.can_start` before, `must_stop` every 20 s, worker
  `diskfull`), fresh base names that avoid cancelled ones, cancel = `Job.kill()` + delete `<base>*` now and after 2 s,
  end of call = reveal the summary in Explorer + "נשמר" / "לא נקלט דיבור" / "נשמר, אין מקום". Capture status lines
  become the card's notice: mic switched / lost / denied, "כרום סגור. ממשיך להקליט את המיקרופון" (recording starts
  without Chrome, as decided in 5.1), Chrome back. The worker's stderr goes to `<app dir>\logs\worker.log`.
* Tray icon (left click = "השיחות שלי") and the same menu on right click of the island. "שפר את טימי", "עצב את טימי"
  and "הרשאות והגדרה" are present but disabled until build step 4 registers them (`TimmyApp.step4`); "הפעל עם הכניסה
  למחשב" (Windows only) writes the Run key. Not enough space at start: a tray balloon (the folder choice is onboarding,
  step 4).

### 7.2 Windows

`ui/calls_window.py` (השיחות שלי), `ui/chat_window.py` (three chat modes), `ui/onboarding.py`, `ui/send_whatsapp.py`,
`ui/pdf_render.py`. All `setLayoutDirection(RightToLeft)`, Hebrew font fallback "Segoe UI" (has Hebrew) with
"Heebo"/"Assistant" bundled if the theme wants them. Theme colors from `config.json` applied through one generated
stylesheet, rebuilt on change so open windows update immediately (the Mac behavior from commit 12957d4).

### 7.3 Hiding from screen sharing

`winsys/affinity.py`: `user32.SetWindowDisplayAffinity(HWND(int(w.winId())), 0x11)` (WDA_EXCLUDEFROMCAPTURE, Windows
10 2004+). Applied by an application-wide event filter on `QEvent.Show` / `QEvent.WinIdChange` for **every top-level
window** (pill, card, all windows, QMenu popups, tooltips, dialogs), so nothing is forgotten. Setting
`hide_in_screen_share` (default true) toggles it live for all windows. Chrome/Meet "share entire screen" uses Windows
Graphics Capture / DXGI duplication, both honor the affinity; the window stays visible to the user. Verified in CI by
reading back `GetWindowDisplayAffinity` and by a desktop screenshot that must not contain a red test window (needs the
native `windows` QPA platform; GitHub Windows runners have an interactive desktop, the test skips if not).

## 8. PDF

The vendored `mac/pdf.py` (Claude writes the body HTML, brand.json, template.html, history/undo/reset, linked PDF
skills, fonts, images) runs unchanged in logic. The UI prints the produced HTML with `QWebEnginePage.printToPdf` (A4,
margins as on the Mac) in a hidden `QWebEngineView`, offscreen-capable for CI. Footer (contact line + page numbers)
and the fixed "נשלח על ידי טימי" line are added by the app, as on the Mac:
1. preferred: CSS `@page { @bottom-center { content: ... counter(page) " / " counter(pages) } }` page-margin boxes
   (Chromium 131+; Qt 6.12 WebEngine is newer), verified by a test that extracts the text of every page;
2. fallback: render once, count pages with `QPdfDocument` (QtPdf, also in Addons), stamp footers with `QPdfWriter`.
`skill_render.py` (used by PDF skills Timmy creates) gets the Windows Chrome/Edge paths in its candidate list
(`%ProgramFiles%\Google\Chrome\Application\chrome.exe`, `%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe`,
`%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe`).

As built in step 3 (`ui/pdf_render.py`, measured with Qt 6.12 offscreen on macOS):
* QtWebEngine **ignores CSS `@page` margins** (the QPageLayout margins win), so the first `@page { margin: ... }` of
  the HTML is parsed (mm / cm / in / pt / px, 1 to 4 values) and passed as the page layout; default 20/19/24/19 mm
  (pdf_template.html). Page-margin boxes **do work**: `@bottom-left` "עמוד X מתוך N" and `@bottom-right` = the brand
  contact (bottom margin raised to at least 14 mm), so option 1 is used and no stamping library is needed. The test
  extracts the text of every page with QtPdf and finds the right numbers on each.
* The page is loaded from a temp file (no 2 MB `setHtml` limit for embedded images) with
  LocalContentCanAccessRemoteUrls on, so Heebo loads from Google Fonts like on the Mac; one shared off-the-record
  profile; print after `document.fonts.ready` + 0.6 s, or after 6 s, or after 25 s at the latest.
* The Timmy line (`inject_timmy_footer`, Mac regexes, data: URIs stashed first) is added right before printing. If the
  last page holds only that line (checked with QtPdf after printing), it is printed again with the line tightened
  (no top gap, `break-before: avoid`), which brings it back onto the previous page; tested with content that fills a
  page exactly.
* Engine run = `python -m timmy.engine.pdf <md> --out <tmp.html>` in a Job (Cancel kills Claude and node), 210 s cap;
  no engine output = the Mac fallback HTML from the Markdown ("pdf.py לא רץ"). A PDF made by a linked skill is moved
  into place unchanged; stamping the Timmy line onto such a foreign PDF is part of the linked-skills work (step 4).
* Lifetime rule learned the hard way: a print / build keeps itself alive (module set) until it finished, and a page is
  deleted before the shared profile; dropping a build early or deleting the profile by hand crashes QtWebEngine.

## 9. Claude Code, secrets, Greenolink

* **Claude Code** (`engine/claude.py`, port of `claudeCandidatePaths` / `findClaudePath`): config `claude_path`, then
  `%USERPROFILE%\.local\bin\claude.exe` (native installer), `%APPDATA%\npm\claude.cmd` (npm global; run through
  `cmd /c` with arguments that never contain user text, prompts always go through stdin), volta/bun shims, `shutil.which`,
  and the Claude desktop app's bundled copy (`%APPDATA%\Claude\claude-code\<ver>\...\claude.exe`, newest first, searched
  every time like the Mac). `TIMMY_CLAUDE_SEARCH` (`;`-separated on Windows) replaces the list in tests. Logged in:
  `claude auth status --json`; login: `claude auth login --claudeai` in the background (opens the browser), never a
  terminal. Claude Code on Windows needs Git for Windows (bash); onboarding checks `git --version` /
  `CLAUDE_CODE_GIT_BASH_PATH` and offers the download. Every child: `CREATE_NO_WINDOW`, `cwd=tempfile.gettempdir()`
  (the Mac code uses `/tmp`), member of the Timmy Job Object so a timeout kills node and its children.
* **Secrets** (`integrations/secrets.py`): keyring (Windows Credential Manager), service names as on the Mac
  ("Timmy Greenolink"). Credential Manager blobs are limited to 2560 bytes, so values longer than 1000 characters are
  stored as `key#0..n` chunks plus a count; a test round-trips a 6 kB token. Tests use an in-memory backend
  (`TIMMY_KEYCHAIN_SERVICE` / keyring `fail.Keyring` swap), never the runner's vault for real tokens.
* **Greenolink** (`integrations/greenolink.py`, port of Recorder.swift "Greenolink ישירות"): metadata discovery,
  dynamic client registration with `http://127.0.0.1:<port>/callback`, PKCE S256, browser via `os.startfile`,
  local `http.server` waits for the code, token + refresh, revoke on "נתק". MCP Streamable HTTP: `initialize` ->
  `notifications/initialized` -> `tools/call` (`search_contacts`, `send_message`, `request_file_upload` + upload +
  `send_file`), SSE or JSON responses, session id header, re-init on 404. `TIMMY_GREENOLINK_BASE` points tests at the
  mock. The mock (`qa-connect/mock_greenolink.py`) is git-ignored, so build step 4 copies it to
  `windows/tests/mock_greenolink.py` (its contacts are fictional placeholder numbers).
  Rule enforced by tests: `send_message`/`send_file` are only reachable from the confirm-click handler.

## 10. Shared vs copied code

`mac/live.py` is a top-level script (it starts threads and loads MLX at import), so it cannot be imported; `mac/pdf.py`
and `mac/skill_render.py` import fine but hard-code `/tmp`, `killpg` and macOS paths. Refactoring them into a shared
package would change the Mac install (`install.sh` copies single files into Application Support), so **mac/ stays
byte-for-byte as is** and Windows gets the code through a generator:

* `tools/sync_mac.py` writes `timmy/engine/_mac/`:
  * `live_shared.py`: selected top-level nodes extracted from `mac/live.py` with `ast` (the prompt strings `PROMPT`,
    `SUMMARY_PROMPT`, `RESTYLE_PROMPT`, the constants `FILLERS`/`STOCK`/`NEVER`/`BIDI`/`FR`/`TH`, and the pure functions
    `_norm`, `dedupe`, `clean`, `final_ok`, `custom_block`, `keep_images`, `text_overlap`, `ts`, `speech_frames`,
    `_env`), rewritten to take their globals as parameters where needed.
  * `pdf.py`, `skill_render.py`, `pdf_template.html`, `pdf_sample.md`: copied verbatim, then a declared list of exact
    string patches (for example `cwd="/tmp"` -> `cwd=TMP`, the `claude_path()` candidates, the PATH join with
    `os.pathsep`, `start_new_session`/`killpg` -> Job Object, Chrome paths). Each patch must apply exactly once or the
    tool fails.
* `tests/test_mac_sync.py` re-runs the generator in memory and asserts the committed `_mac/` is identical, so any Mac
  change that is not re-synced fails Windows CI with a clear message ("run python tools/sync_mac.py").
* Windows-only code (worker main loop, ASR, capture, UI, chat, Greenolink) is new Python, ported from Swift where the
  logic lives in Recorder.swift, with tests that pin the same behavior (for example the isSimpleLooks table).
* The parts of mac/live.py that had to be ported by hand (reader, `tr`, `Chan`, `is_echo`, key points, restyle, the
  main loop and the end of the call) carry the sha256 of their Mac source in `worker.MAC_PORTED`; `test_worker.py`
  fails when the Mac code changes, and a parity test runs the Mac `Chan` itself against the port.

## 11. Installer (`windows/install.ps1`, build step 4)

PowerShell 5.1 compatible, no admin, run by Claude Code per INSTALL.md (a Windows section is added there in step 4,
with the same 3 questions). Steps, each printing `==> ...` lines like install.sh:
1. Check x64, build >= 19041 (else the update message), free space (4 GB, 2 GB if the model is cached), internet
   (`huggingface.co`).
2. uv to `%LOCALAPPDATA%\Timmy\uv` if missing (official standalone zip, no admin), `uv venv -p 3.12`,
   `uv pip install "<repo>\windows[audio,asr,ui,system]"` (copy of the code into `%LOCALAPPDATA%\Timmy\app`).
3. ffmpeg from `imageio-ffmpeg`; model download with resume.
4. `config.json`: same keys and merge rules as install.sh; `--output-dir` explicit wins, an existing `output_dir`
   is never overwritten, default `Documents\טימי - הקלטות`.
5. Start Menu + Desktop shortcuts "טימי" (`WScript.Shell` .lnk to `pythonw.exe -m timmy`, the Timmy icon),
   `HKCU\...\Run\Timmy`, then launch Timmy, which opens onboarding.
6. `uninstall.ps1`: removes app, venv, model, shortcuts, Run key; recordings stay.
Optional later: PyInstaller one-folder build + Inno Setup (`windows-beta` has `timmy.spec` and `Timmy.iss`).

## 12. Tests and CI

Workflow `.github/workflows/windows.yml` (today parked at `windows/ci/windows.yml`, see the top of this file), runner `windows-latest`, push to `windows` + `workflow_dispatch`.
Every job uploads `windows/out/` (junit, logs, PNG screenshots) as an artifact. Agents: `git push`, `gh run watch`,
`gh run view --log-failed`, `gh run download <id> -n windows-test-output` and Read the PNGs.

| Job | When | Installs | Covers |
|---|---|---|---|
| `test` | every push | `.[dev]` (+ the extras of the steps done so far) | ruff, compileall, pytest |
| `asr-bench` | dispatch with `asr_bench=true` | `.[asr]` + real model | speed numbers for 6.2 |
| `asr-real` (step 2) | every push + dispatch (`asr_real`) | `.[dev,asr,system]` + real model (cached with actions/cache) | `pytest -m slow`: fixture transcribed with the real model, merge + split, the whole worker in real time in the cpu profile (`out/worker-real-model.json` has timings) |
| `installer` (step 4) | every push | nothing: Windows PowerShell 5.1 runs install.ps1, which gets uv + Python 3.12 itself | dry run, install into `%RUNNER_TEMP%\Timmy App` (tiny model, no shortcuts / login item / launch), config + model + ffmpeg checks, re-run keeps the folder, uninstall |

Test areas (pytest markers `windows_only`, `audio`, `ui`, `asr`, `slow`):

| Area | How | Step |
|---|---|---|
| Protocol framing, Mac drift guard | pure Python | done |
| config.json rules, paths, version gates, repo rules (no long dash, no real emails) | pure Python | done |
| Windows APIs exist (Mmdevapi export, display affinity, known folders) | ctypes on the runner | done |
| Resampler (rates 8k to 96k, channels 1 to 8, int16/24/32/float), padder (gaps, switches, no drops) | numpy, `test_resample.py`, `test_padder.py` | 1, done |
| WASAPI call machinery against fake COM objects (layouts, GUIDs, QI, activation handler callback, format negotiation, GetBuffer/ReleaseBuffer, device enumeration, friendly names), mic follows the default device, Chrome watcher attach / restart / retry / unsupported | any OS, `test_wasapi.py`, `test_sources.py` | 1, done |
| WASAPI for real: COM + MMDevice enumerator, process-loopback activation on our own PID (IAudioClient or a known "no audio" HRESULT = skip), MicSource state, capture process in real mode keeps both channels on the timeline, a child playing a tone is captured (only with a speaker endpoint) | runner, `test_wasapi_windows.py` | 1, **not run yet** |
| Capture process end-to-end with fake sources (`--fake-chrome` 48 kHz stereo tone, `--fake-mic` fixture with a 0.6 s unplug gap), stop by "stop" / EOF / reader gone, `--mic-test`, `--list` | subprocess, `test_capture_e2e.py` | 1, done |
| Mac sync drift test, patch-miss failure, generated live_shared behaves like mac/live.py | `test_mac_sync.py` | 1, done |
| Worker parity: mac/live.py's own `Chan` / `is_echo` (extracted with ast) and the port get the same audio and must emit identical events, lines and .txt; `segments_info` == the Mac `tr()` on a table of segment lists; hand-ported Mac parts fingerprinted (`worker.MAC_PORTED`) | any OS, `test_worker.py`, `test_asr.py` | 2, done |
| Worker end-to-end: fixtures framed into `python -m timmy.engine.worker` (fake ASR backend and a fake `claude` via `TIMMY_CLAUDE_SEARCH`, a `.cmd` shim on Windows in a folder with a space): events order, transcript, summary md, renamed files, mp3, key points, restyle, meetings copy, person link, echo, no speech, no Claude, same-name call, UTF-8 under cp1252, `--restyle`, cancel = Job kill takes claude down too; cpu profile merges without losing lines | subprocess, `test_worker.py` | 2, done |
| The same worker with faster-whisper: tiny model (plumbing, `test` job) and the real ivrit.ai model (cpu profile, real time) | `test` / `asr-real` | 2, done (real model: proven on macOS CPU only) |
| Disk checks, file-name sanitizing (reserved names, MAX_PATH), Job Object kill of a child tree, timeouts, Claude finder order, `.cmd` command line, Chrome tab titles | `test_disk.py`, `test_files.py`, `test_jobs.py`, `test_claude.py`, `test_chrome_tabs.py` | 2, done (Windows-only parts not run yet) |
| UI smoke offscreen (`QT_QPA_PLATFORM=offscreen`): every island state (pill, hover, 4 messages, card, thinking, cancel armed + mic notice, saving, hidden transcript, light theme), hit targets and clicks, sizes / flags / position, calls window (dark, light, search, rename, PDF working / done, empty), the menu while recording, PNG per screen in `out/screens`; `python -m timmy` stays up and a second launch hands over | `test_ui.py` | 3, done (chat / onboarding screens: 4) |
| Theme rules vs the Mac (contrast, visible(), light background ink, on-accent, apply keeps keys) | `test_theme.py` | 3, done |
| Calls model: parse, list, search, dates, rename (all files, images folder, meetings copy, title-only, collisions, rollback, MAX_PATH), Markdown blocks | `test_calls.py` | 3, done |
| Recorder controller with the real capture process (fake WAVs) and worker (fake ASR + fake claude): full call saved and revealed, two-step cancel kills both processes and deletes every file, arm expiry, no space at start, disk full mid-call, status notices | `test_recorder.py` | 3, done |
| Screen-share hiding: ShareHider sets the affinity on every top-level window (island, windows, menus) and re-applies on toggle (any OS, fake setter); real affinity read back + desktop screenshot with the native QPA | `test_ui.py`, `test_winsys_ui.py` | 3, done (real part: **not run yet**) |
| HTML -> PDF via QtWebEngine offscreen: page count, "עמוד X מתוך N" + contact on every page, Timmy line once, Hebrew text extractable, margins from @page, lone-line re-print, full engine flow (local), fallback, cancel | `test_pdf_render.py` | 3, done |
| Launch-at-login registry write/read/delete (test value name), single instance | `test_winsys_ui.py` | 3, done (registry: **not run yet**) |
| Greenolink OAuth + MCP against `tests/mock_greenolink.py` (sse/json, multi, deny, expired token -> refresh, killed session -> re-init, fail_send, upload host check, cancel, foreign state), never the real server | subprocess, `test_greenolink.py` | 4, done |
| Send flow: draft / revise / undo, recipient resolved while drafting, confirm shows the exact contact, pick list, PDF attach, number recipient, failures; nothing in the mock's WOULD_SEND log before `confirm_send`; the Claude Code MCP fallback (search permission only, then the send prompt); AST check that sending is reachable only from the "שלח עכשיו" click | `test_whatsapp.py` | 4, done |
| Chat logic tables (isSimpleLooks, parseAppearance, shade, colorWord, follow-ups, memory add/forget, history format), improve / design flows with a fake Claude (instructions, memory, link / unlink skill + honest "not found", pdf_instruction + logo hand-off, share hiding, restyle of the last call through the worker, writing-voice skill create / ask / replace, design mode looks-only), PDF design chat (reset, logo copy, history file, queue, adopt a skill without a script), PDF design skill from a generated sample PDF (QtPdf pages, measured colors, stream-json with images, template validated by pdf.py) | `test_chat.py` | 4, done |
| Secrets chunking round trip (in-memory backend enforcing the 2560-byte limit); real Credential Manager with a throwaway service name | `test_greenolink.py` | 4, done (real vault: **not run yet**) |
| Onboarding checks (Claude detect: logged in / not / old version / health "auth" / missing; login in the background incl. pasted code and "outdated"; mic test through the capture process with a fake mic; folder), every onboarding step and every send-dialog stage and both chats as screenshots, app wiring (menu items enabled, WhatsApp button, "no space" -> folder step, first launch opens onboarding, --background does not) | `test_step4_ui.py` | 4, done |
| install.ps1 / uninstall.ps1 rules (BOM, CRLF, PowerShell 5.1 syntax, no empty native arguments), install_helper (config merge rules = install.sh, icon, startup, cleanup, tiny model); `-DryRun` on Windows; CI job `installer`: real install into a temp folder with the tiny model, config checks, re-run keeps the user's folder, uninstall leaves the recordings | `test_installer.py`, job `installer` | 4, done (Windows parts: **not run yet**) |

What CI cannot prove (manual QA on a real PC before release): real Chrome audio from a Meet call, a Bluetooth headset
switch mid-call, Windows 10 2004 itself, SmartScreen / Defender Controlled Folder Access behavior.

## 13. Build steps (sequential, each green in CI on its own)

1. **Audio capture.** `timmy/audio/*` (wasapi declarations, process loopback for the chrome.exe tree, default mic
   with device follow, soxr resampler, wall-clock padder, Chrome PID discovery) and the capture process
   `python -m timmy.audio.capture` writing protocol frames to stdout and JSON status to stderr (`--fake-wav`,
   `--seconds`, `--list`, `--mic-test` for tests and onboarding). Plus `tools/sync_mac.py` with the drift test.
   CI: resampler/padder unit tests, COM init + enumerator + loopback activation smoke on the runner, capture process
   end-to-end with a fake source. Exit gate: decide comtypes vs the C# helper fallback (decided: plain ctypes, 5.1).
2. **Worker and engine.** `timmy/engine/worker.py` (mac/live.py port on faster-whisper, same CLI, events and file
   outputs, including `--restyle`), `asr.py` (device pick, CPU/fast profiles, merge + split by segment timestamps),
   `claude.py` (find + run without console + Job Object), `chrome_tabs.py`, `winsys/disk.py`, `winsys/jobs.py`,
   file-name sanitizing, ffmpeg mp3 via imageio-ffmpeg. CI: worker end-to-end with the tiny model and a fake claude,
   `asr-real` job with the real model on the fixture, Job Object and disk tests.
3. **Core UI.** PySide6 app: single instance, tray + menu, pill and recording card wired to capture + worker
   (record, stop, two-step cancel, hide transcript, mic switch indicator, disk-full stop), screen-share hiding on every
   window, themes (dark + light, contrast rules), launch at login, "השיחות שלי" (list, search, summary, rename title +
   files, transcript, open folder), client PDF through the vendored pdf.py + QtWebEngine printToPdf with footer and
   page numbers. CI: offscreen screenshots of each state as artifacts, affinity test, PDF test, registry test.
4. **Integrations, chats, onboarding, installer** (done, 13.4). Greenolink (OAuth PKCE loopback, MCP, draft -> confirm -> send,
   keyring with chunking), "שפר את טימי" / "עצב את טימי" / "עיצוב ה-PDF" chats with conversation context, memory.json
   and linked Claude skills (and skill creation, images in the PDF), onboarding (mic privacy, Chrome, Claude Code +
   Git for Windows + login, folder + free space, launch at login), `install.ps1` / `uninstall.ps1`, INSTALL.md and
   README Windows sections. CI: Greenolink against the mock, chat tables, onboarding screenshots, installer dry run.

### 13.1 Build step 1 result (2026-11-20)

Done: `timmy/audio/` (wasapi, loopback, mic, resample, padder, procs, fake, capture), `tools/sync_mac.py` +
generated `timmy/engine/_mac/` (live_shared.py, pdf.py, skill_render.py, pdf_template.html, pdf_sample.md), tests
`test_resample.py`, `test_padder.py`, `test_procs.py`, `test_wasapi.py`, `test_sources.py`, `test_capture_e2e.py`,
`test_mac_sync.py`, `test_wasapi_windows.py`; the workflow (still parked at `windows/ci/windows.yml`) now installs
`.[dev,audio]`, tries to start the Windows Audio service, runs `capture --list`, the sync check and a 2 s real-mode
capture smoke before pytest. `comtypes` was removed from the `audio` extra.

Capture process interface for the UI (step 3): spawn `python -m timmy.audio.capture` with stdin/stdout/stderr pipes
and `CREATE_NO_WINDOW`, pass its stdout as the worker's stdin, read JSON lines from its stderr (`ready`, `chrome`,
`mic`, `levels`), write `stop\n` to stop (EOF also stops). Exit code 0 in every normal case, 2 on bad arguments.

Proven on macOS (any OS): every pure part, the full ctypes call layer against fake COM objects, the mic
device-switch logic and the Chrome watcher logic, and the capture process end to end with fake sources (timing
within 50 ms, gap = exact silence, no late samples). **Not proven yet**: anything that needs Windows itself
(Toolhelp32, CoCreateInstance, the real activation callback, real capture formats); these are the `windows_only`
tests and the CI smoke step, which run as soon as the workflow is installed. Manual QA on a real PC: Chrome audio
from a Meet call, a Bluetooth headset switch mid-call.

Notes for step 2: `timmy.engine._mac.live_shared` exposes the Mac prompts, filters, echo helpers and `configure(cfg)`;
`echo_score` uses the module's `CH0_HIST` (one call per worker process, like the Mac). pdf.py still needs the
Windows patches listed in `tools/sync_mac.py` for step 3 (fcntl lock, process-tree kill, interpreter list / PATH).

### 13.2 Build step 2 result (2026-11-21)

Done: `timmy/engine/worker.py` (mac/live.py port: same CLI, events, files, `--restyle`), `asr.py` (model lookup and
download, CUDA probe, int8 CPU with physical-core threads, cpu / fast profiles, merge + split with per-piece
compression ratio, `FakeASR` for tests), `claude.py` (Windows finder order, `.cmd` through `cmd.exe /d /s /c` with
fixed flags only, prompts on stdin, cwd = temp), `chrome_tabs.py` (EnumWindows), `files.py` (names, MAX_PATH,
no overwrite of a same-name call, `os.replace`, mp3 through the imageio-ffmpeg binary), `winsys/jobs.py` (Job
Objects: CREATE_SUSPENDED -> AssignProcessToJobObject -> NtResumeProcess, KILL_ON_JOB_CLOSE, TerminateJobObject;
POSIX: session + killpg + psutil descendants; BELOW_NORMAL priority), `winsys/disk.py`. Fixture `he_reply.wav`
(second synthetic sentence for the mic side). Workflow (still parked): `test` installs `.[dev,audio,asr,system]` and
the cached tiny model; new job `asr-real` with the cached real model.

Worker interface for the UI (step 3): spawn `python -m timmy.engine.worker "<recordings>\<YYYY-MM-DD HH-MM-SS> כרום"`
inside the call's Job (with the capture process), stdin = the capture process's stdout, read JSON lines from stdout
(`protocol.parse_event`), stderr to a log file. Stop = stop the capture (worker sees EOF, saves, `done` has the
final `.md`). Cancel = `Job.kill()` (takes the worker's claude / ffmpeg children too, proven by a test) + delete
`<base>*`. `--restyle <md>` prints one `restyled` line. The worker never checks free space itself (as on the Mac):
the UI calls `winsys.disk.can_start` before a call and `must_stop` every 20 s, and handles `diskfull` from a failed
write.

Proven on macOS: the port emits exactly the events and lines of mac/live.py's own code on the same audio (fast
profile); the cpu profile produces the same lines at the same times when it falls behind; the full worker with the
real ivrit.ai CT2 model on CPU (M2 Max, 8 threads, int8, cpu profile) transcribes both fixture sentences word for
word in real time, each line 2 to 4 s after the utterance ended, model load 4 to 18 s. **Not proven yet** (Windows
only, never run): Job Objects (suspended start, nesting inside the UI's job, TerminateJobObject), `cmd.exe` running
the `.cmd` shim with a space in the path, EnumWindows titles, BELOW_NORMAL, CRLF / code-page behaviour of real
Windows pipes, ffmpeg from the Windows wheel, CTranslate2 speed on the runner CPU (the `asr-real` job will tell).

### 13.3 Build step 3 result (2026-11-21)

Done: `timmy/ui/` (app, theme, paint, island, recorder, calls, markdown, calls_window, pdf_render, shell, assets),
`timmy/winsys/affinity.py`, `startup.py`, `instance.py`, `timmy/engine/pdf.py` + `pdf_win.py`, the pdf.py patches in
`tools/sync_mac.py` (app folder, Claude finder, run() in a Job with tree kill, flock shim, skill interpreters and PATH;
the vendored copy has no `fcntl`, `killpg` or `/usr/bin` left, checked by `test_mac_sync.py`), `claude.command()` now
keeps an empty argument (`--tools ""` from pdf.py) on the `.cmd` path. `python -m timmy` runs the app (`--report`
replaces the old default output; CI updated). Tests: `test_theme.py`, `test_calls.py`, `test_pdf_render.py`,
`test_recorder.py`, `test_ui.py`, `test_winsys_ui.py` (+ `affinity_probe.py`). Workflow (still parked): the `test` job
installs `.[dev,audio,asr,system,ui]`, sets `QT_QPA_PLATFORM=offscreen`, and uploads `out/screens/*.png`.

Proven on macOS (offscreen Qt 6.12): every island state renders as designed (screenshots reviewed), the controller
runs a whole call through the real capture and worker processes, saves, cancels (both processes dead, no file left),
and stops on a full disk; the calls window lists, searches, renames (with rollback) and makes a PDF through the real
engine; QtWebEngine prints A4 with real Hebrew text, the page footer on every page and the Timmy line once.
**Not proven yet** (Windows only, never run): `SetWindowDisplayAffinity` on real windows and whether a desktop
screenshot really leaves the window out, the Run key, pythonw + pipes for the children, Explorer `/select`, the tray
icon (Windows 11 hides new icons under ^), the native QPA look (Segoe UI, DPI scaling, layered translucent window,
click-through of the transparent margins), QtWebEngine on the runner (sandbox / GPU flags) and the named-pipe single
instance. Manual QA on a real PC: the pill over a maximized Chrome (it covers the middle of the tab strip), Meet "share
entire screen" without Timmy, a second monitor.

Notes for step 4 (all done in 13.4 except the Timmy line on PDFs made by linked skills): register the chat / onboarding
handlers in `TimmyApp.step4`; WhatsApp into `CallsWindow.extra_actions`; `Recorder.need_folder` opens the folder step.

### 13.4 Build step 4 result (2026-11-21)

Done: `timmy/integrations/` (secrets with chunking and an in-memory test backend; greenolink: the Recorder.swift client
in Python, loopback `http.server` on 127.0.0.1, PKCE S256, client registration reused per redirect port, 302 probe and
re-register on 4xx, token refresh 2 min before expiry or on 401 with one refresh at a time, MCP session re-init on 404,
SSE and JSON answers, upload only to Greenolink's own host, revoke on "נתק"; whatsapp: SendSession with the Mac kinds,
prompts, draft cleanup, revisions, resolve-while-drafting, confirm / pick, direct send or the Claude Code MCP
fallback), `timmy/chat/` (looks, memory, history, improve, pdf_design, skills incl. creating a writing-voice skill and a
PDF design skill from a sample PDF: pages rendered with QtPdf, colors measured with numpy, fonts from uncompressed
`/BaseFont` entries, Claude with page images over stream-json, the template checked by `pdf.py --validate-template`),
`timmy/onboard.py` + `ui/onboarding.py`, `ui/chat_window.py`, `ui/send_whatsapp.py`, `ui/bridge.py`,
`timmy/engine/procrun.py`, `timmy/install_helper.py`, `install.ps1`, `uninstall.ps1`, `tests/mock_greenolink.py` (copy of
the git-ignored qa-connect mock, placeholder contacts only), INSTALL.md / README Windows sections, the `installer` CI job.
App wiring: `TimmyApp.step4` registers the chats and the onboarding (menu items enabled), "שלח בוואטסאפ" sits first in
the calls window's actions, `Recorder.need_folder` opens the folder step, the first launch without `onboarding_done`
opens the onboarding (not with `--background`). New config keys (Windows only): `onboarding_step`, `onboarding_done`.

Windows differences from the Mac, by design:
* the prompts say "אפליקציה למחשב (ווינדוס)" and "הבועה השחורה" instead of the notch; the contrast note too;
* a replaced Timmy-made skill goes to `<app dir>\trash\skills\` (no Recycle Bin API without extra packages);
* skill names are slugged without transliteration (a Hebrew-only name falls back to e.g. `my-writing-voice`);
* Claude Code login: `claude auth login --claudeai` opens the browser itself; Timmy shows the first https URL it prints
  as a fallback link and accepts a pasted code (no BROWSER helper script as on the Mac);
* microphone: Windows never asks; onboarding reads the privacy switch from the registry and runs a 4 s test capture.

Not ported yet (open): **images in a call's PDF** (the Mac "הוסף תמונות ל-PDF", drag and drop, video frames from Drive,
Claude choosing the section and caption) and the chat actions add_images / image_edit / video_frames. The Windows
prompt tells Claude that this is not available yet, and the app answers so if Claude still returns such an action.
Stamping the "נשלח על ידי טימי" line onto PDFs produced by a linked skill's own script is also still open (step 3 note).

Proven on macOS (Qt 6.12 offscreen, 387 tests): the full OAuth + MCP flow against the mock (no WOULD_SEND without the
confirm click, checked by behaviour and by an AST rule), every chat flow with a fake Claude, both skill creations, the
installer helper rules, and screenshots of every new screen reviewed (chats dark and light, PDF design with a real
preview, all send stages, all onboarding steps). **Not proven yet** (Windows only, never run): the real Credential
Manager (2560-byte limit, chunking), the loopback callback behind Windows Defender Firewall (127.0.0.1 only, so no
prompt is expected), `os.startfile` for `ms-settings:` and the browser, `claude.cmd` with `--allowedTools` patterns,
`claude auth login --claudeai` on Windows, the registry consent read, install.ps1 itself on PowerShell 5.1 (uv download,
`uv venv`, `uv pip install` from the repo path, `.lnk` + `.ico`), uninstall.ps1. Manual QA on a real PC: the Greenolink
login with a real phone (never sending to a real contact without the owner), Claude Code login, Git for Windows prompt.

## 14. Risks and open questions

| Risk | Impact | Mitigation |
|---|---|---|
| ctypes COM callback (agile handler) for ActivateAudioInterfaceAsync misbehaves on real Windows | no Chrome audio | MTA-only capture process; fake-COM tests pin the call layer; C# helper fallback from windows-beta |
| CPU too slow on old laptops | lines appear late | cpu profile (6.3), BELOW_NORMAL priority, the transcript is still complete at the end |
| Claude Code needs Git for Windows | no summaries | onboarding check + download link; transcript works without Claude |
| Credential Manager 2560-byte limit | Greenolink tokens lost | chunked storage, tested |
| Defender Controlled Folder Access blocks writes to Documents | recordings fail | detect the PermissionError, onboarding explains (text exists in windows-beta README) |
| SmartScreen / AV flags pythonw from %LOCALAPPDATA% | scary prompts | per-user venv is common; PyInstaller + signing later if needed |
| Windows 10 reached end of support (Oct 2025) | fewer Win10 users | still supported from 19041, Windows 11 is the main target |
| Hebrew RTL rendering differences in Qt | layout bugs | screenshots of every screen in CI, reviewed by the QA agents. Found in step 3: full-width rich-text tables are laid out left to right whatever the direction (markdown.py emits the cells reversed) |
| The pill at the top center covers part of a maximized window's title / tab strip (no notch to hide in) | a click meant for Chrome lands on Timmy | small pill (180 x 30), transparent margins click through; manual QA; a "move down" option if testers complain |
