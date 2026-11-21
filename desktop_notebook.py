"""The native notebook and its local SQLite storage."""
import sqlite3
from datetime import date, datetime, timezone

from PySide6.QtCore import QEvent, QMimeData, QRectF, QSignalBlocker, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QDrag, QPainter, QPainterPath, QPalette, QPen
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QMenu, QMessageBox, QPushButton, QStyle, QStyledItemDelegate, QTextEdit, QVBoxLayout, QWidget,
)

from desktop_glass import ClientSwitch, FrostedPanel
from desktop_theme import configure_text, ui_font
from model_groups import group_name
from notebook_store import NotebookStore

GROUP_NAMES = {'note': '拾得片羽', 'todo': '待理诸事', 'daily': '每日记录'}
INK = '#f3faff'
MUTED = '#a9c8d7'
CHECK_STATE = Qt.ItemDataRole.CheckStateRole
ENTRY_ID = Qt.ItemDataRole.UserRole
GROUP_HEADER = Qt.ItemDataRole.UserRole + 1


class EntryDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        return QSize(option.fontMetrics.horizontalAdvance('片') * 11, max(38, option.fontMetrics.height() + 14))

    def check_rect(self, rect):
        return QRectF(rect.x() + 9, rect.center().y() - 7, 16, 16)

    def paint(self, painter, option, index):
        rect = option.rect
        if index.data(GROUP_HEADER) is not None:
            painter.save()
            painter.setFont(ui_font(9, True))
            painter.setPen(QColor(MUTED))
            painter.drawText(rect.adjusted(9, 0, -9, 0), Qt.AlignmentFlag.AlignVCenter, index.data())
            painter.restore()
            return
        selected = option.state & QStyle.StateFlag.State_Selected
        hovered = option.state & QStyle.StateFlag.State_MouseOver
        check = index.data(CHECK_STATE)
        done = check == Qt.CheckState.Checked.value
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setClipRect(rect)
        if selected or hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(200, 232, 248, 53 if selected else 23))
            painter.drawRoundedRect(QRectF(rect).adjusted(.5, 1, -.5, -1), 8, 8)
        else:
            painter.setPen(QColor(210, 237, 250, 26))
            painter.drawLine(rect.left() + 9, rect.bottom(), rect.right() - 9, rect.bottom())
        if check is not None:
            box = self.check_rect(rect)
            painter.setPen(QPen(QColor('#a7cadc' if done else INK), 1.2))
            painter.setBrush(QColor('#a7cadc') if done else Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(box, 3.5, 3.5)
            if done:
                mark = QPainterPath()
                mark.moveTo(box.x() + 4, box.y() + 8)
                mark.lineTo(box.x() + 7, box.y() + 11)
                mark.lineTo(box.x() + 12, box.y() + 5)
                pen = QPen(QColor('#17445e'), 1.7)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                painter.setPen(pen)
                painter.drawPath(mark)
        text_rect = rect.adjusted(34 if check is not None else 10, 0, -10, 0)
        painter.setFont(option.font)
        painter.setPen(QColor(MUTED if done else INK))
        text = option.fontMetrics.elidedText(index.data() or '', Qt.TextElideMode.ElideRight, text_rect.width())
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
        painter.restore()

    def editorEvent(self, event, model, option, index):
        check = index.data(CHECK_STATE)
        if check is None:
            return False
        if event.type() == QEvent.Type.KeyPress:
            if event.key() != Qt.Key.Key_Space:
                return False
        elif event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
                             QEvent.Type.MouseButtonDblClick):
            if event.button() != Qt.MouseButton.LeftButton or not self.check_rect(option.rect).contains(event.position()):
                return False
            if event.type() != QEvent.Type.MouseButtonRelease:
                return True
        else:
            return False
        state = Qt.CheckState.Unchecked if check == Qt.CheckState.Checked.value else Qt.CheckState.Checked
        return model.setData(index, state.value, CHECK_STATE)


class GroupList(QListWidget):
    def __init__(self, drop_in_group):
        super().__init__()
        self.drop_in_group = drop_in_group
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)

    def startDrag(self, _actions):
        item = self.currentItem()
        if item is None or item.data(ENTRY_ID) is None:
            return
        mime = QMimeData()
        mime.setText(str(item.data(ENTRY_ID)))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.MoveAction)

    def dragEnterEvent(self, event):
        if event.source() is self:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        item = self.itemAt(event.position().toPoint())
        if event.source() is self and item is not None and item.data(GROUP_HEADER) is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        item = self.itemAt(event.position().toPoint())
        if event.source() is self and item is not None and item.data(GROUP_HEADER) is not None:
            self.drop_in_group(int(event.mimeData().text()), item.data(GROUP_HEADER))
            event.acceptProposedAction()
        else:
            event.ignore()


