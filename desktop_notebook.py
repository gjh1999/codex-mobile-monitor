"""Local notes, archives and todos for the native desktop."""
import sqlite3
from contextlib import closing
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, Qt, QTimer
from PySide6.QtWidgets import (
    QButtonGroup, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QStackedWidget, QTextEdit, QVBoxLayout, QWidget,
)

from desktop_glass import FrostedPanel
from desktop_theme import configure_text, ui_font


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


def record_button(text, size=9):
    button = QPushButton(text)
    button.setObjectName('hoverAction')
    button.setFont(ui_font(size))
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


class NotebookPage(QWidget):
    def __init__(self, store, kind, backdrop, select_kind):
        super().__init__()
        self.store, self.kind = store, kind
        self.entry_id = None
        self.filed = False
        self.dirty = False
        self.entries = {}
        self.draft = QTimer(self)
        self.draft.setSingleShot(True)
        self.draft.setInterval(450)
        self.draft.timeout.connect(self.save)

        note = kind == 'note'
        shell = QHBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(7)

        self.list_panel = FrostedPanel(backdrop)
        self.list_panel.setFixedWidth(266)
        navigation = QVBoxLayout(self.list_panel)
        navigation.setContentsMargins(13, 12, 13, 10)
        navigation.setSpacing(8)
        navigation.addSpacing(15)
        categories = QHBoxLayout()
        categories.setSpacing(4)
        category_group = QButtonGroup(self)
        self.category_buttons = {}
        for key, text in (('note', '随记'), ('todo', '待办')):
            button = record_button(text, 12)
            button.setCheckable(True)
            button.setFixedHeight(34)
            category_group.addButton(button)
            button.setChecked(kind == key)
            button.clicked.connect(lambda _checked, key=key: select_kind(key))
            self.category_buttons[key] = button
            categories.addWidget(button)
        categories.addStretch()
        create = QPushButton('＋')
        create.setFixedSize(35, 30)
        create.setCursor(Qt.CursorShape.PointingHandCursor)
        create.setToolTip('新记' if note else '新增待办')
        create.setAccessibleName(create.toolTip())
        create.clicked.connect(self.new_entry)
        categories.addWidget(create, 0, Qt.AlignmentFlag.AlignVCenter)
        navigation.addLayout(categories)

        views = QHBoxLayout()
        views.setSpacing(5)
        view_group = QButtonGroup(self)
        self.view_buttons = {}
        for filed, text in enumerate(('未归档', '已归档') if note else ('待处理', '已完成')):
            button = record_button(text)
            button.setCheckable(True)
            view_group.addButton(button)
            button.setChecked(filed == 0)
            button.clicked.connect(lambda _checked, filed=bool(filed): self.change_view(filed))
            self.view_buttons[bool(filed)] = button
            views.addWidget(button, 1)
        navigation.addLayout(views)

        self.list = QListWidget()
        self.list.setObjectName('notebookList')
        self.list.setAccessibleName('随记列表' if note else '待办列表')
        self.list.setWordWrap(True)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.viewport().setAutoFillBackground(False)
        self.list.currentItemChanged.connect(self.select_entry)
        navigation.addWidget(self.list, 1)
        self.empty = QLabel()
        self.empty.setFont(ui_font(9))
        self.empty.setStyleSheet('color:#24495f;')
        navigation.addWidget(self.empty)
        shell.addWidget(self.list_panel)

        self.editor_panel = FrostedPanel(backdrop)
        fields = QVBoxLayout(self.editor_panel)
        fields.setContentsMargins(15, 12, 15, 12)
        fields.setSpacing(8)
        header = QVBoxLayout()
        header.setSpacing(3)
        caption = QLabel('随记' if note else '待办')
        caption.setFont(ui_font(9))
        caption.setStyleSheet('color:#24495f;')
        header.addWidget(caption)
        row = QHBoxLayout()
        row.setSpacing(5)
        self.title = QLineEdit()
        self.title.setObjectName('notebookTitle')
        self.title.setFont(ui_font(13, True))
        self.title.setPlaceholderText('题名' if note else '要做的事')
        self.title.setAccessibleName('标题')
        self.title.setMaxLength(120)
        self.title.textChanged.connect(self.edited)
        row.addWidget(self.title, 1)
        self.file_button = record_button('')
        self.file_button.clicked.connect(self.move_entry)
        row.addWidget(self.file_button)
        header.addLayout(row)
        fields.addLayout(header)

        divider = QFrame()
        divider.setObjectName('divider')
        divider.setFixedHeight(1)
        fields.addWidget(divider)

        self.body = QTextEdit()
        self.body.setObjectName('composer')
        self.body.setAcceptRichText(False)
        configure_text(self.body, 10, 3)
        self.body.viewport().setAutoFillBackground(False)
        self.body.setPlaceholderText('把掠过心头的一笔记下来…' if note else '补充做法或线索…')
        self.body.setAccessibleName('随记正文' if note else '待办说明')
        self.body.textChanged.connect(self.edited)
        fields.addWidget(self.body, 1)
        footer = QFrame()
        footer.setObjectName('composerBox')
        actions = QHBoxLayout(footer)
        actions.setContentsMargins(10, 6, 8, 6)
        self.saved_status = QLabel('本机自动保存')
        self.saved_status.setFont(ui_font(8.5))
        self.saved_status.setStyleSheet('color:#24495f;')
        actions.addWidget(self.saved_status, 1)
        self.save_button = QPushButton('保存')
        self.save_button.setObjectName('approvalAllow')
        self.save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_button.clicked.connect(lambda: self.save())
        actions.addWidget(self.save_button)
        fields.addWidget(footer)
        shell.addWidget(self.editor_panel, 1)
        self.load_list()
        self.set_entry(None)

    def load_list(self):
        self.entries = {entry_id: (title, body) for entry_id, title, body in self.store.entries(self.kind, self.filed)}
        with QSignalBlocker(self.list):
            self.list.clear()
            for entry_id, (title, body) in self.entries.items():
                caption = title or (body.strip().splitlines() or ['未题'])[0]
                summary = (body.strip().splitlines() or [''])[0] if title else ''
                if len(summary) > 25:
                    summary = summary[:24] + '…'
                item = QListWidgetItem(f'{caption[:80]}\n{summary}' if summary else caption[:80], self.list)
                item.setData(Qt.ItemDataRole.UserRole, entry_id)
                if entry_id == self.entry_id:
                    self.list.setCurrentItem(item)
        self.file_button.setText('恢复' if self.filed else ('归档' if self.kind == 'note' else '完成'))
        self.file_button.setEnabled(self.entry_id is not None)
        self.empty.setText(
            ('暂无归档' if self.filed else '尚无随记') if self.kind == 'note' else ('暂无已完成待办' if self.filed else '暂无待办'))
        self.empty.setVisible(not self.entries)

    def set_entry(self, entry_id):
        self.entry_id = entry_id
        title, body = self.entries.get(entry_id, ('', ''))
        with QSignalBlocker(self.title), QSignalBlocker(self.body):
            self.title.setText(title)
            self.body.setPlainText(body)
        self.dirty = False
        self.saved_status.setText('已保存至本机' if entry_id is not None else '本机自动保存')
        self.file_button.setEnabled(entry_id is not None)
        editable = not self.filed or entry_id is not None
        self.title.setEnabled(editable)
        self.body.setEnabled(editable)
        self.save_button.setEnabled(editable)

    def edited(self):
        self.dirty = True
        self.saved_status.setText('正在保存…')
        self.draft.start()

    def save(self, reload=True):
        self.draft.stop()
        if not self.dirty:
            return True
        title, body = self.title.text().strip(), self.body.toPlainText()
        if self.entry_id is None and not (title or body.strip()):
            self.dirty = False
            self.saved_status.setText('本机自动保存')
            return True
        try:
            self.entry_id = self.store.save(self.kind, self.entry_id, title, body)
        except (OSError, sqlite3.Error):
            self.saved_status.setText('保存未完成')
            QMessageBox.warning(self, '保存未完成', '本机笔记暂时无法写入，内容仍在编辑框里。')
            return False
        self.dirty = False
        self.saved_status.setText('已保存至本机')
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

    def change_view(self, filed):
        if filed == self.filed:
            return
        if not self.save(reload=False):
            self.view_buttons[self.filed].setChecked(True)
            return
        self.filed = filed
        self.view_buttons[filed].setChecked(True)
        self.set_entry(None)
        self.load_list()

    def new_entry(self):
        if not self.save():
            return
        self.change_view(False)
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


class NotebookWorkspace(QWidget):
    def __init__(self, store, backdrop):
        super().__init__()
        self.setObjectName('notebook')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tabs = QStackedWidget()
        self.notes = NotebookPage(store, 'note', backdrop, self.select_kind)
        self.todos = NotebookPage(store, 'todo', backdrop, self.select_kind)
        self.tabs.addWidget(self.notes)
        self.tabs.addWidget(self.todos)
        layout.addWidget(self.tabs)

    def select_kind(self, kind):
        previous = self.tabs.currentWidget()
        if previous.kind != kind:
            previous.category_buttons[previous.kind].setChecked(True)
            if previous.save():
                self.tabs.setCurrentWidget(self.notes if kind == 'note' else self.todos)

    def flush(self):
        notes_saved = self.notes.save()
        todos_saved = self.todos.save()
        return notes_saved and todos_saved
