"""Desktop dialogs for pairing a new workstation or inviting another device."""

import threading
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QVBoxLayout,
)

from cloud_pairing import create_invite, join
from project_library import configured_root


class PairingDialog(QDialog):
    completed = Signal(bool, str)

    def __init__(self, parent, root, coordination=None):
        super().__init__(parent)
        self.root = root
        self.coordination = coordination
        self.busy = False
        self.setWindowTitle("邀请另一台电脑" if coordination else "连接澜汐")
        self.resize(510, 180)
        box = QVBoxLayout(self)
        box.addWidget(QLabel("在另一台电脑粘贴此配对码。配对码十分钟内有效。" if coordination
                             else "粘贴已配对电脑给出的配对码，然后选择本机项目母目录。"))
        self.code = QLineEdit()
        self.code.setMaxLength(12000)
        self.code.setReadOnly(bool(coordination))
        self.code.setPlaceholderText("配对码")
        box.addWidget(self.code)
        if coordination is None:
            location = QHBoxLayout()
            self.folder = QLineEdit(str(configured_root(root) or ""))
            self.folder.setPlaceholderText("本机项目母目录")
            location.addWidget(self.folder)
            pick = QPushButton("浏览…")
            pick.clicked.connect(self.pick_folder)
            location.addWidget(pick)
            box.addLayout(location)
        self.status = QLabel()
        self.status.setWordWrap(True)
        box.addWidget(self.status)
        buttons = QHBoxLayout()
        self.close_button = QPushButton("关闭" if coordination else "稍后")
        self.close_button.clicked.connect(self.reject)
        buttons.addWidget(self.close_button)
        buttons.addStretch()
        self.action = QPushButton("生成配对码" if coordination else "连接并同步")
        self.action.clicked.connect(self.submit)
        self.action.setDefault(True)
        buttons.addWidget(self.action)
        box.addLayout(buttons)
        self.completed.connect(self.finish)

    def pick_folder(self):
        location = QFileDialog.getExistingDirectory(self, "选择项目母目录", self.folder.text() or str(Path.home()))
        if location:
            self.folder.setText(location)

    def submit(self):
        if self.coordination and self.code.text():
            QApplication.clipboard().setText(self.code.text())
            self.status.setText("已复制配对码。")
            return
        if self.coordination is None:
            invitation = self.code.text()
            folder = Path(self.folder.text().strip())
            if not folder.is_absolute() or not folder.is_dir():
                self.status.setText("请选择可用的项目母目录。")
                return
            def operation():
                join(self.root, invitation, folder)
                return ""
        else:
            def operation():
                return create_invite(self.coordination)
        self.busy = True
        self.action.setEnabled(False)
        self.close_button.setEnabled(False)
        self.status.setText("正在连接协调服务…")

        def work():
            try:
                result = operation()
            except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
                self.completed.emit(False, str(error))
            else:
                self.completed.emit(True, result)

        threading.Thread(target=work, daemon=True, name="lanxi-pairing").start()

    def reject(self):
        if not self.busy:
            super().reject()

    def finish(self, success, text):
        self.busy = False
        self.close_button.setEnabled(True)
        self.action.setEnabled(True)
        if not success:
            self.status.setText(text)
        elif self.coordination:
            self.code.setText(text)
            self.code.selectAll()
            self.status.setText("配对码已生成。")
            self.action.setText("复制配对码")
        else:
            super().accept()
