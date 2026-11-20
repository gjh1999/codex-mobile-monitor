"""Desktop dialogs for pairing a new workstation or inviting another device."""

import threading
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QVBoxLayout,
)

from cloud_pairing import create_invite, join
from project_library import configured_root, set_project_root


class PairingDialog(QDialog):
    completed = Signal(bool, str)

    def __init__(self, parent, root, coordination=None, *, first=False):
        super().__init__(parent)
        self.root = root
        self.coordination = coordination
        self.first = first
        self.paired = False
        self.busy = False
        self.resize(510, 180)
        box = QVBoxLayout(self)
        if coordination:
            self.setWindowTitle("邀请另一台电脑")
            instructions = "在另一台电脑粘贴此配对码。配对码十分钟内有效。"
        elif first:
            self.setWindowTitle("设置项目母目录")
            instructions = "选择这台电脑存放各个项目的上一级文件夹。"
        else:
            self.setWindowTitle("连接澜汐")
            instructions = "在已配对电脑上点「配对」生成配对码，再粘贴到这里。"
        box.addWidget(QLabel(instructions))
        self.code = QLineEdit()
        self.code.setMaxLength(12000)
        self.code.setReadOnly(bool(coordination))
        self.code.setPlaceholderText("配对码（有另一台已配对电脑时填写）" if first else "配对码")
        if not first:
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
        if first:
            box.addWidget(self.code)
            self.folder.setFocus()
        self.status = QLabel()
        self.status.setWordWrap(True)
        box.addWidget(self.status)
        buttons = QHBoxLayout()
        self.close_button = QPushButton("关闭" if coordination else "稍后")
        self.close_button.clicked.connect(self.reject)
        buttons.addWidget(self.close_button)
        buttons.addStretch()
        self.action = QPushButton("生成配对码" if coordination else "开始使用" if first else "连接并同步")
        if first:
            self.code.textChanged.connect(self.update_first_action)
        self.action.clicked.connect(self.submit)
        self.action.setDefault(True)
        buttons.addWidget(self.action)
        box.addLayout(buttons)
        self.completed.connect(self.finish)

    def update_first_action(self, text):
        self.action.setText("连接并同步" if text.strip() else "开始使用")

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
            invitation = self.code.text().strip()
            folder = Path(self.folder.text().strip())
            if not folder.is_absolute() or not folder.is_dir():
                self.status.setText("请选择可用的项目母目录。")
                return
            if self.first and not invitation:
                try:
                    set_project_root(self.root, folder)
                except OSError as error:
                    self.status.setText(f"无法保存项目母目录：{error}")
                    return
                super().accept()
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
            self.paired = True
            super().accept()
