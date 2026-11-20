# Timmy for Windows: design

Status: architecture decided, skeleton pushed, implementation split into 4 build steps (section 13).
Branch: `windows` (from `main`). Everything Windows lives under `windows/` plus the CI workflow.

**CI is not active yet.** The workflow is committed as `windows/ci/windows.yml` because the GitHub token on the dev
machine lacks the `workflow` scope (GitHub rejects pushes that add `.github/workflows/*` without it). One-time fix by
the repo owner: `gh auth refresh -h github.com -s workflow`, then move the file to `.github/workflows/windows.yml`
(exact commands at the top of the file). Until then the build steps can only be verified locally (non-Windows tests).
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
  install.ps1              per-user installer (build step 4)
  timmy/
    __main__.py            entry (python -m timmy); today prints an environment report
    paths.py               %LOCALAPPDATA%\Timmy (TIMMY_APP_DIR override), Documents\טימי - הקלטות     [done]
    config.py              config.json load/save (utf-8-sig, atomic, keep unknown keys, never overwrite output_dir) [done]
    engine/
      protocol.py          frame codec + JSON-lines events, byte-identical to mac/live.py           [done]
      worker.py            port of mac/live.py (same CLI: <base> | --restyle <md>)                  [step 2]
      asr.py               faster-whisper backend, device pick, adaptive segmentation                [step 2]
      claude.py            find claude.exe/claude.cmd, run without console, Job Object kill          [step 2]
      chrome_tabs.py       active tab titles of Chrome windows (EnumWindows), replaces osascript     [step 2]
      pdf.py               thin wrapper: runs the vendored mac/pdf.py with the Windows patches       [step 3]
      _mac/                files generated by tools/sync_mac.py from mac/ (never edited by hand)    [steps 1-3]
    audio/
      wasapi.py            comtypes interface declarations (IMMDeviceEnumerator, IAudioClient, ...) [step 1]
      loopback.py          process loopback client for one PID tree                                 [step 1]
      mic.py               default-input capture, default-device follow, invalidation recovery      [step 1]
      resample.py          soxr streaming resampler to 16 kHz mono float32                          [step 1]
      padder.py            wall-clock silence padding per channel                                   [step 1]
      procs.py             Chrome browser root PIDs (Toolhelp32 snapshot, psutil fallback)          [step 1]
      capture.py           the capture process (__main__), status JSON on stderr                    [step 1]
    winsys/
      winver.py            version gates (19041)                                                    [done]
      affinity.py          SetWindowDisplayAffinity WDA_EXCLUDEFROMCAPTURE                           [step 3]
      startup.py           HKCU Run key "Timmy"                                                      [step 3]
      disk.py              free-space checks (500 MB start / 200 MB keep, same as Mac)              [step 2]
      jobs.py              Job Object helper (the Windows twin of killpg / start_new_session)       [step 2]
      instance.py          single instance via QLocalServer; second launch opens "השיחות שלי"       [step 3]
    ui/                    PySide6 Widgets (see 7)                                                    [steps 3-4]
    chat/                  looks fast path, memory.json, chat history, improve/design calls, skills [step 4]
    integrations/          secrets (keyring, chunked), greenolink (OAuth PKCE + MCP)                [step 4]
  tests/                   pytest; fixtures/he_short.wav (synthetic TTS voice, 6.3 s, 16 kHz mono)
  tools/
    bench_asr.py           model speed on the current machine (CI job asr-bench)                    [done]
    sync_mac.py            regenerate timmy/engine/_mac/ from mac/ (+ drift test)                   [step 1]
