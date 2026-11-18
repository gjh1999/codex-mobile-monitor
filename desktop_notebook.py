"""Local notes, archives and todos for the native desktop."""
import sqlite3
from contextlib import closing
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPlainTextEdit, QPushButton, QTabWidget, QVBoxLayout, QWidget,
)

from desktop_theme import ui_font


class NotebookStore:
    def __init__(self, path: Path):
        self.path = path
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('''
                CREATE TABLE IF NOT EXISTS notebook (
                    id INTEGER PRIMARY KEY,
                    kind TEXT NOT NULL CHECK(kind IN ('note','todo')),
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    filed INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            ''')

    def entries(self, kind, filed):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute(
                'SELECT id, title, body FROM notebook WHERE kind=? AND filed=? ORDER BY updated_at DESC, id DESC',
                (kind, int(filed)),
            ).fetchall()

    def save(self, kind, entry_id, title, body):
        with closing(sqlite3.connect(self.path)) as db, db:
            if entry_id is None:
                return db.execute('INSERT INTO notebook(kind, title, body) VALUES(?,?,?)', (kind, title, body)).lastrowid
            db.execute(
                'UPDATE notebook SET title=?, body=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND kind=?',
                (title, body, entry_id, kind),
            )
            return entry_id

    def set_filed(self, kind, entry_id, filed):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                'UPDATE notebook SET filed=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND kind=?',
                (int(filed), entry_id, kind),
            )


class NotebookPage(QWidget):
    def __init__(self, store, kind):
        super().__init__()
        self.store, self.kind = store, kind
        self.entry_id = None
        self.dirty = False
        self.entries = {}
        self.draft = QTimer(self)
        self.draft.setSingleShot(True)
        self.draft.setInterval(450)
        self.draft.timeout.connect(self.save)

        note = kind == 'note'
        shell = QHBoxLayout(self)
        shell.setContentsMargins(8, 8, 8, 8)
        navigation = QVBoxLayout()
        self.view = QComboBox()
        self.view.setAccessibleName('查看记录')
        self.view.addItems(['随记', '已归档'] if note else ['待处理', '已完成'])
        self.view.currentIndexChanged.connect(self.change_view)
        navigation.addWidget(self.view)
        self.list = QListWidget()
        self.list.setAccessibleName('随记列表' if note else '待办列表')
        self.list.setMaximumWidth(210)
        self.list.currentItemChanged.connect(self.select_entry)
        navigation.addWidget(self.list, 1)
        create = QPushButton('新记' if note else '新增待办')
        create.clicked.connect(self.new_entry)
        navigation.addWidget(create)
        shell.addLayout(navigation)

        fields = QVBoxLayout()
        self.title = QLineEdit()
        self.title.setFont(ui_font(13, True))
        self.title.setPlaceholderText('题名' if note else '要做的事')
        self.title.setAccessibleName('标题')
        self.title.setMaxLength(120)
        self.title.textChanged.connect(self.edited)
        fields.addWidget(self.title)
        self.body = QPlainTextEdit()
        self.body.setPlaceholderText('把掠过心头的一笔记下来…' if note else '补充做法或线索…')
        self.body.setAccessibleName('随记正文' if note else '待办说明')
        self.body.textChanged.connect(self.edited)
        fields.addWidget(self.body, 1)
        actions = QHBoxLayout()
        actions.addStretch()
        self.file_button = QPushButton()
        self.file_button.clicked.connect(self.move_entry)
        actions.addWidget(self.file_button)
        self.save_button = QPushButton('保存')
        self.save_button.clicked.connect(lambda: self.save())
        actions.addWidget(self.save_button)
        fields.addLayout(actions)
        shell.addLayout(fields, 1)
        self.load_list()
        self.set_entry(None)

    @property
    def filed(self):
        return self.view.currentIndex() == 1

    def load_list(self):
        self.entries = {entry_id: (title, body) for entry_id, title, body in self.store.entries(self.kind, self.filed)}
        with QSignalBlocker(self.list):
            self.list.clear()
            for entry_id, (title, body) in self.entries.items():
                caption = title or (body.strip().splitlines() or ['未题'])[0]
                item = QListWidgetItem(caption[:80], self.list)
                item.setData(Qt.ItemDataRole.UserRole, entry_id)
                if entry_id == self.entry_id:
                    self.list.setCurrentItem(item)
        self.file_button.setText('恢复' if self.filed else ('归档' if self.kind == 'note' else '完成'))
        self.file_button.setEnabled(self.entry_id is not None)

    def set_entry(self, entry_id):
        self.entry_id = entry_id
        title, body = self.entries.get(entry_id, ('', ''))
        with QSignalBlocker(self.title), QSignalBlocker(self.body):
            self.title.setText(title)
            self.body.setPlainText(body)
        self.dirty = False
        self.file_button.setEnabled(entry_id is not None)
        editable = not self.filed or entry_id is not None
        self.title.setEnabled(editable)
        self.body.setEnabled(editable)
        self.save_button.setEnabled(editable)

    def edited(self):
        self.dirty = True
        self.draft.start()

    def save(self, reload=True):
        self.draft.stop()
        if not self.dirty:
            return True
        title, body = self.title.text().strip(), self.body.toPlainText()
        if self.entry_id is None and not (title or body.strip()):
            return True
        try:
            self.entry_id = self.store.save(self.kind, self.entry_id, title, body)
        except (OSError, sqlite3.Error):
            QMessageBox.warning(self, '保存未完成', '本机笔记暂时无法写入，内容仍在编辑框里。')
            return False
        self.dirty = False
        if reload:
            self.load_list()
        return True

    def select_entry(self, current, previous):
        entry_id = current.data(Qt.ItemDataRole.UserRole) if current else None
        changed = self.dirty
        if self.save(reload=False):
            self.set_entry(entry_id)
            if changed:
                self.load_list()
        else:
            with QSignalBlocker(self.list):
                self.list.setCurrentItem(previous)

    def change_view(self):
        if not self.save(reload=False):
            with QSignalBlocker(self.view):
                self.view.setCurrentIndex(0 if self.filed else 1)
            return
        self.set_entry(None)
        self.load_list()

    def new_entry(self):
        if not self.save():
            return
        self.view.setCurrentIndex(0)
        self.set_entry(None)
        with QSignalBlocker(self.list):
            self.list.setCurrentRow(-1)
        self.title.setFocus()

    def move_entry(self):
        if not self.save(reload=False) or self.entry_id is None:
            return
        try:
            self.store.set_filed(self.kind, self.entry_id, not self.filed)
        except (OSError, sqlite3.Error):
            QMessageBox.warning(self, '更新未完成', '暂时无法更新这条记录。')
            return
        self.set_entry(None)
        self.load_list()


class NotebookDialog(QDialog):
    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.setObjectName('notebook')
        self.setWindowTitle('吉光片羽')
        self.resize(690, 450)
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.notes = NotebookPage(store, 'note')
        self.todos = NotebookPage(store, 'todo')
        self.tabs.addTab(self.notes, '吉光片羽')
        self.tabs.addTab(self.todos, '待办')
        layout.addWidget(self.tabs)

    def flush(self):
        notes_saved = self.notes.save()
        todos_saved = self.todos.save()
        return notes_saved and todos_saved

    def done(self, result):
        if self.flush():
            super().done(result)
