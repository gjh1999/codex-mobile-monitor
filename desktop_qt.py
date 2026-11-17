"""Native task workspace: light cards, soft-blue actions and explicit failures."""
import json
import re
import sys
import threading
import time
import uuid
from pathlib import Path

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QSharedMemory, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFrame, QGraphicsDropShadowEffect, QGridLayout,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMainWindow,
    QMenu, QMessageBox, QPushButton, QScrollArea, QSizePolicy, QStyle, QSystemTrayIcon,
    QTextBrowser, QTextEdit, QVBoxLayout, QWidget,
)

import monitor
from desktop_theme import STYLESHEET, configure_text, message_html, set_reading_html, ui_font, warning_html

MUTED = '#61738c'
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


class Card(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('taskCard')


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
        self.client_drafts = {'codex': '', 'claude': ''}
        self._pending_operations = {}
        self._started_at = time.time()
        self._seen_operation_states = {'codex': {}, 'claude': {}}
        self._error_events = {}
        self._dialogs = []
        self._control_signature = None
        self._conversation_signature = None
        self.task_action_buttons = {}
        self.setWindowTitle('Codex / Claude Code 任务运行监控')
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
        root = QWidget()
        root.setObjectName('shell')
        self.setCentralWidget(root)
        shell = QHBoxLayout(root)
        shell.setContentsMargins(18, 18, 18, 18)
        shell.setSpacing(18)

        sidebar = QFrame()
        sidebar.setObjectName('sidebar')
        sidebar.setFixedWidth(224)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(16, 22, 16, 18)
        side.setSpacing(10)
        brand_row = QHBoxLayout()
        mark = label('任', 15, True)
        mark.setObjectName('brandMark')
        mark.setFixedSize(40, 40)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand_row.addWidget(mark)
        brand_copy = QVBoxLayout()
        brand_copy.setSpacing(2)
        brand = label('任务小站', 16, True)
        brand.setObjectName('brand')
        brand_copy.addWidget(brand)
        brand_copy.addWidget(label('专注当前，继续向前', 8.5, False, MUTED))
        brand_row.addLayout(brand_copy)
        brand_row.addStretch()
        side.addLayout(brand_row)
        side.addSpacing(20)
        side.addWidget(label('工作空间', 9, True, MUTED))
        self.client_buttons = {}
        for key, title in (('codex', 'Codex'), ('claude', 'Claude Code')):
            button = QPushButton(title)
            button.setFont(ui_font(10.5, True))
            button.setObjectName('clientSwitch')
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setCheckable(True)
            button.setChecked(key == self.client)
            button.clicked.connect(lambda _=False, c=key: self.switch_client(c))
            self.client_buttons[key] = button
            side.addWidget(button)
        self.live = label('○ 正在连接', 9)
        self.live.setObjectName('status')
        side.addWidget(self.live)
        side.addSpacing(17)
        side.addWidget(label('我的任务', 10, True))
        self.task_choice = QComboBox()
        self.task_choice.setFont(ui_font(9.5))
        self.task_choice.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.task_choice.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.task_choice.setMinimumContentsLength(8)
        self.task_choice.currentIndexChanged.connect(self.select_task)
        side.addWidget(self.task_choice)
        self.rename_button = ActionButton('修改任务名称')
        self.rename_button.setProperty('tone', 'quiet')
        self.rename_button.clicked.connect(self.rename_task)
        side.addWidget(self.rename_button)
        self.attach_button = ActionButton('接入当前任务')
        self.attach_button.setObjectName('attachPrimary')
        self.attach_button.clicked.connect(lambda: self.operate('attach'))
        side.addWidget(self.attach_button)
        create = ActionButton('＋ 创建测试任务')
        create.setProperty('tone', 'quiet')
        create.clicked.connect(lambda: self.operate('create_test'))
        side.addWidget(create)
        side.addStretch()
        summary = QFrame()
        summary.setObjectName('sidebarStats')
        counts = QGridLayout(summary)
        counts.setContentsMargins(12, 12, 12, 12)
        counts.setHorizontalSpacing(10)
        counts.setVerticalSpacing(4)
        self.stat_total, self.stat_running, self.stat_pending = (label('—', 17, True) for _ in range(3))
        for column, (value, caption) in enumerate(((self.stat_total, '任务'), (self.stat_running, '运行'), (self.stat_pending, '待确认'))):
            value.setAlignment(Qt.AlignmentFlag.AlignCenter)
            counts.addWidget(value, 0, column)
            caption_label = label(caption, 8.5, False, MUTED)
            caption_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            counts.addWidget(caption_label, 1, column)
        side.addWidget(summary)
        side.addSpacing(5)
        side.addWidget(label('● 关闭窗口后继续在托盘运行', 8, False, MUTED))
        shell.addWidget(sidebar)

        workspace = QWidget()
        workspace.setObjectName('workspace')
        page = QVBoxLayout(workspace)
        page.setContentsMargins(0, 3, 0, 0)
        page.setSpacing(14)
        heading = QHBoxLayout()
        title = QVBoxLayout()
        title.setSpacing(3)
        page_title = label('任务工作台', 20, True)
        page_title.setObjectName('pageTitle')
        title.addWidget(page_title)
        self.workspace_subtitle = label('Codex · 所有任务都在本机', 9, False, MUTED)
        title.addWidget(self.workspace_subtitle)
        heading.addLayout(title, 1)
        refresh = ActionButton('刷新状态')
        refresh.setProperty('tone', 'quiet')
        refresh.clicked.connect(self.refresh)
        heading.addWidget(refresh)
        page.addLayout(heading)

        focus = QFrame()
        focus.setObjectName('focus')
        body = QVBoxLayout(focus)
        body.setContentsMargins(16, 13, 16, 13)
        body.setSpacing(7)
        self.control_cards = QVBoxLayout()
        self.control_cards.setSpacing(8)
        body.addLayout(self.control_cards)
        self.control_message = label('操作结果会在这里显示', 8.5, False, MUTED)
        self.control_message.setObjectName('messageNotice')
        body.addWidget(self.control_message)
        page.addWidget(focus)

        middle = QHBoxLayout()
        middle.setSpacing(14)
        self.conversation_card = QFrame()
        self.conversation_card.setObjectName('conversationCard')
        self.conversation_card.setMinimumWidth(360)
        conversation_layout = QVBoxLayout(self.conversation_card)
        conversation_layout.setContentsMargins(18, 17, 18, 14)
        conversation_layout.setSpacing(10)
        conversation_header = QHBoxLayout()
        conversation_title = QVBoxLayout()
        conversation_title.setSpacing(3)
        self.conversation_heading = label('任务对话', 12, True)
        self.conversation_heading.setObjectName('sectionTitle')
        conversation_title.addWidget(self.conversation_heading)
        self.conversation_subtitle = label('最近消息 · 仅保存在本机', 8.5, False, MUTED)
        conversation_title.addWidget(self.conversation_subtitle)
        conversation_header.addLayout(conversation_title, 1)
        record_badge = label('本机记录', 8, False, MUTED)
        conversation_header.addWidget(record_badge)
        conversation_layout.addLayout(conversation_header)
        divider = QFrame()
        divider.setObjectName('divider')
        divider.setFixedHeight(1)
        conversation_layout.addWidget(divider)
        self.conversation = QTextBrowser()
        self.conversation.setObjectName('conversation')
        configure_text(self.conversation, 10, 2)
        self.conversation.setMinimumHeight(130)
        self.conversation.setOpenExternalLinks(False)
        conversation_layout.addWidget(self.conversation, 1)

        composer_box = QFrame()
        composer_box.setObjectName('composerBox')
        composer_layout = QVBoxLayout(composer_box)
        composer_layout.setContentsMargins(11, 7, 11, 10)
        composer_layout.setSpacing(4)
        self.message_edit = Composer()
        self.message_edit.setObjectName('composer')
        configure_text(self.message_edit, 10, 3)
        self.message_edit.setFixedHeight(70)
        self.message_edit.setPlaceholderText('输入下一步指令，或补充任务需要的信息…')
        self.message_edit.setAcceptRichText(False)
        self.message_edit.send_requested.connect(lambda: self.operate('message') if self.send_button.isEnabled() else None)
        composer_layout.addWidget(self.message_edit)
        send_row = QHBoxLayout()
        send_row.addWidget(label('Ctrl + Enter 发送 · Enter 换行', 8, False, MUTED), 1)
        self.send_button = ActionButton('发送指令  →')
        self.send_button.setObjectName('send')
        self.send_button.clicked.connect(lambda: self.operate('message'))
        send_row.addWidget(self.send_button)
        composer_layout.addLayout(send_row)
        conversation_layout.addWidget(composer_box)
        self.message_hint = label('请先接入当前任务', 8.5, False, MUTED)
        conversation_layout.addWidget(self.message_hint)
        middle.addWidget(self.conversation_card, 1)

        self.details_scroll = QScrollArea()
        self.details_scroll.setWidgetResizable(True)
        self.details_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.details_scroll.setFixedWidth(264)
        details = QWidget()
        details.setObjectName('details')
        detail_layout = QVBoxLayout(details)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(12)
        approval_group = QFrame()
        approval_group.setObjectName('sectionCard')
        self.approval_layout = QVBoxLayout(approval_group)
        self.approval_layout.setContentsMargins(14, 15, 14, 15)
        self.approval_layout.setSpacing(10)
        detail_layout.addWidget(approval_group)
        for heading, attr in (('连接与自动推进', 'guard_text'), ('账户额度', 'quota_text')):
            card = QFrame()
            card.setObjectName('sectionCard')
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(14, 15, 14, 13)
            card_layout.setSpacing(8)
            section_title = label(heading, 10, True)
            section_title.setObjectName('sectionTitle')
            card_layout.addWidget(section_title)
            text = QTextBrowser()
            text.setObjectName('infoText')
            configure_text(text, 9, 0)
            text.setFixedHeight(118 if attr == 'guard_text' else 105)
            setattr(self, attr, text)
            card_layout.addWidget(text)
            detail_layout.addWidget(card)
        detail_layout.addStretch()
        self.details_scroll.setWidget(details)
        middle.addWidget(self.details_scroll)
        page.addLayout(middle, 1)
        shell.addWidget(workspace, 1)

    def build_tray(self):
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(self.windowIcon() if not self.windowIcon().isNull() else self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))
        self.tray.setToolTip('Codex / Claude Code 任务运行监控')
        menu = QMenu(self)
        menu.addAction('打开监控窗口', self.show_window)
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

    def select_task(self):
        self.selected_thread = self.task_choice.currentData() or ''
        self._conversation_signature = None
        self.refresh()

    def all_tasks(self, snap):
        controls = snap.get('controls') or {}
        names = controls.get('names') or {}
        labels = (getattr(monitor, 'history_labels', {}) or {}) if self.client == 'codex' else {}
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
        self.client_drafts[self.client] = self.message_edit.toPlainText()
        self.client = client
        self.selected_thread = self.client_selections[client]
        self.message_edit.setPlainText(self.client_drafts[client])
        self._conversation_signature = None
        for key, button in self.client_buttons.items():
            button.setChecked(key == client)
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
        client_name = 'Claude Code' if self.client == 'claude' else 'Codex'
        self.workspace_subtitle.setText(client_name + ' · 所有任务都在本机')
        threads = snap.get('threads') or []
        self.stat_total.setText(str(len(threads)))
        self.stat_running.setText(str(sum(1 for task in threads if (task.get('status') or {}).get('type') == 'active'
                                           and not (task.get('status') or {}).get('activeFlags'))))
        pending = [a for a in snap.get('approvals') or [] if not a.get('decision') and a.get('expires', time.time() + 1) > time.time()]
        self.stat_pending.setText(str(len(pending)))
        titles = self.all_tasks(snap)
        self.task_choice.blockSignals(True)
        self.task_choice.clear()
        for tid, title in titles:
            self.task_choice.addItem(title, tid)
        index = self.task_choice.findData(self.selected_thread)
        if index < 0 and titles:
            index = 0
        self.task_choice.setCurrentIndex(index)
        self.selected_thread = self.task_choice.currentData() or ''
        self.task_choice.setToolTip(self.task_choice.currentText())
        self.task_choice.blockSignals(False)
        selected_title = self.task_choice.currentText() if self.selected_thread else '任务对话'
        self.conversation_heading.setText('任务对话')
        self.conversation_heading.setToolTip(selected_title)
        self.conversation_subtitle.setText(('Claude Code' if self.client == 'claude' else 'Codex') + ' · 最近消息 · 本机记录')
        self.render_controls(snap)
        self.render_guard_quota(snap)
        self.render_approvals(pending)
        self.refresh_conversation()

    def rename_task(self):
        if not self.selected_thread:
            return
        target, client = self.selected_thread, self.client
        title, accepted = QInputDialog.getText(self, '修改任务名称', '名称（1–80字，仅修改本机显示）',
                                              QLineEdit.EchoMode.Normal, self.task_choice.currentText())
        if accepted:
            if client != self.client:
                self._popup('名称未保存', '客户端已切换，请在原任务上重新修改名称。')
            else:
                self.operate('rename', title, tid=target)

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
            self.control_message.setText('正在请求暂停当前执行…' if action == 'pause' else '正在保存任务名称…' if action == 'rename' else '已提交，等待确认')
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
            if status in ('failed', 'unknown') and previous != status and (own or fresh):
                if own and own['action'] == 'message' and own['threadId'] == self.selected_thread and not self.message_edit.toPlainText():
                    self.message_edit.setPlainText(own['text'])
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
        titles = dict(self.all_tasks(snap))
        visible = {key: selected.get(key) for key in ('threadId', 'paused', 'managed', 'canControl', 'message', 'threadStatus', 'autoProgress')} if selected else None
        signature = json.dumps([self.client, self.selected_thread, visible, quota_wait, titles.get(self.selected_thread)], ensure_ascii=False, sort_keys=True)
        if signature != self._control_signature:
            self._control_signature = signature
            while self.control_cards.count():
                item = self.control_cards.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            self.task_action_buttons = {}
            card = Card()
            body = QVBoxLayout(card)
            body.setContentsMargins(0, 0, 0, 0)
            body.setSpacing(8)
            row = QHBoxLayout()
            row.setSpacing(12)
            task_title = label(titles.get(self.selected_thread) or '从左侧选择一个任务', 12, True)
            task_title.setObjectName('taskTitle')
            row.addWidget(task_title, 1)
            active = bool(selected and (selected.get('threadStatus') or {}).get('type') == 'active')
            state = '未接入'
            tone = 'muted'
            if selected:
                if selected.get('paused'):
                    state = ('Ⅱ 本地已暂停 · 后台运行中' if self.client == 'codex' else '◷ 暂停中') if active else 'Ⅱ 已暂停'
                    tone = 'warning'
                elif not selected.get('canControl'):
                    state = '○ 只读关注'
                elif waiting_quota:
                    state = '◷ 等待额度刷新'
                    tone = 'warning'
                elif active:
                    state = '● 运行中'
                    tone = 'active'
                else:
                    state = '○ 可控制'
                    tone = 'active'
            badge = label(state, 8.5)
            badge.setObjectName('taskState')
            badge.setProperty('tone', tone)
            row.addWidget(badge)
            body.addLayout(row)
            if selected:
                tid = selected['threadId']
                progress = selected.get('autoProgress')
                if selected.get('paused') and active and self.client == 'codex':
                    caption = 'Codex 已在后台运行；点击“继续任务”解除本地暂停后即可发送，不会重复启动。'
                elif quota_wait:
                    caption = quota_wait.get('message') or '等待额度恢复后自动继续。'
                    if quota_wait.get('resetsAt'):
                        caption += ' 刷新时间：' + self.local_time(quota_wait['resetsAt'])
                    elif waiting_quota:
                        caption += ' 接口未提供刷新时间，定期重新查询。'
                elif selected.get('managed'):
                    caption = ('自动推进已开启 · 任务暂停期间不会发送' if selected.get('paused') else
                               '自动推进已开启 · 下次检查 ' + self.local_time(progress['due']) if progress and progress.get('due') else
                               '自动推进已开启 · 满足空闲、控制权和目标条件时继续')
                else:
                    caption = ('任务已暂停 · 继续后可以发送新指令' if selected.get('paused') else
                               display_text(selected.get('message') or '已接入当前任务，可以发送下一步指令。'))
                body.addWidget(label(caption, 9, False, MUTED))
                actions = QHBoxLayout()
                actions.setSpacing(8)
                buttons = {}
                for caption, action in (('重新接入', 'attach'), ('已开启自动推进', 'manage') if selected.get('managed') else ('开启自动推进', 'manage'),
                                        ('关闭自动推进', 'unmanage'), ('移出', 'detach'),
                                        ('继续任务', 'resume') if selected.get('paused') else ('暂停任务', 'pause')):
                    button = ActionButton(caption)
                    button.setFont(ui_font(9))
                    button.setProperty('tone', 'soft' if action in ('manage', 'unmanage', 'pause', 'resume') else 'quiet')
                    button.clicked.connect(lambda _=False, a=action, i=tid: self.operate(a, tid=i))
                    if action == 'manage':
                        button.setEnabled(not selected.get('managed'))
                        button.setVisible(not selected.get('managed'))
                        button.setToolTip('每小时检查一次；空闲、可控制且无审批或执行错误时才继续。')
                    elif action == 'unmanage':
                        button.setEnabled(bool(selected.get('managed')))
                        button.setVisible(bool(selected.get('managed')))
                    elif action == 'resume':
                        button.setEnabled(bool(selected.get('canControl')) and (self.client == 'codex' or not active))
                        if self.client == 'codex' and active:
                            button.setToolTip('当前任务已运行；仅解除本地暂停并恢复消息发送，不额外启动一轮。')
                    buttons[action] = button
                    actions.addWidget(button)
                actions.addStretch()
                self.task_action_buttons[tid] = buttons
                body.addLayout(actions)
            else:
                body.addWidget(label('接入后即可发送指令、暂停执行和查看最新回复。', 9, False, MUTED))
            self.control_cards.addWidget(card)
        allowed = bool(selected and selected.get('canControl') and not selected.get('paused') and not waiting_quota)
        attach_style = 'taskAction' if selected else 'attachPrimary'
        if self.attach_button.objectName() != attach_style:
            self.attach_button.setObjectName(attach_style)
            self.attach_button.style().unpolish(self.attach_button)
            self.attach_button.style().polish(self.attach_button)
        self.attach_button.setText('重新接入任务' if selected else '接入当前任务')
        if waiting_quota:
            message_hint = '额度暂不可用，刷新后自动继续；点击“暂停任务”可取消自动恢复。'
        elif allowed:
            message_hint = '运行中也可发送，新指令会优先进入当前会话。' if self.client == 'claude' else '运行中补充指令，空闲时开始新一轮。'
        elif selected and selected.get('paused'):
            if self.client == 'codex' and (selected.get('threadStatus') or {}).get('type') == 'active' and selected.get('canControl'):
                message_hint = 'Codex 已在后台运行；点击“继续任务”解除本地暂停后即可发送。'
            else:
                message_hint = '任务已暂停，请先继续。'
        else:
            message_hint = '接入任务并取得控制权后即可发送。'
        self.message_hint.setText(message_hint)
        self.send_button.setEnabled(allowed)
        self.rename_button.setEnabled(bool(self.selected_thread))
        self.attach_button.setEnabled(bool(self.selected_thread))
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

    def render_approvals(self, approvals):
        signature = json.dumps(approvals, ensure_ascii=False, sort_keys=True)
        if signature == getattr(self, '_approval_signature', None):
            return
        self._approval_signature = signature
        while self.approval_layout.count():
            item = self.approval_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        section_title = label('待确认操作', 10, True)
        section_title.setObjectName('sectionTitle')
        self.approval_layout.addWidget(section_title)
        for approval in approvals:
            card = Card()
            body = QVBoxLayout(card)
            body.addWidget(label('需要确认一项操作', 9.5, True))
            details = approval.get('details') or {}
            # Keep concrete command / permission details complete for consent.
            visible = {key: value for key, value in details.items() if key not in ('threadId', 'turnId', 'itemId', 'client')}
            summary = details.get('command') or json.dumps(details.get('permissions') or details.get('changes') or visible, ensure_ascii=False, indent=2)
            detail = QTextBrowser()
            configure_text(detail, 9, 3)
            detail.setPlainText(str(summary))
            detail.setMinimumHeight(100)
            detail.setMaximumHeight(180)
            body.addWidget(detail)
            buttons = QHBoxLayout()
            for text, decision in (('允许一次', True), ('拒绝', False)):
                button = ActionButton(text)
                button.clicked.connect(lambda _=False, token=approval['token'], allow=decision: self.decide(token, allow))
                buttons.addWidget(button)
            body.addLayout(buttons)
            self.approval_layout.addWidget(card)
        if not approvals:
            self.approval_layout.addWidget(label('✓ 暂无待确认操作', 9, False, MUTED))
            self.approval_layout.addWidget(label('需要你确认时，会在这里展示操作详情。', 8.5, False, MUTED))
        self.approval_layout.addStretch()

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
            self.conversation.setPlainText('选择一个任务，最近对话会显示在这里。')
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
            set_reading_html(self.conversation, ''.join(bubbles) or '<p style="color:#61738c;">等待第一条消息，在下方写下下一步。</p>')
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
        for title in ('Codex / Claude Code 任务运行监控', 'Codex 任务运行监控', 'Codex 本机守护'):
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
