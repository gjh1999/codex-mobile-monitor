"""Expose the portable acceptance checks in the desktop."""

import codecs
import sys
from pathlib import Path

from PySide6.QtCore import QProcess, QProcessEnvironment, QTimer
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QHBoxLayout, QLabel, QPlainTextEdit,
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
        self.status = QLabel()
        self.status.setWordWrap(True)
        box.addWidget(self.status)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        box.addWidget(self.output, 1)
        header = QHBoxLayout()
        self.self_test = QPushButton("本机自检")
        self.self_test.clicked.connect(lambda: self.run("local"))
        header.addWidget(self.self_test)
        self.server_test = QPushButton("服务器测试")
        self.server_test.clicked.connect(lambda: self.run("server"))
        header.addWidget(self.server_test)
        self.project_test = QPushButton("项目会话诊断")
        self.project_test.clicked.connect(self.choose_project)
        header.addWidget(self.project_test)
        self.pairing = QPushButton("连接云服务器")
        self.pairing.clicked.connect(lambda: pair_device(self))
        header.addWidget(self.pairing)
        self.copy = QPushButton("复制结果")
        self.copy.setEnabled(False)
        self.copy.clicked.connect(lambda: QApplication.clipboard().setText(self.output.toPlainText()))
        self.output.textChanged.connect(lambda: self.copy.setEnabled(not self.output.document().isEmpty()))
        header.addWidget(self.copy)
        box.addLayout(header)
        QTimer.singleShot(0, lambda: self.run("server" if self.has_pairing() else "local"))

    def choose_project(self):
        project = QFileDialog.getExistingDirectory(self, "选择要诊断的项目目录")
        if project:
            options = ["--project-dir", project]
            if self.has_pairing():
                options.append("--with-server")
            self.run("project", options)

    def run(self, mode, options=()):
        if self.runner.state() != QProcess.ProcessState.NotRunning:
            return
        if mode == "server" and not self.has_pairing():
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
        self.set_busy(True)
        self.status.setText("正在测试…")
        self.runner.start(str(helper), ["--exe", str(executable), "--mode", mode, *options])

    def set_busy(self, busy):
        for button in (self.self_test, self.server_test, self.project_test, self.pairing):
            button.setEnabled(not busy)

    def read_output(self):
        output = self.decoder.decode(bytes(self.runner.readAllStandardOutput()))
        self.output.moveCursor(QTextCursor.MoveOperation.End)
        self.output.insertPlainText(output)

    def process_finished(self, code, exit_status):
        self.read_output()
        self.set_busy(False)
        self.status.setText("测试通过。" if code == 0 and exit_status == QProcess.ExitStatus.NormalExit
                            else "测试未通过，请查看下方结果。")

    def process_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.set_busy(False)
            self.status.setText("无法启动内置测试程序，请重新下载澜汐。")
