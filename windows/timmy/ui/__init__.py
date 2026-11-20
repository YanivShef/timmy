"""PySide6 UI (build steps 3 and 4). Hebrew, right-to-left, no long dash anywhere.

app.py           QApplication, RTL, single instance, tray icon + menu, screen-share hiding on every window
pill.py          frameless always-on-top top-center pill (Timmy's face) that expands into the recording card
card.py          live transcript card: speaker tags, partial "מתמלל…", key points, stop / cancel (two-step) / hide
calls_window.py  "השיחות שלי": list, search, summary, rename, PDF, transcript, open folder
pdf_render.py    HTML -> A4 PDF with QtWebEngine printToPdf (footer + page numbers)
chat_window.py   "שפר את טימי" / "עצב את טימי" / "עיצוב ה-PDF" chats
send_whatsapp.py draft -> confirm recipient -> send (never without the click)
onboarding.py    mic permission, Chrome, Claude Code, folder + disk, launch at login
theme.py         accent / eyes / background colors with the Mac contrast rules
"""
