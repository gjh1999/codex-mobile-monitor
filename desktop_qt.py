"""Native task workspace: light cards, soft-blue actions and explicit failures."""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QSharedMemory, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QDialog, QFrame, QGraphicsDropShadowEffect, QGraphicsOpacityEffect, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMenu, QMessageBox, QPushButton, QSizePolicy, QStackedWidget, QStyle, QSystemTrayIcon,
    QTextBrowser, QTextEdit, QVBoxLayout,
)

import monitor
from desktop_glass import ClientSwitch, FrostedPanel, OceanBackdrop, motion_enabled
from desktop_theme import STYLESHEET, configure_text, message_html, set_reading_html, ui_font, warning_html

MUTED = '#24495f'
WINDOW_TITLE = '澜汐'
TASK_TITLE_ROLE = Qt.ItemDataRole.UserRole + 1
UUID_PATTERN = re.compile(r'\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b')


def display_text(value):
    """Identifiers remain in data, not in task captions or operation notices."""
    return UUID_PATTERN.sub('此任务', str(value or ''))


def label(text, size=13, bold=False, color=None):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setFont(ui_font(size, bold))
    widget.setWordWrap(True)
    if color:
        widget.setStyleSheet(f'color:{color};')
    return widget


def vscode_executable():
    found = shutil.which('Code.exe')
    if found:
        return Path(found)
    launcher = shutil.which('code.cmd')
    candidates = [Path(launcher).parent.parent / 'Code.exe'] if launcher else []
    for root in ('LOCALAPPDATA', 'ProgramFiles', 'ProgramFiles(x86)'):
        if os.environ.get(root):
            directory = Path(os.environ[root])
            if root == 'LOCALAPPDATA':
                directory /= 'Programs'
            candidates.append(directory / 'Microsoft VS Code' / 'Code.exe')
    return next((candidate for candidate in candidates if candidate.is_file()), None)


class TaskTitle(QLabel):
    edit_requested = Signal()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.edit_requested.emit()
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)


class ActionHeader(QFrame):
    """Reveal actions without moving the conversation or losing keyboard access."""
    def __init__(self):
        super().__init__()
        self.actions = QFrame()
        self.opacity = QGraphicsOpacityEffect(self.actions)
        self.opacity.setOpacity(0.0)
        self.actions.setGraphicsEffect(self.opacity)
        self.animation = QPropertyAnimation(self.opacity, b'opacity', self)
        self.animation.setDuration(160)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.target = 0.0
        self.hovered = False
        QApplication.instance().focusChanged.connect(self.sync_actions)

    def sync_actions(self, *_):
        focus = QApplication.focusWidget()
        value = float(self.hovered or (focus is not None and self.actions.isAncestorOf(focus)))
        if value == self.target:
            return
        self.target = value
        self.animation.stop()
        if motion_enabled():
            self.animation.setStartValue(self.opacity.opacity())
            self.animation.setEndValue(value)
            self.animation.start()
        else:
            self.opacity.setOpacity(value)

    def enterEvent(self, event):
        super().enterEvent(event)
        self.hovered = True
        self.sync_actions()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self.hovered = False
        self.sync_actions()


class ActionButton(QPushButton):
    """160ms hover/press elevation, without moving or resizing the hit target."""
    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setObjectName('taskAction')
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.effect = QGraphicsDropShadowEffect(self)
        self.effect.setColor(QColor(43, 99, 198, 26))
        self.effect.setOffset(0, 1)
        self.effect.setBlurRadius(0)
        self.setGraphicsEffect(self.effect)
        self.animation = QPropertyAnimation(self.effect, b'blurRadius', self)
        self.animation.setDuration(160)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)

    def animate_to(self, radius):
        self.animation.stop()
        if not motion_enabled():
            self.effect.setBlurRadius(radius if self.isEnabled() else 0)
            return
        self.animation.setStartValue(self.effect.blurRadius())
        self.animation.setEndValue(radius if self.isEnabled() else 0)
        self.animation.start()

    def enterEvent(self, event):
        self.animate_to(12)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.animate_to(0)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        self.animate_to(2)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        self.animate_to(12 if self.underMouse() else 0)
        super().mouseReleaseEvent(event)