```

UI toolkit: **PySide6 Widgets**, not QML. Reasons: `QWidget.grab()` gives deterministic offscreen screenshots for CI,
custom painting of the pill (eyes, mouth wave) is simple `QPainter` code ported from the SwiftUI shapes, and RTL is
`app.setLayoutDirection(Qt.RightToLeft)`. Full `PySide6` (not `-Essentials`) because QtWebEngine is in the Addons.

## 4. Feature parity map

| Feature (Mac) | Mac implementation | Windows implementation | Risk / note |
|---|---|---|---|
| One-click record, Chrome audio + mic only, separate channels | ScreenCaptureKit app filter + AVAudioEngine, frames into live.py | WASAPI process loopback (chrome.exe tree) + WASAPI mic in `timmy.audio.capture`, same frames | High: process loopback via comtypes is the main technical risk (5.1). Fallback: harvest the C# helper from windows-beta |
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

Implementation in Python (`timmy/audio/wasapi.py` + `loopback.py`):

* `comtypes` with `sys.coinit_flags = 0` (MTA) set before the first `import comtypes`, in the capture process only.
  Interfaces declared with their exact vtable order (`IAudioClient` 1CB9AD4C-..., `IAudioCaptureClient` C8ADBD64-...,
  `IActivateAudioInterfaceAsyncOperation` 72A22D78-..., `IActivateAudioInterfaceCompletionHandler` 41D949AB-...).
* The completion handler is a `comtypes.COMObject` that also implements `IAgileObject`
  (94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90); without an agile handler activation fails with `E_ILLEGAL_METHOD_CALL`.
  It sets a Win32 event; the caller waits up to 5 s.
* `ActivateAudioInterfaceAsync` is called through `ctypes.WinDLL("Mmdevapi")` with the PROPVARIANT built by hand
  (x64 layout: `vt` at 0, `cbSize` at 8, `pBlobData` at 16, total 24 bytes).
* Format: do not rely on `GetMixFormat` (not implemented for the process-loopback client in practice; the sample also
  sets its own). Try in order with AUTOCONVERTPCM: float32 16 kHz mono (no resampling needed), PCM16 48 kHz stereo,
  PCM16 44.1 kHz stereo (the sample's format). Whatever succeeds is resampled by `resample.py` when needed.
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
* Fallback if comtypes proves unreliable: build the C# helper from `windows-beta` (`dotnet publish` single-file,
  about 70 MB) in CI and ship it; the frame protocol is identical, so nothing else changes. Decision point: end of
  build step 1.

### 5.2 Microphone

Refinement of the plan: the mic also uses **WASAPI shared mode through the same comtypes layer**, not sounddevice.
Reason: PortAudio keeps the device list from `Pa_Initialize`, so following a new default device (headset plugged in or
disconnected) needs a full library re-init; with WASAPI we just reopen the new default endpoint.
`IMMDeviceEnumerator::GetDefaultAudioEndpoint(eCapture, eConsole)`; same format negotiation as 5.1; poll the
default endpoint id every 500 ms; on change or `AUDCLNT_E_DEVICE_INVALIDATED` (0x88890004) close, reopen, and let the
padder fill the gap. No device at all: channel 1 is padded silence and the card shows "בלי מיקרופון".
`sounddevice` stays as a dependency only for the onboarding level meter fallback and dev tools.
Mic privacy: if `HKCU\Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone\
NonPackaged` is `Deny`, or the test capture is all zeros, onboarding opens `ms-settings:privacy-microphone`.

### 5.3 Resampling and padding

* `resample.py`: interleaved int16/float32, any channel count -> mono average -> `soxr.ResampleStream(in_rate, 16000,
  1, dtype="float32", quality="HQ")`; identity when the device already gives 16 kHz mono float32.
* `padder.py`: each channel keeps `sent_samples`; every 100 ms, `expected = (now - t0) * 16000`; if `expected -
  sent > 0.2 s` insert zeros up to `expected`. Never drops real audio (small clock drift is tolerated, same as the
  Mac). This keeps `[hh:mm:ss]` timestamps and the echo detector (which compares the two channels by position) right
  across device switches and Chrome restarts.
* Frames of 100 ms (1600 samples) per channel are written with `protocol.encode_frame` to stdout.

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
| `asr-real` (step 2) | dispatch / nightly | `.[asr]` + real model (cached with actions/cache) | fixture transcribed with the real model, keywords present |

Test areas (pytest markers `windows_only`, `audio`, `ui`, `asr`, `slow`):

| Area | How | Step |
|---|---|---|
| Protocol framing, Mac drift guard | pure Python | done |
| config.json rules, paths, version gates, repo rules (no long dash, no real emails) | pure Python | done |
| Windows APIs exist (Mmdevapi export, display affinity, known folders) | ctypes on the runner | done |
| Resampler (rates 8k to 96k, channels 1 to 8, int16/float), padder (gaps, switches, no drops) | numpy | 1 |
| WASAPI: comtypes interfaces import, MMDevice enumerator works, process-loopback activation on our own PID returns an IAudioClient or a known "no audio service" HRESULT (skip), mic open skips when no endpoint | runner | 1 |
| Capture process end-to-end with a fake source (`--fake-wav`) -> frames -> decoder | subprocess | 1 |
| Mac sync drift test | `tools/sync_mac.py --check` | 1 |
| Worker end-to-end: fixture WAV framed into `python -m timmy.engine.worker` with the tiny model and a fake `claude` (`TIMMY_CLAUDE_SEARCH`), asserts events order, transcript, summary md, renamed files, mp3 | subprocess | 2 |
| Real model transcription of the fixture (slow, cached) | `asr-real` job | 2 |
| Disk checks, file-name sanitizing (reserved names, long paths), Job Object kill of a child tree | runner | 2 |
| UI smoke offscreen (`QT_QPA_PLATFORM=offscreen`): pill, card with fake events, calls window with sample calls, chat window, onboarding pages, light + dark themes, PNG per screen | PySide6 | 3, 4 |
| Screen-share hiding: affinity read back + desktop screenshot without the test window (native QPA) | runner | 3 |
| HTML -> PDF via QtWebEngine offscreen: page count, footer text on every page, Hebrew text extractable | PySide6 | 3 |
| Launch-at-login registry write/read/delete (test value name) | winreg | 3 |
| Greenolink OAuth + MCP against `tests/mock_greenolink.py` (sse/json/deny/ttl/fail_send modes), never the real server | subprocess | 4 |
| Chat logic tables (isSimpleLooks, follow-ups, memory add/forget, action JSON validation) | pure Python | 4 |
| Secrets chunking round trip (in-memory backend) | pure Python | 4 |
| install.ps1 in a temp `%LOCALAPPDATA%` with `TIMMY_TEST_*` switches (tiny model, no shortcuts), then `python -m timmy --version` from the venv | pwsh | 4 |

What CI cannot prove (manual QA on a real PC before release): real Chrome audio from a Meet call, a Bluetooth headset
switch mid-call, Windows 10 2004 itself, SmartScreen / Defender Controlled Folder Access behavior.

## 13. Build steps (sequential, each green in CI on its own)

1. **Audio capture.** `timmy/audio/*` (wasapi declarations, process loopback for the chrome.exe tree, default mic
   with device follow, soxr resampler, wall-clock padder, Chrome PID discovery) and the capture process
   `python -m timmy.audio.capture` writing protocol frames to stdout and JSON status to stderr (`--fake-wav`,
   `--seconds`, `--list`, `--mic-test` for tests and onboarding). Plus `tools/sync_mac.py` with the drift test.
   CI: resampler/padder unit tests, COM init + enumerator + loopback activation smoke on the runner, capture process
   end-to-end with a fake source. Exit gate: decide comtypes vs the C# helper fallback.
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
4. **Integrations, chats, onboarding, installer.** Greenolink (OAuth PKCE loopback, MCP, draft -> confirm -> send,
   keyring with chunking), "שפר את טימי" / "עצב את טימי" / "עיצוב ה-PDF" chats with conversation context, memory.json
   and linked Claude skills (and skill creation, images in the PDF), onboarding (mic privacy, Chrome, Claude Code +
   Git for Windows + login, folder + free space, launch at login), `install.ps1` / `uninstall.ps1`, INSTALL.md and
   README Windows sections. CI: Greenolink against the mock, chat tables, onboarding screenshots, installer dry run.

## 14. Risks and open questions

| Risk | Impact | Mitigation |
|---|---|---|
| comtypes COM callback (agile handler) for ActivateAudioInterfaceAsync misbehaves | no Chrome audio | MTA-only capture process; C# helper fallback from windows-beta (decision at end of step 1) |
| CPU too slow on old laptops | lines appear late | cpu profile (6.3), BELOW_NORMAL priority, the transcript is still complete at the end |
| Claude Code needs Git for Windows | no summaries | onboarding check + download link; transcript works without Claude |
| Credential Manager 2560-byte limit | Greenolink tokens lost | chunked storage, tested |
| Defender Controlled Folder Access blocks writes to Documents | recordings fail | detect the PermissionError, onboarding explains (text exists in windows-beta README) |
| SmartScreen / AV flags pythonw from %LOCALAPPDATA% | scary prompts | per-user venv is common; PyInstaller + signing later if needed |
| Windows 10 reached end of support (Oct 2025) | fewer Win10 users | still supported from 19041, Windows 11 is the main target |
| Hebrew RTL rendering differences in Qt | layout bugs | screenshots of every screen in CI, reviewed by the QA agents |
