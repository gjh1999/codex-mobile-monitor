"""Prompt for a private update and switch executables after a normal exit."""

import subprocess
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QMessageBox

import monitor
from portable_update import latest, prepare, register_start_menu_shortcut, restart_after_exit


class DesktopUpdates(QObject):
    result = Signal(str, object)

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.busy = False
        self.seen = None
        self.staged = None
        self.result.connect(self.receive)
        self.timer = QTimer(self)
        self.timer.setInterval(180_000)
        self.timer.timeout.connect(self.check)

    def watch(self):
        threading.Thread(target=self.ensure_shortcut, daemon=True, name="lanxi-start-menu").start()
        self.timer.start()
        QTimer.singleShot(12_000, self.check)

    def ensure_shortcut(self):
        try:
            register_start_menu_shortcut(Path(sys.executable), monitor.ROOT.parent / "versions")
        except (OSError, subprocess.SubprocessError):
            pass

    def work(self, phase, callback):
        self.busy = True

        def run():
            try:
                self.result.emit(phase, callback())
            except Exception as error:
                self.result.emit(phase, error)

        threading.Thread(target=run, daemon=True, name="lanxi-update").start()

    def check(self, manual=False):
        if self.busy or not getattr(sys, "frozen", False):
            return
        if self.staged:
            if manual:
                self.restart()
            return
        self.work("manual" if manual else "check", lambda: latest(monitor.config()))

    def receive(self, phase, result):
        self.busy = False
        if self.window.stop.is_set():
            return
        if isinstance(result, Exception):
            if phase != "check":
                self.window._popup("更新未完成", str(result))
            return
        if phase == "install":
            self.staged = result
            self.restart()
            return
        if result is None:
            if phase == "manual":
                self.window._popup("澜汐更新", "当前已是最新版本。")
            return
        version = result["version"]
        if phase == "check" and self.seen == version:
            return
        self.seen = version
        reply = QMessageBox.question(self.window, "澜汐更新", f"发现澜汐 R{version}。现在下载并更新吗？")
        if reply == QMessageBox.StandardButton.Yes:
            self.window.control_message.setText("正在后台下载并校验更新…")
            root = monitor.ROOT.parent / "versions"
            self.work("install", lambda: prepare(monitor.config(), result, root))

    def restart(self):
        state = monitor.snapshot()
        clients = (state, state.get("claude") or {})
        active = any(
            (task.get("threadStatus") or {}).get("type") == "active"
            for client in clients for task in (client.get("controls") or {}).get("tasks", [])
        )
        if active or monitor.approvals.public():
            self.window._popup("更新已下载", "请等当前对话和审批结束，再从托盘选择「检查更新」切换新版。")
            return
        try:
            restart_after_exit(Path(self.staged))
        except OSError as error:
            self.window._popup("无法打开新版", str(error))
            return
        self.window.quit_app()
