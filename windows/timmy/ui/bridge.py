"""The glue between the Qt-free models (chat/*, integrations/whatsapp.py, onboard.py) and Qt: spawn(fn, done) runs fn
in a worker thread and calls done(result) on the UI thread; notify() may be called from any thread and repaints the
window (a refresh that notifies again does not recurse)."""
from __future__ import annotations

import sys
import threading
import traceback

from PySide6.QtCore import QObject, Signal


class Bridge(QObject):
    changed = Signal()
    _done = Signal(object, object)
    _poke = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._done.connect(self._call)
        self._poke.connect(self._schedule)
        self._pending = False
        self.running = 0

    def _call(self, cb, r):
        self.running -= 1
        try:
            cb(r)
        except Exception:
            traceback.print_exc(file=sys.stderr)
        self._schedule()

    def spawn(self, fn, done):
        self.running += 1

        def run():
            try:
                r = fn()
            except Exception:
                traceback.print_exc(file=sys.stderr)
                self._done.emit(lambda _r: None, None)
                return
            self._done.emit(done, r)
        threading.Thread(target=run, name="timmy-task", daemon=True).start()

    def notify(self):
        self._poke.emit()          # queued when called from a worker thread

    def _schedule(self):
        if self._pending:          # a refresh that triggers another notify: no recursion
            return
        self._pending = True
        try:
            self.changed.emit()
        finally:
            self._pending = False
