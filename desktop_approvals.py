"""A single review window for local and workspace approval requests."""
import json

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import (
    QDialog, QHeaderView, QHBoxLayout, QLabel, QPushButton, QTextBrowser,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from desktop_theme import configure_text, ui_font

KINDS = {
    'item/commandExecution/requestApproval': '运行命令',
    'item/fileChange/requestApproval': '修改文件',
    'item/permissions/requestApproval': '权限请求',
}
STATUS = {
    'pending': '待审批', 'sending': '正在提交', 'queued': '等待回执', 'responding': '正在处理',
    'allowed': '已同意', 'denied': '已拒绝', 'resolved': '已在来源端处理', 'expired': '请求已过期',
    'not_found': '请求已失效', 'details_unavailable': '缺少操作详情', 'control_lost': '控制权已变更',
    'unavailable': '暂时无法处理', 'disconnected': '来源连接已断开', 'unknown_delivery': '结果未确认',
}
WORKSPACE = {'connected': '云工作区已连接', 'retrying': '正在重连云工作区', 'local': '本机使用'}
TOKEN = Qt.ItemDataRole.UserRole
CHECKED = Qt.CheckState.Checked


def approval_status(decision):
    return STATUS.get(decision, '结果未确认')


class ApprovalQueueDialog(QDialog):
    def __init__(self, parent, task_title, decide):
        super().__init__(parent)
        self.task_title = task_title
        self.decide = decide
        self.requests = {}
        self.names = {}
        self.items = {}
        self.setObjectName('approvalDialog')
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle('待你定夺 · 审批')
        self.setWindowIcon(parent.windowIcon())
        self.setMinimumSize(760, 410)
        self.resize(880, 520)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 15)
        layout.setSpacing(10)
        header = QHBoxLayout()
        layout.addLayout(header)
        self.summary = QLabel()
        self.summary.setFont(ui_font(12, True))
        header.addWidget(self.summary, 1)
        self.scope = QLabel()
        self.scope.setObjectName('approvalMeta')
        header.addWidget(self.scope)

        row = QHBoxLayout()
        row.setSpacing(14)
        layout.addLayout(row, 1)
        self.queue = QTreeWidget()
        self.queue.setObjectName('approvalQueue')
        self.queue.setAccessibleName('审批请求列表')
        self.queue.setHeaderLabels(['请求', '执行设备', '状态'])
        self.queue.setRootIsDecorated(False)
        self.queue.setUniformRowHeights(True)
        self.queue.setMinimumWidth(352)
        self.queue.header().setStretchLastSection(False)
        self.queue.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.queue.setColumnWidth(1, 83)
        self.queue.setColumnWidth(2, 104)
        self.queue.itemChanged.connect(self.update_actions)
        self.queue.currentItemChanged.connect(self.show_selected)
        row.addWidget(self.queue, 4)

        details = QVBoxLayout()
        details.setSpacing(7)
        row.addLayout(details, 5)
        self.heading = QLabel('审批详情')
        self.heading.setFont(ui_font(13, True))
        details.addWidget(self.heading)
        self.meta = QLabel()
        self.meta.setObjectName('approvalMeta')
        self.meta.setTextFormat(Qt.TextFormat.PlainText)
        self.meta.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.meta.setWordWrap(True)
        details.addWidget(self.meta)
        self.details = QTextBrowser()
        self.details.setObjectName('approvalDetails')
        self.details.setAccessibleName('命令或权限详情')
        configure_text(self.details, 9, 8)
        details.addWidget(self.details, 1)

        actions = QHBoxLayout()
        layout.addLayout(actions)
        self.reject_current = QPushButton('拒绝当前项')
        self.reject_current.clicked.connect(lambda: self.decide([self.current_token()], False))
        actions.addWidget(self.reject_current)
        actions.addStretch()
        self.approve_selected = QPushButton('同意所选')
        self.approve_selected.setObjectName('approvalAllow')
        self.approve_selected.clicked.connect(lambda: self.decide(self.checked_tokens(), True))
        actions.addWidget(self.approve_selected)
        self.approve_all = QPushButton('全部同意')
        self.approve_all.clicked.connect(lambda: self.decide(self.eligible_tokens(), True))
        actions.addWidget(self.approve_all)
        close = QPushButton('关闭')
        close.clicked.connect(self.reject)
        actions.addWidget(close)
        for button in (self.reject_current, self.approve_selected, self.approve_all, close):
            button.setAutoDefault(False)

    def current_token(self):
        item = self.queue.currentItem()
        return item.data(0, TOKEN) if item is not None else None

    def eligible_tokens(self):
        return [token for token, request in self.requests.items() if request['canAllow']]

    def checked_tokens(self):
        return [token for token in self.eligible_tokens() if self.items[token].checkState(0) == CHECKED]

    def update_requests(self, pending, feedback, submitting, workspace):
        requests = {}
        for token, item in {**pending, **{key: row[0] for key, row in feedback.items()}, **submitting}.items():
            item = pending.get(token, item)
            decision = 'sending' if token in submitting else 'queued' if item.get('decisionQueued') else (
                feedback[token][1] if token in feedback else 'pending')
            busy = bool(submitting) or decision in ('queued', 'responding')
            requests[token] = {
                'method': item['method'], 'client': item.get('client'), 'details': item.get('details') or {},
                'device': item.get('deviceName') or '本机', 'decision': decision,
                'canAllow': token in pending and item.get('canAllow', False) and not busy,
                'canReject': token in pending and not busy,
            }
        counts = [f'{sum(key in pending and row["decision"] not in ("queued", "sending") for key, row in requests.items())} 项待审批']
        waiting = sum(row['decision'] == 'queued' for row in requests.values())
        if waiting:
            counts.append(f'{waiting} 项等待回执')
        if submitting:
            counts.append(f'{len(submitting)} 项正在提交')
        self.summary.setText(' · '.join(counts))
        self.scope.setText(WORKSPACE.get(workspace, ''))
        if tuple(self.requests.items()) == tuple(requests.items()):
            return

        selected = self.current_token()
        checked = set(self.checked_tokens())
        # During an in-flight batch the buttons are disabled, while unrelated checks remain selected.
        checked.update(token for token, row in self.items.items() if row.checkState(0) == CHECKED)
        scroll = self.queue.verticalScrollBar().value()
        self.requests = requests
        with QSignalBlocker(self.queue):
            self.queue.clear()
            self.items.clear()
            self.names = {token: self.names[token] for token in requests if token in self.names}
            for token, request in requests.items():
                if token not in self.names:
                    self.names[token] = self.task_title(token, request)
                kind = KINDS.get(request['method'], '审批请求')
                caption = f'{kind} · {self.names[token]}'
                row = QTreeWidgetItem([caption, request['device'], approval_status(request['decision'])])
                row.setData(0, TOKEN, token)
                for column in range(3):
                    row.setToolTip(column, row.text(column))
                if request['canAllow'] or token in pending and pending[token].get('canAllow') and token not in submitting:
                    row.setCheckState(0, CHECKED if token in checked else Qt.CheckState.Unchecked)
                else:
                    row.setFlags(row.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                self.queue.addTopLevelItem(row)
                self.items[token] = row
            self.queue.setCurrentItem(self.items.get(selected) or self.queue.topLevelItem(0))
        self.queue.verticalScrollBar().setValue(scroll)
        self.show_selected()

    def show_selected(self):
        token = self.current_token()
        request = self.requests.get(token)
        if request is None:
            self.heading.setText('审批详情')
            self.meta.clear()
            self.details.setPlainText('当前没有待审批的请求。')
        else:
            params = request['details']
            client = 'Claude Code' if request['client'] == 'claude' else 'Codex'
            self.heading.setText(KINDS.get(request['method'], '审批请求'))
            self.meta.setText(f'客户端　{client}\n任务　{self.names[token]}\n执行设备　{request["device"]}\n'
                              f'工作目录　{params.get("cwd") or "未提供"}')
            concrete = {key: value for key, value in params.items()
                        if key not in ('threadId', 'turnId', 'itemId', 'client', 'cwd')}
            if set(concrete) == {'command'} and isinstance(concrete['command'], str):
                text = concrete['command']
            else:
                text = json.dumps(concrete, ensure_ascii=False, indent=2) if concrete else '未收到可核对的操作详情。'
            if self.details.toPlainText() != text:
                self.details.setPlainText(text)
        self.update_actions()

    def update_actions(self):
        request = self.requests.get(self.current_token(), {})
        self.reject_current.setEnabled(request.get('canReject', False))
        self.approve_selected.setEnabled(bool(self.checked_tokens()))
        self.approve_all.setEnabled(bool(self.eligible_tokens()))
