"""The desktop settings sheet for the optional grouping service."""
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QCheckBox, QFormLayout, QLabel, QLineEdit, QVBoxLayout

from model_groups import update_settings


class ModelApiDialog(QDialog):
    def __init__(self, parent, organizer):
        super().__init__(parent)
        self.organizer = organizer
        self.setObjectName('lanxiDialog')
        self.setWindowTitle('大模型 API')
        self.setMinimumWidth(460)
        current = organizer.settings

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(11)
        intro = QLabel('设置 OpenAI 兼容接口后，澜汐会在后台把任务、随记和待办归组。')
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QFormLayout()
        layout.addLayout(form)
        self.address = QLineEdit(current.base_url)
        self.address.setPlaceholderText('https://api.example.com/v1')
        form.addRow('API 基础地址', self.address)
        self.model = QLineEdit(current.model)
        self.model.setPlaceholderText('接口提供的模型名称')
        form.addRow('模型', self.model)
        self.secret = QLineEdit()
        self.secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.secret.setPlaceholderText('已保存，留空沿用' if current.encrypted_key else '按接口要求填写')
        form.addRow('API Key', self.secret)
        self.remove_key = QCheckBox('移除已保存的密钥')
        self.remove_key.setVisible(bool(current.encrypted_key))
        self.remove_key.toggled.connect(self.secret.setDisabled)
        form.addRow('', self.remove_key)
        self.notice = QLabel('归组会发送条目标题或正文片段到所填地址。清空地址和模型可暂停归组。')
        self.notice.setObjectName('dialogHint')
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        self.error = QLabel('')
        self.error.setWordWrap(True)
        self.error.setStyleSheet('color:#8a3428;')
        self.error.hide()
        layout.addWidget(self.error)
        actions = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        save = actions.button(QDialogButtonBox.StandardButton.Save)
        save.setText('保存')
        save.setObjectName('approvalAllow')
        actions.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        actions.accepted.connect(self.accept)
        actions.rejected.connect(self.reject)
        layout.addWidget(actions)

    def accept(self):
        key = '' if self.remove_key.isChecked() else (self.secret.text().strip() or None)
        try:
            settings = update_settings(self.organizer.root, self.address.text(), self.model.text(), key)
        except ValueError as error:
            self.error.setText(str(error))
            self.error.show()
            return
        except OSError:
            self.error.setText('暂时无法在本机保存设置或保护密钥。')
            self.error.show()
            return
        self.organizer.configure(settings)
        self.secret.clear()
        super().accept()
