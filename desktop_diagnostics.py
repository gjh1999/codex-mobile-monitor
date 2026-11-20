"""Expose the portable acceptance checks in the desktop."""

import codecs
import re
import sys
from pathlib import Path

from PySide6.QtCore import QProcess, QProcessEnvironment, QTimer
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QVBoxLayout,
)


class DiagnosticsDialog(QDialog):
    def __init__(self, parent, has_pairing, pair_device):
        super().__init__(parent)
        self.has_pairing = has_pairing
        self.setWindowTitle("澜汐测试")
        self.resize(670, 400)
        self.runner = QProcess(self)
        self.runner.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("PYINSTALLER_RESET_ENVIRONMENT", "1")
        self.runner.setProcessEnvironment(environment)
        self.runner.readyReadStandardOutput.connect(self.read_output)
        self.runner.finished.connect(self.process_finished)
        self.runner.errorOccurred.connect(self.process_error)
        box = QVBoxLayout(self)
        self.status = QLabel("本机自检可直接运行，双机测试需要两台电脑都已连接云服务器。")
        self.status.setWordWrap(True)
        box.addWidget(self.status)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        box.addWidget(self.output, 1)
        header = QHBoxLayout()
        self.self_test = QPushButton("开始自检")
        self.self_test.clicked.connect(self.run_self_test)
        header.addWidget(self.self_test)
        pairing = QPushButton("连接云服务器")
        pairing.clicked.connect(lambda: pair_device(self))
        header.addWidget(pairing)
        self.source = QPushButton("发起双机测试")
        self.source.clicked.connect(lambda: self.run("cloud-source"))
        header.addWidget(self.source)
        self.copy = QPushButton("复制测试码")
        self.copy.setEnabled(False)
        self.copy.clicked.connect(self.copy_code)
        header.addWidget(self.copy)
        box.addLayout(header)
        row = QHBoxLayout()
        self.code = QLineEdit()
        self.code.setPlaceholderText("另一台电脑生成的测试码")
        self.code.setMaxLength(32)
        row.addWidget(self.code, 1)
        self.receiver = QPushButton("回传测试")
        self.receiver.clicked.connect(self.reply)
        row.addWidget(self.receiver)
        box.addLayout(row)
        QTimer.singleShot(0, self.run_self_test)

    def run_self_test(self):
        self.run("diagnostics" if self.has_pairing() else "local")

    def reply(self):
        code = self.code.text().strip()
        if not re.fullmatch(r"[a-f0-9]{32}", code):
            self.status.setText("请输入另一台电脑生成的 32 位测试码。")
            return
        self.run("cloud-receiver", ["--code", code])

    def run(self, mode, options=()):
        if self.runner.state() != QProcess.ProcessState.NotRunning:
            return
        if mode in ("cloud-source", "cloud-receiver") and not self.has_pairing():
            self.status.setText("请先点击「连接云服务器」。")
            return
        executable = Path(sys.executable)
        if not getattr(sys, "frozen", False):
            self.status.setText("请在澜汐 Windows EXE 中运行测试。")
            return
        helper = Path(sys._MEIPASS) / "Lanxi-Verify.exe"
        if not helper.is_file():
            self.status.setText("内置测试程序缺失，请重新下载澜汐。")
            return
        self.output.clear()
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.copy.setEnabled(False)
        self.set_busy(True)
        self.status.setText("正在测试…")
        self.runner.start(str(helper), ["--exe", str(executable), "--mode", mode, *options])

    def set_busy(self, busy):
        for button in (self.self_test, self.source, self.receiver):
            button.setEnabled(not busy)

    def read_output(self):
        output = self.decoder.decode(bytes(self.runner.readAllStandardOutput()))
        self.output.moveCursor(QTextCursor.MoveOperation.End)
        self.output.insertPlainText(output)
        self.copy.setEnabled(bool(self.test_code()))

    def test_code(self):
        return re.search(r"测试码 ([a-f0-9]{32})", self.output.toPlainText())

    def copy_code(self):
        match = self.test_code()
        if match:
            QApplication.clipboard().setText(match[1])

    def process_finished(self, code, exit_status):
        self.read_output()
        self.set_busy(False)
        self.status.setText("测试通过。" if code == 0 and exit_status == QProcess.ExitStatus.NormalExit
                            else "测试未通过，请查看下方结果。")

    def process_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.set_busy(False)
            self.status.setText("无法启动内置测试程序，请重新下载澜汐。")
