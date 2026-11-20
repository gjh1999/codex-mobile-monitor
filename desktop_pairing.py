"""Desktop dialogs for pairing a new workstation or inviting another device."""

import threading
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QVBoxLayout,
)

from cloud_pairing import MAX_CODE, SETUP_PREFIX, create_invite, decode_invite, join, join_workspace
from project_library import configured_root, set_project_root


CONNECT_CLOUD = "连接云服务器"


class WorkspaceConnectDialog(QDialog):
    completed = Signal(str)

    def __init__(self, parent, root, grant, *, enrolled=False, startup=False):
        super().__init__(parent)
        self.root = root
        self.grant = grant
        self.enrolled = enrolled
        self.online = False
        self.busy = False
        self.setWindowTitle("选择启动方式" if startup else CONNECT_CLOUD)
        box = QVBoxLayout(self)
        self.status = QLabel("连接后，可以在同一个私人工作区接续其他电脑上的项目和会话。")
        self.status.setWordWrap(True)
        box.addWidget(self.status)
        row = QHBoxLayout()
        self.local = QPushButton("本机使用" if startup else "取消")
        self.local.clicked.connect(self.accept if startup else self.reject)
        row.addWidget(self.local)
        row.addStretch()
        self.connect = QPushButton(CONNECT_CLOUD)
        self.connect.setDefault(True)
        self.connect.clicked.connect(self.connect_workspace)
        row.addWidget(self.connect)
        box.addLayout(row)
        self.completed.connect(self.finish)
        self.resize(440, self.sizeHint().height())

    def connect_workspace(self):
        if self.enrolled:
            self.finish("")
            return
        self.busy = True
        self.connect.setEnabled(False)
        self.local.setEnabled(False)
        self.status.setText("正在连接云服务器…")

        def work():
            try:
                join_workspace(self.root, self.grant)
            except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
                self.completed.emit(str(error) or "连接失败，请重试。")
            else:
                self.completed.emit("")

        threading.Thread(target=work, daemon=True, name="lanxi-workspace-connect").start()

    def reject(self):
        if not self.busy:
            super().reject()

    def finish(self, message):
        self.busy = False
        if message:
            self.status.setText(message)
            self.connect.setEnabled(True)
            self.local.setEnabled(True)
        else:
            self.online = True
            super().accept()


class PairingDialog(QDialog):
    completed = Signal(bool, str)
    waiting = Signal()

    def __init__(self, parent, root, coordination=None, *, first=False):
        super().__init__(parent)
        self.root = root
        self.coordination = coordination
        self.first = first
        self.paired = False
        self.busy = False
        self.cancel_wait = None
        self.resize(510, 180)
        box = QVBoxLayout(self)
        if coordination:
            self.setWindowTitle("邀请另一台电脑")
            instructions = "在另一台电脑粘贴此邀请码。邀请码十分钟内有效。"
        elif first:
            self.setWindowTitle("设置项目母目录")
            instructions = "选择这台电脑存放各个项目的上一级文件夹。"
        else:
            self.setWindowTitle(CONNECT_CLOUD)
            instructions = "输入已连接电脑生成的邀请码。"
        box.addWidget(QLabel(instructions))
        self.code = QLineEdit()
        self.code.setMaxLength(MAX_CODE)
        self.code.setReadOnly(bool(coordination))
        self.code.setPlaceholderText("邀请码（选填）" if first else "邀请码")
        if coordination:
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
            credentials = QHBoxLayout()
            credentials.addWidget(self.code)
            load = QPushButton("导入双机设置…")
            load.clicked.connect(self.load_setup)
            credentials.addWidget(load)
            box.addLayout(credentials)
            if first:
                self.folder.setFocus()
        self.status = QLabel()
        self.status.setWordWrap(True)
        box.addWidget(self.status)
        buttons = QHBoxLayout()
        self.close_button = QPushButton("关闭" if coordination else "稍后")
        self.close_button.clicked.connect(self.reject)
        buttons.addWidget(self.close_button)
        buttons.addStretch()
        self.action = QPushButton("生成邀请码" if coordination else "开始使用" if first else "连接并同步")
        if not coordination:
            self.code.textChanged.connect(self.update_join_action)
        self.action.clicked.connect(self.submit)
        self.action.setDefault(True)
        buttons.addWidget(self.action)
        box.addLayout(buttons)
        self.completed.connect(self.finish)
        self.waiting.connect(lambda: self.status.setText("已提交申请。请在另一台电脑导入同一份设置文件，配对会自动完成。"))

    def update_join_action(self, text):
        automatic = text.strip().startswith(SETUP_PREFIX)
        self.code.setEchoMode(QLineEdit.EchoMode.Password if automatic else QLineEdit.EchoMode.Normal)
        self.action.setText("申请自动配对" if automatic else "开始使用" if self.first and not text.strip() else "连接并同步")

    def load_setup(self):
        selected, _ = QFileDialog.getOpenFileName(self, "选择双机设置文件", "", "澜汐双机设置 (*.lanxi-pairing)")
        if selected:
            try:
                with Path(selected).open(encoding="utf-8-sig") as stream:
                    setup = stream.read(MAX_CODE + 1).strip()
                decode_invite(setup, prefix=SETUP_PREFIX)
            except (OSError, ValueError):
                self.status.setText("无法读取双机设置文件，请选择服务器签发的文件。")
                return
            self.code.setText(setup)
            self.status.setText("已导入设置文件。选择项目母目录后，点「申请自动配对」。")

    def pick_folder(self):
        location = QFileDialog.getExistingDirectory(self, "选择项目母目录", self.folder.text() or str(Path.home()))
        if location:
            self.folder.setText(location)

    def submit(self):
        if self.coordination and self.code.text():
            QApplication.clipboard().setText(self.code.text())
            self.status.setText("已复制邀请码。")
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
                if self.cancel_wait is not None:
                    if not join(self.root, invitation, folder, waiting=self.waiting.emit, cancel=self.cancel_wait):
                        return None
                else:
                    join(self.root, invitation, folder)
                return ""
            if invitation.startswith(SETUP_PREFIX):
                self.cancel_wait = threading.Event()
        else:
            def operation():
                return create_invite(self.coordination)
        self.busy = True
        self.action.setEnabled(False)
        self.close_button.setEnabled(self.cancel_wait is not None)
        self.status.setText("正在连接协调服务…")

        def work():
            try:
                result = operation()
            except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
                self.completed.emit(False, str(error))
            else:
                self.completed.emit(result is not None, result or "")

        threading.Thread(target=work, daemon=True, name="lanxi-pairing").start()

    def reject(self):
        if not self.busy:
            super().reject()
        elif self.cancel_wait is not None:
            self.cancel_wait.set()
            self.close_button.setEnabled(False)
            self.status.setText("正在停止等待…")

    def finish(self, success, text):
        self.busy = False
        self.cancel_wait = None
        self.close_button.setEnabled(True)
        self.action.setEnabled(True)
        if not success:
            if text:
                self.status.setText(text)
            else:
                super().reject()
        elif self.coordination:
            self.code.setText(text)
            self.code.selectAll()
            self.status.setText("邀请码已生成。")
            self.action.setText("复制邀请码")
        else:
            self.paired = True
            super().accept()