class Composer(QTextEdit):
    send_requested = Signal()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.send_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class MainWindow(QMainWindow):
    def __init__(self, shared):
        super().__init__()
        QApplication.instance().setStyle('Fusion')
        self.setFont(ui_font(10))
        self.shared = shared
        self.stop = threading.Event()
        self.worker = None
        self._quitting = False
        self._shutdown_complete = False
        self.client = 'codex'
        self.selected_thread = ''
        self.client_selections = {'codex': '', 'claude': ''}
        self.task_drafts = {}
        self._pending_operations = {}
        self._started_at = time.time()
        self._seen_operation_states = {'codex': {}, 'claude': {}}
        self._error_events = {}
        self._dialogs = []
        self._task_signature = None
        self._pending_approvals = {}
        self._shown_approvals = set()
        self.approval_dialog = None
        self._review_token = None
        self._rename_target = None
        self._project_directory = None
        self._conversation_signature = None
        self.setWindowTitle(WINDOW_TITLE)
        icon_path = Path(getattr(sys, '_MEIPASS', monitor.ROOT)) / 'app.ico'
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
        self.setMinimumSize(1020, 640)
        screen = QApplication.primaryScreen()
        available = screen.availableGeometry() if screen else None
        self.resize(min(1260, max(1020, available.width() - 40)) if available else 1260,
                    min(840, max(640, available.height() - 50)) if available else 840)
        self.build_ui()
        self.build_tray()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        _stop, self.worker = monitor.start_background_service(self.stop)
        self.refresh()

    def build_ui(self):
        self.setStyleSheet(STYLESHEET)
        try:
            minutes = monitor.config().get('desktopWallpaperMinutes', 30)
        except (OSError, ValueError, AttributeError):
            minutes = 30
        root = OceanBackdrop(Path(getattr(sys, '_MEIPASS', monitor.ROOT)), minutes)
        self.setCentralWidget(root)
        page = QVBoxLayout(root)
        page.setContentsMargins(24, 18, 24, 24)
        page.setSpacing(17)

        self.client_switch = ClientSwitch(self.switch_client)
        self.client_buttons = self.client_switch.buttons
        page.addWidget(self.client_switch, 0, Qt.AlignmentFlag.AlignHCenter)

        heading = QHBoxLayout()
        heading.setContentsMargins(12, 14, 4, 8)
        title = QVBoxLayout()
        title.setSpacing(16)
        verse = label('潮平两岸阔，风正一帆悬。', 31, color='#ffffff')
        verse.setFont(QFont('SimSun', 31))
        verse.setWordWrap(False)
        title.addWidget(verse)
        title.addWidget(label('一处开始，处处继续。百日写的那一行，暮归时仍等着你。', 10, color='#effaff'))
        heading.addLayout(title, 1)

        summary = QFrame()
        summary.setObjectName('oceanStats')
        counts = QGridLayout(summary)
        counts.setContentsMargins(11, 10, 11, 10)
        counts.setHorizontalSpacing(10)
        counts.setVerticalSpacing(3)
        self.stat_total, self.stat_running, self.stat_pending = (label('0', 16, True, '#f3fbff') for _ in range(3))
        for column, (value, caption, meaning) in enumerate((
            (self.stat_total, '案头', '任务总数'), (self.stat_running, '行舟', '运行中的任务'),
            (self.stat_pending, '待你定夺', '待审批的请求'),
        )):
            caption_label = label(caption, 9, color='#d5edf9')
            caption_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            caption_label.setToolTip(meaning)
            caption_label.setAccessibleName(meaning)
            counts.addWidget(caption_label, 0, column)
            value.setAlignment(Qt.AlignmentFlag.AlignCenter)
            value.setAccessibleName(meaning)
            counts.addWidget(value, 1, column)
        heading.addWidget(summary, 0, Qt.AlignmentFlag.AlignBottom)
        page.addLayout(heading)

        middle = QHBoxLayout()
        middle.setSpacing(7)
        self.task_panel = FrostedPanel(root)
        self.task_panel.setFixedWidth(266)
        side = QVBoxLayout(self.task_panel)
        side.setContentsMargins(13, 13, 13, 10)
        side.setSpacing(8)
        task_header = QHBoxLayout()
        task_header.addWidget(label('案头诸事', 12, True), 1)
        create = ActionButton('＋')
        create.setToolTip('创建测试任务')
        create.setAccessibleName('创建测试任务')
        create.clicked.connect(lambda: self.operate('create_test'))
        task_header.addWidget(create)
        side.addLayout(task_header)
        self.task_search = QLineEdit()
        self.task_search.setAccessibleName('搜索任务')
        self.task_search.setPlaceholderText('寻一件案头事')
        self.task_search.textChanged.connect(self.filter_tasks)
        side.addWidget(self.task_search)
        self.task_choice = QListWidget()
        self.task_choice.setObjectName('taskList')
        self.task_choice.setAccessibleName('任务列表')
        self.task_choice.setSpacing(3)
        self.task_choice.setWordWrap(True)
        self.task_choice.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.task_choice.currentItemChanged.connect(self.select_task)
        self.task_choice.itemClicked.connect(self.focus_task)
        self.task_choice.itemActivated.connect(self.focus_task)
        self.task_choice.viewport().setAutoFillBackground(False)
        side.addWidget(self.task_choice, 1)
        self.task_empty = label('', 9, color=MUTED)
        self.task_empty.hide()
        side.addWidget(self.task_empty)
        self.live = label('○ 正在连接', 9, color=MUTED)
        self.live.setObjectName('status')
        side.addWidget(self.live)
        middle.addWidget(self.task_panel)

        self.conversation_card = FrostedPanel(root)
        conversation_layout = QVBoxLayout(self.conversation_card)
        conversation_layout.setContentsMargins(15, 12, 15, 12)
        conversation_layout.setSpacing(8)
        self.conversation_header = ActionHeader()
        conversation_header = QHBoxLayout(self.conversation_header)
        conversation_header.setContentsMargins(0, 0, 0, 0)
        conversation_title = QVBoxLayout()
        conversation_title.setSpacing(3)
        conversation_title.addWidget(label('灯下对谈', 9, color=MUTED))
        self.title_stack = QStackedWidget()
        self.title_stack.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.conversation_heading = TaskTitle('未尽的话，可以接着说。')
        self.conversation_heading.setFont(ui_font(13, True))
        self.conversation_heading.setTextFormat(Qt.TextFormat.PlainText)
        self.conversation_heading.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.conversation_heading.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.conversation_heading.setWordWrap(False)
        self.conversation_heading.edit_requested.connect(self.begin_rename)
        QShortcut(QKeySequence('F2'), self.conversation_heading, self.begin_rename,
                  context=Qt.ShortcutContext.WidgetShortcut)
        self.title_editor = QLineEdit()
        self.title_editor.setMaxLength(80)
        self.title_editor.setAccessibleName('任务名称')
        self.title_editor.returnPressed.connect(self.save_rename)
        QShortcut(QKeySequence('Escape'), self.title_editor, self.cancel_rename,
                  context=Qt.ShortcutContext.WidgetShortcut)
        self.title_stack.addWidget(self.conversation_heading)
        self.title_stack.addWidget(self.title_editor)
        conversation_title.addWidget(self.title_stack)
        conversation_header.addLayout(conversation_title, 1)
        self.build_task_actions()
        conversation_header.addWidget(self.conversation_header.actions)
        conversation_layout.addWidget(self.conversation_header)
        divider = QFrame()
        divider.setObjectName('divider')
        divider.setFixedHeight(1)
        conversation_layout.addWidget(divider)

        self.conversation = QTextBrowser()
        self.conversation.setObjectName('conversation')
        configure_text(self.conversation, 10, 3)
        self.conversation.setMinimumHeight(130)
        self.conversation.setOpenExternalLinks(False)
        self.conversation.viewport().setAutoFillBackground(False)
        conversation_layout.addWidget(self.conversation, 1)

        self.review_button = ActionButton('查看待审批请求')
        self.review_button.clicked.connect(lambda: self.offer_approval(force=True))
        self.review_button.hide()
        conversation_layout.addWidget(self.review_button, 0, Qt.AlignmentFlag.AlignLeft)
        composer_box = QFrame()
        composer_box.setObjectName('composerBox')
        composer_layout = QVBoxLayout(composer_box)
        composer_layout.setContentsMargins(10, 6, 10, 7)
        composer_layout.setSpacing(3)
        self.message_edit = Composer()
        self.message_edit.setObjectName('composer')
        configure_text(self.message_edit, 10, 3)
        self.message_edit.viewport().setAutoFillBackground(False)
        self.message_edit.setFixedHeight(57)
        self.message_edit.setPlaceholderText('把下一步，写在这里…')
        self.message_edit.setAccessibleName('任务指令')
        self.message_edit.setAcceptRichText(False)
        self.message_edit.send_requested.connect(lambda: self.operate('message') if self.send_button.isEnabled() else None)
        composer_layout.addWidget(self.message_edit)
        send_row = QHBoxLayout()
        self.message_hint = label('', 8.5, color=MUTED)
        send_row.addWidget(self.message_hint, 1)
        self.send_button = ActionButton('↑')
        self.send_button.setObjectName('send')
        self.send_button.setFixedSize(34, 34)
        self.send_button.setToolTip('发送指令 · Ctrl+Enter')
        self.send_button.setAccessibleName('发送指令')
        self.send_button.clicked.connect(lambda: self.operate('message'))
        send_row.addWidget(self.send_button)
        composer_layout.addLayout(send_row)
        conversation_layout.addWidget(composer_box)
        self.control_message = label('', 8.5, color=MUTED)
        conversation_layout.addWidget(self.control_message)
        middle.addWidget(self.conversation_card, 1)
        page.addLayout(middle, 1)

        self.details_dialog = QDialog(self)
        self.details_dialog.setWindowTitle('运行与额度')
        self.details_dialog.resize(520, 380)
        details = QVBoxLayout(self.details_dialog)
        for title, attr in (('连接与自动推进', 'guard_text'), ('账户额度', 'quota_text')):
            details.addWidget(label(title, 11, True))
            content = QTextBrowser()
            configure_text(content, 10, 4)
            setattr(self, attr, content)
            details.addWidget(content, 1)

    def build_task_actions(self):
        self.action_buttons = {}
        actions = QHBoxLayout(self.conversation_header.actions)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(5)
        for action, text, meaning in (
            ('attach', '执舟', '接入任务'), ('pause', '暂泊', '暂停任务'),
            ('resume', '续航', '继续任务'), ('manage', '扬帆', '开启自动推进'),
            ('unmanage', '收帆', '关闭自动推进'), ('detach', '移离案头', '移出任务'),
            ('open_project', '启卷', '在 VS Code 打开任务项目'),
        ):
            button = QPushButton(text)
            button.setObjectName('hoverAction')
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            button.setFont(ui_font(9))
            button.setToolTip(meaning)
            button.setAccessibleName(meaning)
            if action == 'open_project':
                button.clicked.connect(self.open_project)
            elif action == 'attach':
                button.clicked.connect(lambda: self.focus_task(self.task_choice.currentItem()))
            else:
                button.clicked.connect(lambda _=False, action=action: self.operate(action))
            self.action_buttons[action] = button
            actions.addWidget(button)

    def build_tray(self):
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(self.windowIcon() if not self.windowIcon().isNull() else self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))
        self.tray.setToolTip(WINDOW_TITLE)
        menu = QMenu(self)
        menu.addAction('打开澜汐', self.show_window)
        menu.addAction('运行与额度', self.show_details)
        menu.addAction('查看待审批请求', lambda: self.offer_approval(force=True))
        menu.addSeparator()
        menu.addAction('退出程序', self.quit_app)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.show_window() if reason in (
            QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick) else None)
        self.tray.show()

    def show_window(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def quit_app(self):
        self._quitting = True
        self.close()
        # Closing an already-hidden window does not emit lastWindowClosed.
        # Explicitly end the application loop after cleanup in both tray cases.
        QApplication.instance().quit()

    def shutdown(self):
        """Idempotent cleanup, including quit paths that bypass closeEvent."""
        if self._shutdown_complete:
            return
        self._shutdown_complete = True
        self._quitting = True
        self.stop.set()
        self.timer.stop()
        if self.worker:
            self.worker.join(timeout=8)
        self.tray.hide()

    def show_details(self):
        self.refresh()
        self.details_dialog.show()
        self.details_dialog.raise_()

    def select_task(self, current, _previous):
        self.change_task(self.client, current.data(Qt.ItemDataRole.UserRole) if current else '')
        self.refresh()

    def change_task(self, client, tid):
        previous = (self.client, self.selected_thread)
        selected = (client, tid)
        if selected != previous:
            self.task_drafts[previous] = self.message_edit.toPlainText()
            self.client, self.selected_thread = selected
            self.message_edit.setPlainText(self.task_drafts.get(selected, ''))
            self._conversation_signature = None

    def focus_task(self, item):
        if item is None:
            return
        tid = item.data(Qt.ItemDataRole.UserRole)
        last = next((operation for (client, _), operation in reversed(self._pending_operations.items())
                     if client == self.client and operation['action'] == 'focus'), None)
        if not last or last['threadId'] != tid:
            self.operate('focus', tid=tid)

    def task_title(self):
        item = self.task_choice.currentItem()
        return item.data(TASK_TITLE_ROLE) if item is not None else ''

    def filter_tasks(self):
        query = self.task_search.text().strip().casefold()
        matches = 0
        for row in range(self.task_choice.count()):
            item = self.task_choice.item(row)
            visible = query in item.data(TASK_TITLE_ROLE).casefold()
            item.setHidden(not visible)
            matches += int(visible)
        self.task_empty.setText('案头未见这一件，换个词再寻。' if query else '案头暂空，待写新篇。')
        self.task_empty.setVisible(matches == 0)

    def render_tasks(self, titles, threads, controls):
        thread_status = {task.get('id'): (task.get('status') or {}).get('type') for task in threads}
        active_controls = {task.get('threadId'): task for task in controls}
        rows = []
        for tid, title in titles:
            control = active_controls.get(tid) or {}
            status = (control.get('threadStatus') or {}).get('type') or thread_status.get(tid)
            caption = '暂泊' if control.get('paused') else {
                'active': '行舟中', 'idle': '闲泊', 'systemError': '执行异常',
            }.get(status, '待接入')
            rows.append((tid, title, caption))
        if rows != self._task_signature:
            previous_scroll = self.task_choice.verticalScrollBar().value()
            self._task_signature = rows
            with QSignalBlocker(self.task_choice):
                self.task_choice.clear()
                for tid, title, caption in rows:
                    item = QListWidgetItem(f'{title}\n{caption}', self.task_choice)
                    item.setData(Qt.ItemDataRole.UserRole, tid)
                    item.setData(TASK_TITLE_ROLE, title)
                    item.setToolTip(f'{title}\n{caption}')
                self.filter_tasks()
            self.task_choice.verticalScrollBar().setValue(previous_scroll)
        index = next((i for i, row in enumerate(rows) if row[0] == self.selected_thread), 0 if rows else -1)
        if self.task_choice.currentRow() != index:
            with QSignalBlocker(self.task_choice):
                self.task_choice.setCurrentRow(index)
        item = self.task_choice.currentItem()
        self.change_task(self.client, item.data(Qt.ItemDataRole.UserRole) if item is not None else '')

    def all_tasks(self, snap, client=None):
        client = client or self.client
        controls = snap.get('controls') or {}
        names = controls.get('names') or {}
        labels = (getattr(monitor, 'history_labels', {}) or {}) if client == 'codex' else {}
        result = {}
        for task in snap.get('threads') or []:
            tid = task.get('id')
            result[tid] = names.get(tid) or task.get('title') or task.get('name') or labels.get(tid, {}).get('title') or '未命名任务'
        for task in controls.get('tasks') or []:
            tid = task.get('threadId')
            if tid not in result or tid in names:
                result[tid] = names.get(tid) or task.get('title') or '未命名任务'
        return [(tid, display_text(title)) for tid, title in result.items() if tid]

    def switch_client(self, client):
        if client == self.client:
            self.client_buttons[client].setChecked(True)
            return
        self.client_selections[self.client] = self.selected_thread
        self.change_task(client, self.client_selections[client])
        self.client_switch.select(client)
        self.task_search.clear()
        self.control_message.setText('已切换到 ' + ('Claude Code' if client == 'claude' else 'Codex'))
        self.refresh()

    def refresh(self):
        snap = monitor.client_snapshot(monitor.snapshot(), self.client)
        connected = snap.get('connection') == 'connected'
        self.live.setText('● 已连接' if connected else '○ 连接待检查')
        if self.live.property('connected') != str(connected).lower():
            self.live.setProperty('connected', str(connected).lower())
            self.live.style().unpolish(self.live)
            self.live.style().polish(self.live)
        threads = snap.get('threads') or []
        controls = (snap.get('controls') or {}).get('tasks') or []
        titles = self.all_tasks(snap)
        running = {task.get('id') for task in threads if (task.get('status') or {}).get('type') == 'active'}
        for task in controls:
            tid = task.get('threadId')
            if task.get('paused'):
                running.discard(tid)
            elif (task.get('threadStatus') or {}).get('type') == 'active':
                running.add(tid)
        self.stat_total.setText(str(len(titles)))
        self.stat_running.setText(str(len(running)))
        self.render_tasks(titles, threads, controls)
        if self._rename_target is not None and self._rename_target != (self.client, self.selected_thread):
            self.cancel_rename()
        task_title = self.task_title() or '未尽的话，可以接着说。'
        self.conversation_heading.setText(task_title)
        self.conversation_heading.setToolTip(f'{task_title}\n双击标题或按 F2 改名' if self.selected_thread else '')
        self.conversation_heading.setAccessibleDescription('双击标题或按 F2 改名' if self.selected_thread else '')
        directory = next((entry['cwd'] for entries, key in ((threads, 'id'), (controls, 'threadId'))
                          for entry in entries if entry.get(key) == self.selected_thread and entry.get('cwd')), None)
        self._project_directory = Path(directory) if isinstance(directory, str) and Path(directory).is_absolute() else None
        self.render_controls(snap)
        self.render_guard_quota(snap)
        self.refresh_conversation()
        self.refresh_approvals()

    def begin_rename(self):
        if not self.selected_thread:
            return
        self._rename_target = (self.client, self.selected_thread)
        self.title_editor.setText(self.task_title())
        self.title_stack.setCurrentWidget(self.title_editor)
        self.title_editor.setFocus()
        self.title_editor.selectAll()

    def cancel_rename(self):
        had_focus = self.title_editor.hasFocus()
        self._rename_target = None
        self.title_stack.setCurrentWidget(self.conversation_heading)
        if had_focus:
            self.conversation_heading.setFocus()

    def save_rename(self):
        target = self._rename_target
        title = self.title_editor.text().strip()
        if not title or target != (self.client, self.selected_thread):
            return
        self.cancel_rename()
        if title != self.task_title():
            self.operate('rename', title, tid=target[1])

    def open_project(self):
        directory = self._project_directory
        if directory is None or not directory.is_dir():
            self._popup('无法启卷', '当前任务的项目目录在本机不可用。')
            return
        editor = vscode_executable()
        if editor is None:
            self._popup('无法启卷', '本机未找到 VS Code。')
            return
        try:
            subprocess.Popen([str(editor), str(directory)])
        except OSError as error:
            self._popup('无法启卷', f'VS Code 启动失败。{error}')

    def _popup(self, title, message):
        QTimer.singleShot(0, lambda title=title, message=message: self._popup_now(title, message))

    def _popup_now(self, title, message):
        box = QMessageBox(self)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(title)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(display_text(message))
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.button(QMessageBox.StandardButton.Ok).setText('知道了')
        self._dialogs.append(box)
        box.finished.connect(lambda _result: self._dialogs.remove(box) if box in self._dialogs else None)
        box.open()

    def operate(self, action, text_override=None, tid=None):
        target = self.selected_thread if tid is None else tid
        if action != 'create_test' and not target:
            self._popup('无法操作', '请先选择一个任务。')
            return
        text = self.message_edit.toPlainText() if action == 'message' else (text_override or '')
        if action == 'message' and not text.strip():
            self._popup('消息未发送', '请输入要发送的内容。')
            return
        controller = monitor.get_controller(self.client)
        if controller is None:
            self._popup('操作失败', '后台仍在启动，请稍候重试。')
            return
        try:
            operation = controller.submit(target, action, text, str(uuid.uuid4()))
            self._pending_operations[(self.client, operation)] = {'action': action, 'threadId': target, 'text': text}
            if action == 'message':
                self.message_edit.clear()
            self.control_message.setText({
                'pause': '正在请求暂停当前执行…', 'rename': '正在保存任务名称…', 'focus': '正在接入选中的任务…',
            }.get(action, '已提交，等待确认'))
        except Exception as error:
            self._popup('操作失败', str(error))

    def check_operation_results(self, controls):
        if controls.get('ready') is not True:
            return  # The worker has not loaded persistent history yet.
        states = self._seen_operation_states[self.client]
        for operation in reversed(controls.get('operations') or []):
            key, status = operation['id'], operation.get('status')
            previous = states.get(key)
            own = self._pending_operations.get((self.client, key))
            fresh = operation.get('stamp', 0) >= self._started_at
            if own and own['action'] == 'message' and status in ('failed', 'unknown', 'cancelled'):
                if own['threadId'] == self.selected_thread and not self.message_edit.toPlainText():
                    self.message_edit.setPlainText(own['text'])
                elif own['threadId'] != self.selected_thread:
                    key = (self.client, own['threadId'])
                    if not self.task_drafts.get(key):
                        self.task_drafts[key] = own['text']
            if status in ('failed', 'unknown') and previous != status and (own or fresh):
                self._popup('消息发送失败' if operation.get('action') == 'message' else '操作未完成', operation.get('message') or '请检查当前任务状态。')
            if own and status in ('succeeded', 'cancelled', 'failed', 'unknown'):
                self.control_message.setText(display_text(operation.get('message') or '操作已处理'))
                self._pending_operations.pop((self.client, key), None)
            states[key] = status

    def render_controls(self, snap):
        controls = snap.get('controls') or {}
        tasks = controls.get('tasks') or []
        selected = next((task for task in tasks if task.get('threadId') == self.selected_thread), None)
        quota_wait = (snap.get('quotaWaits') or {}).get(self.selected_thread) if self.client == 'codex' else None
        if not selected or selected.get('paused') or not selected.get('managed'):
            quota_wait = None
        waiting_quota = bool(quota_wait and quota_wait.get('state') in ('waiting', 'checking', 'reserved'))
        controlled = bool(selected)
        paused = controlled and bool(selected.get('paused'))
        manageable = controlled and bool(selected.get('canControl'))
        active = controlled and (selected.get('threadStatus') or {}).get('type') == 'active'
        managed = controlled and bool(selected.get('managed'))
        allowed = manageable and not paused and not waiting_quota
        buttons = self.action_buttons
        buttons['manage'].setVisible(not managed)
        buttons['unmanage'].setVisible(managed)
        buttons['attach'].setEnabled(bool(self.selected_thread))
        buttons['pause'].setEnabled(controlled and not paused)
        buttons['resume'].setEnabled(manageable and paused and (self.client == 'codex' or not active))
        buttons['resume'].setToolTip(
            '继续任务 · 解除本地暂停，不重复启动当前轮次' if active and self.client == 'codex' else '继续任务')
        buttons['manage'].setEnabled(controlled and not managed)
        buttons['unmanage'].setEnabled(managed)
        buttons['detach'].setEnabled(controlled)
        buttons['open_project'].setEnabled(self._project_directory is not None)
        buttons['open_project'].setToolTip(
            f'在 VS Code 打开项目\n{self._project_directory}' if self._project_directory else '任务未提供本机项目目录')
        if waiting_quota:
            message_hint = '额度暂不可用，刷新后自动继续；点击“暂泊”可取消自动恢复。'
        elif allowed:
            message_hint = '运行中也可发送，新指令会优先进入当前会话。' if self.client == 'claude' else '运行中补充指令，空闲时开始新一轮。'
        elif selected and selected.get('paused'):
            if self.client == 'codex' and (selected.get('threadStatus') or {}).get('type') == 'active' and selected.get('canControl'):
                message_hint = 'Codex 已在后台运行；点击“续航”解除本地暂停后即可发送。'
            else:
                message_hint = '已暂泊，点「续航」后即可发送。'
        else:
            message_hint = '点击案头任务接入后即可发送。'
        self.message_hint.setText(message_hint)
        self.send_button.setEnabled(allowed)
        self.check_operation_results(controls)

    def render_guard_quota(self, snap):
        connection = '已连接' if snap.get('connection') == 'connected' else '连接待检查'
        if self.client == 'claude':
            tasks = (snap.get('controls') or {}).get('tasks') or []
            count = sum(1 for task in tasks if task.get('managed') and not task.get('paused'))
            self.guard_text.setPlainText(f'Claude Code · {connection}\n自动推进已设置：{count} 个未暂停任务，每小时检查一次。\n只对本监控可控制、空闲、无审批且无执行错误的会话发送。')
        else:
            count = sum(1 for task in (snap.get('controls') or {}).get('tasks') or [] if task.get('managed') and not task.get('paused'))
            waits = snap.get('quotaWaits') or {}
            waiting = waits.get(self.selected_thread)
            description = (waiting.get('message', '') + '\n下次检查：' + self.local_time(waiting.get('due'))) if waiting else '额度不足时等待刷新，服务端确认恢复后继续原任务。'
            self.guard_text.setPlainText(f'Codex · {connection}\n自动推进已开启：{count} 个任务\n{description}\n仍需控制权、空闲、有效目标且无待审批。')
        if snap.get('quotaStatus') != 'official_api' or not snap.get('quota'):
            self.quota_text.setPlainText('Claude Code CLI 未提供官方剩余额度，显示未知。' if self.client == 'claude' else '暂时无法查询额度\n未知不代表额度为零。')
            return
        lines = []
        quota = snap['quota']
        permission = quota.get('ordinaryUsageAllowed')
        if permission is not None:
            lines.append('当前额度：可用' if permission else '当前额度：等待恢复')
        for name, group in [('默认', quota.get('default') or {}), *list((quota.get('models') or {}).items())]:
            for key, caption in (('primary', '短周期'), ('secondary', '长周期')):
                window = (group or {}).get(key) or {}
                if window.get('usedPercent') is not None:
                    lines.append(f'{name} · {caption}：已用 {window["usedPercent"]}%')
                    if window.get('resetsAt'):
                        lines.append('重置：' + self.local_time(window['resetsAt']))
        self.quota_text.setPlainText('\n'.join(lines) or '尚无额度窗口数据')

    def refresh_approvals(self):
        now = time.time()
        self._pending_approvals = {
            item['token']: item for item in monitor.approvals.public()
            if not item.get('decision') and item.get('expires', 0) > now
        }
        self._shown_approvals.intersection_update(self._pending_approvals)
        count = len(self._pending_approvals)
        self.stat_pending.setText(str(count))
        self.review_button.setVisible(count > 0)
        if self.approval_dialog is not None and self._review_token not in self._pending_approvals:
            self.approval_dialog.reject()
        self.offer_approval()

    def offer_approval(self, force=False):
        if self._quitting:
            return
        if self.approval_dialog is not None:
            if force:
                self.approval_dialog.show()
                self.approval_dialog.raise_()
                self.approval_dialog.activateWindow()
            return
        pending = (item for token, item in self._pending_approvals.items()
                   if force or token not in self._shown_approvals)
        item = next(pending, None)
        if item is None:
            return
        token = item['token']
        params = item.get('details') or {}
        method = item.get('method', '')
        title = '允许运行命令？' if 'commandExecution' in method else (
            '允许修改文件？' if 'fileChange' in method else '允许此权限请求？')
        dialog = QDialog(self)
        dialog.setObjectName('approvalDialog')
        dialog.setWindowTitle(title)
        dialog.setWindowIcon(self.windowIcon())
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.resize(510, 294)
        body = QVBoxLayout(dialog)
        body.setContentsMargins(20, 16, 20, 16)
        heading = QHBoxLayout()
        mark = QLabel()
        mark.setPixmap(self.windowIcon().pixmap(32, 32))
        heading.addWidget(mark)
        heading.addWidget(label(title, 14, True), 1)
        body.addLayout(heading)
        client = item.get('client', 'codex')
        snapshot = monitor.client_snapshot(monitor.snapshot(), client)
        names = dict(self.all_tasks(snapshot, client))
        task_name = names.get(params.get('threadId'), '未提供')
        info = label(
            f'客户端　{"Claude Code" if client == "claude" else "Codex"}\n'
            f'任务　{task_name}\n执行设备　本机\n工作目录　{params.get("cwd") or "未提供"}',
            9, color=MUTED)
        info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.addWidget(info)
        concrete = {key: value for key, value in params.items() if key not in ('threadId', 'turnId', 'itemId', 'client', 'cwd')}
        text = QTextBrowser()
        text.setObjectName('approvalDetails')
        configure_text(text, 9, 7)
        if set(concrete) == {'command'} and isinstance(concrete['command'], str):
            text.setPlainText(concrete['command'])
            text.setMaximumHeight(82)
        else:
            text.setPlainText(json.dumps(concrete, ensure_ascii=False, indent=2) if concrete else '未收到可核对的操作详情。')
        body.addWidget(text, 1)
        actions = QHBoxLayout()
        actions.addStretch()
        for title, allow in (('拒绝', False), ('允许一次', True)):
            button = ActionButton(title)
            button.setAutoDefault(False)
            if allow:
                button.setObjectName('approvalAllow')
                button.setEnabled(any(params.get(key) for key in ('command', 'changes', 'permissions', 'networkApprovalContext')))
            button.clicked.connect(lambda _=False, value=allow: self.decide(token, value))
            actions.addWidget(button)
        body.addLayout(actions)
        self._shown_approvals.add(token)
        self._review_token = token
        self.approval_dialog = dialog
        dialog.finished.connect(self.approval_closed)
        dialog.show()

    def approval_closed(self, _result):
        self.approval_dialog = None
        self._review_token = None

    def decide(self, token, allowed):
        try:
            result = monitor.approvals.decide(token, allowed)
            if result in ('unknown_delivery', 'disconnected', 'resolved'):
                self._popup('审批未完成', '请求已失效或送达结果不确定，请核查任务。')
            else:
                self.control_message.setText('已允许一次' if result == 'allowed' else '已拒绝')
        except (KeyError, ValueError) as error:
            self._popup('审批未完成', str(error))
        self.refresh()

    def refresh_conversation(self):
        if not self.selected_thread:
            self.conversation.setPlainText('未尽的话，可以接着说。选择一个任务查看对话。')
            return
        try:
            data = monitor.conversation_for(self.selected_thread, self.client)
            signature = json.dumps([self.client, self.selected_thread, data], ensure_ascii=False, sort_keys=True)
            if signature == self._conversation_signature:
                return
            self._conversation_signature = signature
            scroll = self.conversation.verticalScrollBar()
            old_value = scroll.value()
            at_bottom = old_value >= scroll.maximum() - 4
            bubbles = []
            for message in data.get('messages') or []:
                assistant = message.get('role') == 'assistant'
                role = ('Claude Code' if self.client == 'claude' else 'Codex') if assistant else '你'
                bubbles.append(message_html(role, message.get('text', ''), assistant))
            key = (self.client, self.selected_thread)
            errors = {json.dumps(event, sort_keys=True, ensure_ascii=False) for event in data.get('events') or []
                      if isinstance(event.get('status'), dict) and event['status'].get('error')}
            previous = self._error_events.get(key)
            if previous is not None:
                for encoded in errors - previous:
                    event = json.loads(encoded)
                    self._popup('任务执行失败', str(event['status']['error']))
            self._error_events[key] = errors
            for encoded in sorted(errors):
                bubbles.append(warning_html(display_text(json.loads(encoded)['status']['error'])))
            set_reading_html(self.conversation, ''.join(bubbles) or '<p style="color:#61738c;">灯下尚无来信。写下第一句吧。</p>')
            scroll.setValue(scroll.maximum() if at_bottom else min(old_value, scroll.maximum()))
        except (OSError, ValueError, RuntimeError) as error:
            self.conversation.setPlainText('暂时无法读取对话：' + str(error))

    @staticmethod
    def local_time(value):
        if not value:
            return '暂无记录'
        try:
            return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(float(value) / (1000 if float(value) > 1e12 else 1)))
        except (ValueError, TypeError, OSError):
            return '时间不可用'

    def closeEvent(self, event):
        if not self._quitting:
            event.ignore()
            self.hide()
            self.tray.showMessage('任务监控仍在运行', '点击托盘图标可以重新打开。', QSystemTrayIcon.MessageIcon.Information, 2000)
            return
        self.shutdown()
        event.accept()


def main():
    app = QApplication(sys.argv)
    shared = QSharedMemory('CodexMobileMonitor.QtDesktop')
    if not shared.create(1):
        import ctypes
        user32 = ctypes.windll.user32
        for title in (WINDOW_TITLE, 'Codex / Claude Code 任务运行监控', 'Codex 任务运行监控', 'Codex 本机守护'):
            hwnd = user32.FindWindowW(None, title)
            if hwnd:
                user32.ShowWindow(hwnd, 9)
                user32.SetForegroundWindow(hwnd)
                return 0
        QMessageBox.information(None, '任务监控', '程序已在后台运行，请从系统托盘打开。')
        return 1
    window = None
    try:
        window = MainWindow(shared)
        app.aboutToQuit.connect(window.shutdown)
        window.show()
        return app.exec()
    finally:
        try:
            if window is not None:
                window.shutdown()
        finally:
            # Hold the lock through worker cleanup and event-loop shutdown, not
            # merely until the tray/window disappears.
            shared.detach()


if __name__ == '__main__':
    raise SystemExit(main())