def notebook_button(text, name, size):
    button = QPushButton(text)
    button.setObjectName(name)
    button.setFont(ui_font(size))
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


class NotebookWorkspace(QWidget):
    def __init__(self, store, backdrop, organizer=None):
        super().__init__()
        self.store = store
        self.organizer = organizer
        self.kind = 'note'
        self.today = date.today()
        self.archived = False
        self.entry_id = None
        self.dirty = False
        self.edit_basis = None
        self.selections = {}
        self.records = {}
        self.items = {}
        self.draft = QTimer(self)
        self.draft.setSingleShot(True)
        self.draft.setInterval(450)
        self.draft.timeout.connect(self.flush)

        shell = QHBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(7)
        tint, edge = QColor(9, 45, 68, 172), QColor(221, 244, 255, 83)
        self.list_panel = FrostedPanel(backdrop, tint=tint, edge=edge)
        self.list_panel.setFixedWidth(266)
        navigation = QVBoxLayout(self.list_panel)
        navigation.setContentsMargins(13, 12, 13, 10)
        navigation.setSpacing(5)
        self.category_switch = ClientSwitch(
            backdrop, self.select_kind, compact=True, accessible_name='吉光片羽分类',
            segments=tuple((kind, name, 74) for kind, name in GROUP_NAMES.items()),
        )
        navigation.addWidget(self.category_switch, 0, Qt.AlignmentFlag.AlignHCenter)

        divider = QFrame()
        divider.setObjectName('notebookDivider')
        divider.setFixedHeight(1)
        navigation.addWidget(divider)
        navigation.addSpacing(3)
        self.empty = QLabel()
        self.empty.setObjectName('notebookMuted')
        self.empty.setFont(ui_font(9.5))
        self.empty.setContentsMargins(9, 5, 0, 0)
        navigation.addWidget(self.empty)
        self.list = GroupList(self.move_to_group)
        self.list.setObjectName('notebookList')
        self.list.setFont(ui_font(11))
        self.list.setMouseTracking(True)
        self.list.setUniformItemSizes(True)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.viewport().setAutoFillBackground(False)
        self.list.setItemDelegate(EntryDelegate(self.list))
        self.list.currentItemChanged.connect(self.select_entry)
        self.list.itemChanged.connect(self.update_check)
        navigation.addWidget(self.list, 1)
        shell.addWidget(self.list_panel)

        self.editor_panel = FrostedPanel(backdrop, tint=tint, edge=edge)
        fields = QVBoxLayout(self.editor_panel)
        fields.setContentsMargins(24, 13, 17, 16)
        fields.setSpacing(7)
        header = QHBoxLayout()
        self.heading = QLabel()
        self.heading.setObjectName('notebookHeading')
        self.heading.setFont(ui_font(15, True))
        header.addWidget(self.heading, 1)
        self.create = notebook_button('＋', 'notebookIcon', 14)
        self.create.setFixedSize(31, 32)
        self.create.clicked.connect(lambda: self.new_entry(self.kind))
        header.addWidget(self.create)
        self.more = notebook_button('⋯', 'notebookIcon', 17)
        self.more.setFixedSize(31, 32)
        self.more.setAccessibleName('更多操作')
        self.more.clicked.connect(self.open_menu)
        header.addWidget(self.more)
        fields.addLayout(header)
        self.stamp = QLabel()
        self.stamp.setObjectName('notebookMuted')
        self.stamp.setFont(ui_font(10))
        self.stamp.setFixedHeight(self.stamp.fontMetrics().height())
        fields.addWidget(self.stamp)
        fields.addSpacing(3)
        self.body = QTextEdit()
        self.body.setObjectName('notebookBody')
        self.body.setAcceptRichText(False)
        configure_text(self.body, 12, 0)
        colors = self.body.palette()
        colors.setColor(QPalette.ColorRole.PlaceholderText, QColor(MUTED))
        self.body.setPalette(colors)
        self.body.viewport().setAutoFillBackground(False)
        self.body.textChanged.connect(self.edited)
        fields.addWidget(self.body, 1)
        self.saved_status = QLabel()
        self.saved_status.setObjectName('notebookMuted')
        self.saved_status.setFont(ui_font(9.5))
        self.saved_status.setFixedHeight(self.saved_status.fontMetrics().height())
        fields.addWidget(self.saved_status)
        shell.addWidget(self.editor_panel, 1)
        self.load_group('note')

    def select_kind(self, kind):
        if kind == self.kind and not self.archived:
            self.category_switch.select(kind)
            return
        if self.flush():
            self.load_group(kind)
        else:
            self.category_switch.select(self.kind)

    def load_group(self, kind, archived=False):
        self.kind, self.archived = kind, archived
        self.heading.setText(GROUP_NAMES[kind])
        self.list.setAccessibleName(GROUP_NAMES[kind])
        self.body.setAccessibleName(GROUP_NAMES[kind] + '正文')
        self.body.setPlaceholderText({'note': '有念，便落一笔。', 'todo': '把要做的事写在这里。', 'daily': ''}[kind])
        daily = kind == 'daily'
        self.category_switch.select(kind)
        self.list.setDragEnabled(not daily)
        self.list.setAcceptDrops(not daily)
        self.create.setVisible(not daily)
        self.more.setVisible(not daily)
        self.create.setToolTip('落一笔' if kind == 'note' else '记一件待理之事')
        self.create.setAccessibleName(self.create.toolTip())
        self.entry_id = None
        self.load_list()
        remembered = self.selections.get((kind, archived))
        self.set_entry(remembered if remembered in self.records else next(iter(self.records), None))

    def load_list(self):
        if self.kind == 'daily':
            self.load_daily_list()
            return
        rows = self.store.entries(self.kind, self.archived, today=self.today.isoformat())
        self.records = {row[0]: row[1:] for row in rows}
        self.items = {}
        scrollbar = self.list.verticalScrollBar()
        scroll = scrollbar.value()
        grouped = {}
        manual_groups = self.store.groups(self.kind)
        for row in rows:
            entry_id, title, body, _filed, _updated_at = row
            if self.organizer is not None and not self.archived and entry_id not in manual_groups:
                self.organizer.observe(self.kind, entry_id, title, body)
            name = manual_groups.get(entry_id)
            if name is None:
                name = self.organizer.label(self.kind, entry_id) if self.organizer is not None else ''
            grouped.setdefault(name, []).append(row)
        has_groups = any(grouped)
        with QSignalBlocker(self.list):
            self.list.clear()
            for name, entries in grouped.items():
                if has_groups:
                    header = QListWidgetItem(name or '未归组', self.list)
                    header.setData(GROUP_HEADER, name)
                    header.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsDropEnabled)
                    header.setToolTip('可拖动条目到此组')
                for entry_id, title, body, filed, _updated_at in entries:
                    line = next((part.strip() for part in (title or body).splitlines() if part.strip()), '一笔未题')
                    item = QListWidgetItem(line, self.list)
                    item.setData(ENTRY_ID, entry_id)
                    if self.kind == 'todo':
                        item.setCheckState(Qt.CheckState.Checked if filed else Qt.CheckState.Unchecked)
                    self.items[entry_id] = item
            self.list.setCurrentItem(self.items.get(self.entry_id))
        scrollbar.setValue(scroll)
        self.empty.setText('旧笺尚空。' if self.archived else '这一页尚空。')
        self.empty.setVisible(not rows)

    def load_daily_list(self):
        days = self.store.daily_dates()
        self.records = dict.fromkeys(days)
        self.items = {}
        scrollbar = self.list.verticalScrollBar()
        scroll = scrollbar.value()
        with QSignalBlocker(self.list):
            self.list.clear()
            for day in days:
                item = QListWidgetItem(day, self.list)
                item.setData(ENTRY_ID, day)
                self.items[day] = item
            self.list.setCurrentItem(self.items.get(self.entry_id))
        scrollbar.setValue(scroll)
        self.empty.setText('暂无每日记录')
        self.empty.setVisible(not days)

    def move_to_group(self, entry_id, name):
        if entry_id not in self.records:
            return
        try:
            self.store.assign(self.kind, entry_id, group_name(name))
            self.load_list()
        except (OSError, sqlite3.Error):
            QMessageBox.warning(self, '未能移入', '暂时无法保存分组，请稍后再试。')

    def set_entry(self, entry_id):
        self.entry_id = entry_id
        self.selections[(self.kind, self.archived)] = entry_id
        if self.kind == 'daily':
            self.show_daily_entry(entry_id)
            return
        title, body, _filed, _updated_at = self.records.get(entry_id, ('', '', False, ''))
        self.edit_basis = (title, body) if entry_id is not None else None
        if title:
            body = title + ('\n\n' + body if body else '')
        with QSignalBlocker(self.body), QSignalBlocker(self.list):
            self.body.setPlainText(body)
            self.list.setCurrentItem(self.items.get(entry_id))
        self.body.setReadOnly(self.archived and entry_id is None)
        self.dirty = False
        self.update_stamp()

    def show_daily_entry(self, day):
        sections = {'note': [], 'todo': []}
        if day is not None:
            for kind, title, body, filed, _updated, _group in self.store.daily_entries(day):
                text = '\n\n'.join(part for part in (title, body) if part) or '一笔未题'
                if kind == 'todo':
                    text = ('已完成 · ' if filed else '待完成 · ') + text
                sections[kind].append(text)
        text = '\n\n'.join(GROUP_NAMES[kind] + '\n\n' + '\n\n'.join(entries)
                           for kind, entries in sections.items() if entries)
        self.heading.setText(day or GROUP_NAMES['daily'])
        with QSignalBlocker(self.body), QSignalBlocker(self.list):
            self.body.setReadOnly(True)
            if text != self.body.toPlainText():
                self.body.setPlainText(text)
            self.list.setCurrentItem(self.items.get(day))
        self.stamp.clear()
        self.saved_status.clear()

    def update_stamp(self):
        record = self.records.get(self.entry_id)
        self.more.setEnabled(self.kind == 'note' or record is not None)
        if record is None:
            self.stamp.clear()
            self.saved_status.clear()
            return
        updated = datetime.fromisoformat(record[3]).replace(tzinfo=timezone.utc).astimezone()
        today = datetime.now().date()
        date = ('今夜' if updated.hour >= 18 else '今日') if updated.date() == today else updated.strftime('%m月%d日')
        self.stamp.setText(f'{date} · {updated:%H:%M}')
        self.saved_status.setText('已收于此')

    def edited(self):
        self.dirty = True
        self.saved_status.setText('正在记下…')
        self.draft.start()

    def refresh_synced(self):
        if self.dirty:
            return False
        previous = self.entry_id
        self.load_list()
        if previous is not None or self.kind == 'daily':
            self.set_entry(previous if previous in self.records else next(iter(self.records), None))
        return True

    def refresh_day(self):
        today = date.today()
        if today != self.today and not self.dirty:
            self.today = today
            self.refresh_synced()

    def flush(self):
        self.draft.stop()
        if not self.dirty:
            return True
        body = self.body.toPlainText()
        if self.entry_id is None and not body.strip():
            self.saved_status.clear()
            self.dirty = False
            return True
        try:
            self.entry_id = self.store.save(self.kind, self.entry_id, body, expected=self.edit_basis)
        except (OSError, sqlite3.Error):
            self.saved_status.setText('尚未存下')
            QMessageBox.warning(self, '尚未存下', '暂时无法写入本机，文字还留在此处。')
            return False
        self.dirty = False
        self.selections[(self.kind, self.archived)] = self.entry_id
        self.load_list()
        self.edit_basis = self.records.get(self.entry_id, ())[:2]
        self.update_stamp()
        return True

    def select_entry(self, current, previous):
        entry_id = current.data(ENTRY_ID) if current is not None else None
        if self.flush():
            self.set_entry(entry_id)
        else:
            with QSignalBlocker(self.list):
                self.list.setCurrentItem(previous)

    def new_entry(self, kind):
        if not self.flush():
            return
        if kind != self.kind or self.archived:
            self.load_group(kind)
        self.set_entry(None)
        self.body.setFocus()

    def set_filed(self, entry_id, filed):
        try:
            updated = self.store.set_filed(self.kind, entry_id, filed)
        except (OSError, sqlite3.Error):
            QMessageBox.warning(self, '尚未记下', '暂时无法更新这条记录。')
            return False
        title, body, _filed, updated_at = self.records[entry_id]
        self.records[entry_id] = (title, body, filed, updated or updated_at)
        if entry_id == self.entry_id:
            self.update_stamp()
        return True

    def update_check(self, item):
        if self.kind != 'todo' or item.data(ENTRY_ID) is None:
            return
        entry_id = item.data(ENTRY_ID)
        filed = item.checkState() == Qt.CheckState.Checked
        if not self.set_filed(entry_id, filed):
            with QSignalBlocker(self.list):
                item.setCheckState(Qt.CheckState.Unchecked if filed else Qt.CheckState.Checked)

    def file_note(self):
        if self.flush() and self.entry_id is not None and self.set_filed(self.entry_id, not self.archived):
            self.load_group('note', self.archived)

    def show_archive(self):
        if self.flush():
            self.load_group('note', not self.archived)

    def open_menu(self):
        menu = QMenu(self)
        menu.setObjectName('notebookMenu')
        if self.kind == 'note':
            move = menu.addAction('重拾此笺' if self.archived else '收入旧笺', self.file_note)
            move.setEnabled(self.entry_id is not None)
            menu.addAction('返回拾得片羽' if self.archived else '翻看旧笺', self.show_archive)
        elif self.entry_id is not None:
            item = self.items[self.entry_id]
            checked = item.checkState() == Qt.CheckState.Checked
            menu.addAction('重开此事' if checked else '勾记此事',
                           lambda: item.setCheckState(Qt.CheckState.Unchecked if checked else Qt.CheckState.Checked))
        if not menu.isEmpty():
            menu.exec(self.more.mapToGlobal(self.more.rect().bottomLeft()))
