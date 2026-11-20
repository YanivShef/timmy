"""File and folder pickers that stay hidden from screen sharing.

The static QFileDialog calls open the native Windows shell dialog: it is not a Qt widget, so the ShareHider event
filter (ui/app.py) never sees it and WDA_EXCLUDEFROMCAPTURE is never set on it. A picker opened during a call (a logo
from "שפר את טימי", the onboarding folder step) would then show in Meet's "share entire screen". While hiding is on,
Timmy uses Qt's own dialog instead (a QWidget window, so the filter hides it like every other Timmy window); with
hiding off the familiar native dialog is used.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog, QWidget


def hiding_on() -> bool:
    from timmy.ui.app import hide_in_share_setting
    try:
        return hide_in_share_setting()
    except Exception:
        return True


def make_dialog(parent: QWidget | None, title: str, folder: str = "", name_filter: str = "",
                mode: QFileDialog.FileMode = QFileDialog.FileMode.ExistingFile) -> QFileDialog:
    d = QFileDialog(parent, title, folder or "")
    d.setFileMode(mode)
    if mode == QFileDialog.FileMode.Directory:
        d.setOption(QFileDialog.Option.ShowDirsOnly, True)
    if name_filter:
        d.setNameFilters([f for f in name_filter.split(";;") if f.strip()])
    if hiding_on():
        d.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        d.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
    return d


def _run(d: QFileDialog) -> list[str]:
    try:
        return list(d.selectedFiles()) if d.exec() == QFileDialog.DialogCode.Accepted else []
    finally:
        d.deleteLater()


def existing_directory(parent, title: str, folder: str = "") -> str:
    files = _run(make_dialog(parent, title, folder, mode=QFileDialog.FileMode.Directory))
    return files[0] if files else ""


def open_file(parent, title: str, folder: str = "", name_filter: str = "") -> str:
    files = _run(make_dialog(parent, title, folder, name_filter))
    return files[0] if files else ""


def open_files(parent, title: str, folder: str = "", name_filter: str = "") -> list[str]:
    return _run(make_dialog(parent, title, folder, name_filter, QFileDialog.FileMode.ExistingFiles))
