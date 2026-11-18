"""Click real production Qt widgets against synthetic state (no model requests)."""
import argparse
import copy
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QEvent, QPoint, QPointF, QTimer, Qt
from PySide6.QtGui import QEnterEvent, QFontDatabase, QImage, QMouseEvent, QRawFont, QTextCursor, QTextDocument
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QFrame, QLabel, QMessageBox, QPushButton

import monitor
from automation import Approvals
from desktop_glass import FrostedPanel, OceanBackdrop
from desktop_notebook import NotebookStore
from desktop_qt import MainWindow
from desktop_theme import configure_text, message_html, set_reading_html, ui_font

CODEX = '00000000-0000-4000-8000-000000000001'
CLAUDE = '11111111-2222-4333-8444-555555555555'


class NoWorkers:
    def join(self, timeout=None):
        pass


class Shared:
    def detach(self):
        pass


class FakeApprovalClient:
    client_type = 'claude'

    def __init__(self):
        self.sent = []

    def send(self, reply):
        self.sent.append(reply)


class FakeControl:
    def __init__(self, tid, title, managed):
        self.calls = []
        self.data = {'tasks': [{'threadId': tid, 'title': title, 'canControl': True, 'paused': False,
                               'managed': managed, 'mode': 'control', 'threadStatus': {'type': 'active'}}],
                     'operations': [], 'names': {}, 'ready': True}

    def snapshot(self):
        return copy.deepcopy(self.data)

    def submit(self, tid, action, text, request_id):
        self.calls.append((tid, action, text, request_id))
        self.data['operations'].insert(0, {'id': request_id, 'threadId': tid, 'action': action,
                                          'status': 'queued', 'message': '等待确认'})
        return request_id


def wait_until(condition, seconds=2):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(.005)
    return condition()


def check_client_switch(window, output):
    switch = window.client_switch
    codex, claude, notebook = switch.buttons.values()
    left = Qt.MouseButton.LeftButton
    for button in (codex, claude, notebook):
        assert button.font().bold()
    with patch('desktop_glass.motion_enabled', return_value=True):
        QTest.mousePress(claude, left)
        assert claude.isDown() and window.client == 'codex'
        QTest.mouseRelease(claude, left)
        QTest.qWait(65)
        assert .05 * switch.offsets['claude'] < switch.position < .92 * switch.offsets['claude'] and switch.velocity > 0
        midpoint, velocity = switch.position, switch.velocity
        QTest.mouseClick(codex, left, pos=QPoint(4, codex.height() // 2))
        assert window.client == 'codex'
        assert switch.position == midpoint and switch.velocity == velocity, 'Reverse from current position and velocity'
        QTest.qWait(400)
        assert not switch.spring.isActive() and switch.position == 0.0

        origin = codex.rect().center()
        drag_to = origin + QPoint(64, 0)
        QTest.mousePress(codex, left, pos=origin)
        global_pos = QPointF(codex.mapToGlobal(drag_to))
        QApplication.sendEvent(codex, QMouseEvent(QEvent.Type.MouseMove, QPointF(drag_to), global_pos,
                                                  Qt.MouseButton.NoButton, left, Qt.KeyboardModifier.NoModifier))
        assert abs(switch.position - 64) < .1 and window.client == 'codex'
        switch.grab().save(str(output / 'client-switch-drag.png'))
        QApplication.sendEvent(codex, QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(drag_to), global_pos,
                                                  left, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
        assert window.client == 'claude'
        QTest.qWait(400)
        assert switch.position == switch.offsets['claude'] and not switch.spring.isActive()
    with patch('desktop_glass.motion_enabled', return_value=False):
        QTest.mouseClick(codex, left)
        assert switch.position == 0.0 and not switch.spring.isActive()


def check_notebook(window, output):
    with tempfile.TemporaryDirectory() as directory, patch.object(monitor, 'ROOT', Path(directory)):
        switch = window.client_switch
        previous_client = window.client
        previous_task = window.selected_thread
        original_draft = window.message_edit.toPlainText()
        QTest.mouseClick(switch.buttons['notebook'], Qt.MouseButton.LeftButton)
        workspace = window.notebook
        assert workspace is not None and workspace.isVisible()
        assert not window.task_workspace.isVisible() and window.client == previous_client
        QTest.qWait(420)
        assert switch.position == switch.offsets['notebook']
        note = workspace.notes
        note.title.setText('海边的随记')
        note.body.setPlainText('记录日常灵感，晚上再回看。')
        QTest.qWait(520)
        store = NotebookStore(Path(directory) / 'lanxi-notebook.sqlite3')
        assert store.entries('note', False)[0][1:] == ('海边的随记', '记录日常灵感，晚上再回看。')
        window.grab().save(str(output / 'notebook-desktop.png'))
        QTest.mouseClick(note.file_button, Qt.MouseButton.LeftButton)
        assert not store.entries('note', False)
        note.view.setCurrentIndex(1)
        note.list.setCurrentRow(0)
        assert note.body.toPlainText() == '记录日常灵感，晚上再回看。'
        QTest.mouseClick(note.file_button, Qt.MouseButton.LeftButton)
        assert not store.entries('note', True) and store.entries('note', False)

        workspace.tabs.setCurrentWidget(workspace.todos)
        todo = workspace.todos
        todo.title.setText('整理项目待办')
        QTest.mouseClick(todo.save_button, Qt.MouseButton.LeftButton)
        QTest.mouseClick(todo.file_button, Qt.MouseButton.LeftButton)
        assert not store.entries('todo', False) and store.entries('todo', True)
        todo.view.setCurrentIndex(1)
        todo.list.setCurrentRow(0)
        assert todo.title.text() == '整理项目待办'
        QTest.mouseClick(todo.file_button, Qt.MouseButton.LeftButton)
        todo.new_entry()
        todo.title.setText('关闭窗口时保存')
        QTest.mouseClick(switch.buttons[previous_client], Qt.MouseButton.LeftButton)
        assert not workspace.isVisible() and window.task_workspace.isVisible()
        assert window.selected_thread == previous_task and window.message_edit.toPlainText() == original_draft
        assert {row[1] for row in store.entries('todo', False)} == {'整理项目待办', '关闭窗口时保存'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    codex = FakeControl(CODEX, '中文界面测试', True)
    claude = FakeControl(CLAUDE, 'Claude 运行测试', False)
    original_state = monitor.snapshot()
    window = None
    approvals = Approvals()
    approvals_rpc = FakeApprovalClient()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    data = {'messages': [{'role': 'user', 'text': '先查看进度，再继续下一步。'},
                         {'role': 'assistant', 'text': '当前任务正在执行，可以继续发送新指令。'}], 'events': []}
    project_directory = str(Path(__file__).resolve().parents[1])
    monitor.update(connection='connected', threads=[{'id': CODEX, 'title': '中文界面测试', 'status': {'type': 'active'}, 'cwd': project_directory}],
                   controls=codex.snapshot(), claude={'connection': 'connected', 'threads': [
                       {'id': CLAUDE, 'title': 'Claude 运行测试', 'status': {'type': 'active'}}], 'controls': claude.snapshot()})
    try:
        with patch.object(monitor, 'approvals', approvals), patch.object(monitor, 'controller', codex), patch.object(monitor, 'claude_controller', claude), \
             patch.object(monitor, 'conversation_for', side_effect=lambda *_: copy.deepcopy(data)), \
             patch.object(monitor, 'start_background_service', side_effect=lambda stop: (stop, NoWorkers())):
            window = MainWindow(Shared())
            window.resize(1020, 640)
            window.show()
            window.timer.stop()  # Explicit refreshes make assertions deterministic.
            QTest.qWait(120)
            assert window.windowTitle() == '澜汐'
            ocean = window.centralWidget()
            assert isinstance(ocean, OceanBackdrop) and not ocean.current[0].isNull()
            assert len(ocean.wallpapers) == 3 and ocean.rotation.interval() == 30 * 60_000
            assert abs(window.client_switch.geometry().center().x() - ocean.rect().center().x()) <= 1
            verse = window.findChild(QLabel, 'heroVerse')
            source = window.findChild(QLabel, 'heroSource')
            intro = window.findChild(QLabel, 'heroIntro')
            formatted = QTextDocument()
            formatted.setHtml(intro.text())
            assert verse.font().bold() and verse.font().pointSize() == 42
            assert [verse.y(), source.y(), intro.y()] == sorted([verse.y(), source.y(), intro.y()])
            assert source.text() == '唐 · 王湾《次北固山下》'
            assert ' '.join(formatted.toPlainText().split()) == (
                '一处开始，处处继续。 白日写到的那一行，暮归时仍等着你。 '
                '终端续上未尽的话，掌中一瞥，便知今日行至何处。'
            )
            assert verse.width() >= verse.fontMetrics().horizontalAdvance(verse.text())
            task_heading = window.findChild(QLabel, 'taskPanelTitle')
            current_heading = window.conversation_heading
            assert task_heading.font() == current_heading.font()
            assert task_heading.mapTo(ocean, task_heading.rect().center()).y() == (
                current_heading.mapTo(ocean, current_heading.rect().center()).y())
            columns = window.summary.findChildren(QFrame, 'statColumn')
            assert len(columns) == 3 and len({column.width() for column in columns}) == 1
            for column in columns:
                caption, number = column.findChildren(QLabel)
                assert caption.font().pointSizeF() == 8.5 and not number.font().bold()
            assert len(window.summary.findChildren(QFrame, 'statDivider')) == 2
            normal = ocean.grab().toImage()
            original = ocean.current
            ocean.current = (original[0], QImage())
            window.client_switch.update()
            unblurred = ocean.grab().toImage()
            ocean.current = original
            window.client_switch.update()
            for card in (window.task_panel, window.conversation_card):
                assert isinstance(card, FrostedPanel)
                pos = card.mapTo(ocean, QPoint(card.width() // 2, card.height() * 2 // 3))
                pos *= normal.devicePixelRatio()
                assert normal.pixel(pos) != unblurred.pixel(pos), 'Frosted panel must sample the blurred wallpaper'
            switch = window.client_switch
            pos = switch.mapTo(ocean, QPoint(switch.width() - 9, switch.height() // 2))
            pos *= normal.devicePixelRatio()
            assert normal.pixel(pos) != unblurred.pixel(pos), 'The switch must sample the blurred wallpaper'
            thumb = switch.thumb_rect()
            pos = switch.mapTo(ocean, QPoint(round(thumb.center().x()), round(thumb.top()) + 4))
            pos *= normal.devicePixelRatio()
            assert normal.pixel(pos) != unblurred.pixel(pos), 'The thumb must also transmit wallpaper'
            assert switch.graphicsEffect() is None
            check_client_switch(window, args.output)
            assert window.conversation_header.opacity.opacity() == 0.0
            original_top = window.conversation.y()
            window.action_buttons['attach'].setFocus()
            QTest.qWait(190)
            assert window.conversation_header.opacity.opacity() == 1.0
            assert window.conversation.y() == original_top
            window.message_edit.setFocus()
            QTest.qWait(190)
            assert window.conversation_header.opacity.opacity() == 0.0
            window.task_search.setText('不存在的任务')
            assert window.task_items[CODEX].isHidden() and window.task_empty.isVisible()
            window.task_search.clear()
            assert not window.task_items[CODEX].isHidden()
            assert not hasattr(window, 'operation_log'), 'History must not be in the visible UI'
            # Reproduce the cold-start sequence, not only a preloaded snapshot:
            # placeholder -> DB ready with old failures -> more old pages arrive.
            cold_window = None
            initial_ready = codex.data.pop('ready')
            try:
                cold_window = MainWindow(Shared())
                cold_window.timer.stop()
                codex.data['ready'] = initial_ready
                old_failure = {'id': 'historic-not-attached', 'threadId': CODEX, 'action': 'message',
                               'status': 'failed', 'message': '请先接入这个任务。', 'stamp': 1}
                codex.data['operations'].append(old_failure)
                cold_window.refresh()
                codex.data['operations'].append(dict(old_failure, id='historic-late-arrival'))
                cold_window.refresh()
                QTest.qWait(40)
                assert not any(p.isVisible() for p in cold_window.findChildren(QMessageBox)), 'Startup must not replay old failures'
                assert not codex.calls, 'Startup must not submit control operations'
            finally:
                codex.data['ready'] = initial_ready
                codex.data['operations'].clear()
                if cold_window:
                    cold_window._quitting = True
                    cold_window.close()
                    cold_window.deleteLater()
                    app.processEvents()
            assert window.task_title() == '中文界面测试'
            other = '22222222-2222-4222-8222-222222222222'
            separate = '33333333-3333-4333-8333-333333333333'
            primary = {'id': CODEX, 'title': '中文界面测试', 'status': {'type': 'active'}, 'cwd': project_directory}
            monitor.update(threads=[
                primary, {'id': other, 'title': '整理笔记归档', 'cwd': project_directory},
                {'id': separate, 'title': '不同项目的会话', 'cwd': 'D:/Archive/mobile-monitor'},
            ])
            window.refresh()
            assert window.conversation_heading.text() == '中文界面测试'
            group = window.task_items[CODEX].parent()
            assert group.text(0).startswith(Path(project_directory).name) and group.childCount() == 2
            assert window.task_items[other].parent() is group
            assert window.task_items[separate].parent() is not group
            assert window.task_items[other].text(0) == '整理笔记归档\n待执舟'
            group.setExpanded(False)
            window.task_search.setText('笔记归档')
            assert group.isExpanded() and not window.task_items[other].isHidden()
            window.task_search.clear()
            assert not group.isExpanded()
            group.setExpanded(True)
            window.message_edit.setPlainText('单个任务的草稿')
            second_row = window.task_choice.visualItemRect(window.task_items[other]).center()
            QTest.mouseClick(window.task_choice.viewport(), Qt.MouseButton.LeftButton, pos=second_row)
            assert window.selected_thread == other and codex.calls[-1][:2] == (other, 'focus')
            assert window.message_edit.toPlainText() == ''
            window.refresh()
            assert window.task_items[other].text(0).endswith('执舟中')
            first_row = window.task_choice.visualItemRect(window.task_items[CODEX]).center()
            QTest.mouseClick(window.task_choice.viewport(), Qt.MouseButton.LeftButton, pos=first_row)
            assert codex.calls[-1][:2] == (CODEX, 'focus')
            assert window.message_edit.toPlainText() == '单个任务的草稿'
            window.message_edit.clear()
            for operation in codex.data['operations']:
                if operation['action'] == 'focus':
                    operation.update(status='succeeded', message='已执舟')
            window.refresh()
            editor = Path('C:/Program Files/Microsoft VS Code/Code.exe')
            with patch('desktop_qt.vscode_executable', return_value=editor), patch('desktop_qt.subprocess.Popen') as launch:
                QTest.mouseClick(window.action_buttons['open_project'], Qt.MouseButton.LeftButton)
                launch.assert_called_once_with([str(editor), project_directory])
            assert not window.action_buttons['manage'].isEnabled()
            assert window.action_buttons['unmanage'].isEnabled()
            assert window.action_buttons['pause'].isEnabled()
            # A running Codex turn can coexist with an older local pause after
            # reattach. Continue must stay clickable; actual routing is covered
            # with a persistent controller in test_codex_desktop.py.
            codex.data['tasks'][0]['paused'] = True
            window.refresh()
            assert not window.send_button.isEnabled()
            assert window.action_buttons['resume'].isEnabled()
            QTest.mouseClick(window.action_buttons['resume'], Qt.MouseButton.LeftButton)
            assert codex.calls[-1][:2] == (CODEX, 'resume')
            codex.data['tasks'][0]['paused'] = False
            window.refresh()
            assert window.send_button.isEnabled()
            if app.platformName() == 'windows':
                assert QFontDatabase.families(), 'Native Windows fonts must be available'
                assert QRawFont.fromFont(ui_font()).supportsCharacter(ord('澜')), 'Chinese glyphs are missing'
                icon = window.windowIcon().pixmap(128, 128).toImage()
                assert not icon.isNull() and icon.pixelColor(0, 0).alpha() == 0
            window.message_edit.setPlainText('Codex 草稿')
            window.grab().save(str(args.output / 'codex-desktop.png'))
            QTest.mouseClick(window.client_buttons['claude'], Qt.MouseButton.LeftButton)
            assert window.client == 'claude' and window.selected_thread == CLAUDE
            assert window.message_edit.toPlainText() == ''
            assert window.send_button.isEnabled(), 'An active controlled Claude session must accept input'
            assert window.action_buttons['manage'].isEnabled(), 'Disabled only after explicit enable, not for the whole Claude client'
            QTest.mouseClick(window.action_buttons['manage'], Qt.MouseButton.LeftButton)
            assert claude.calls[-1][1] == 'manage'
            claude.data['tasks'][0]['managed'] = True
            claude.data['tasks'][0]['autoProgress'] = {'state': 'scheduled', 'due': 2000000000}
            window.refresh()
            assert not window.action_buttons['manage'].isEnabled()
            assert window.action_buttons['manage'].text() == '扬帆'
            assert window.action_buttons['unmanage'].isEnabled()
            QTest.mouseClick(window.action_buttons['unmanage'], Qt.MouseButton.LeftButton)
            assert claude.calls[-1][1] == 'unmanage'
            claude.data['tasks'][0]['managed'] = False
            window.refresh()
            assert window.action_buttons['manage'].isEnabled()
            assert window.action_buttons['pause'].isEnabled()
            window.message_edit.setPlainText('请优先处理这个新指令')
            QTest.mouseClick(window.send_button, Qt.MouseButton.LeftButton)
            assert claude.calls[-1][:3] == (CLAUDE, 'message', '请优先处理这个新指令')
            message_id = claude.calls[-1][3]
            QTest.mouseClick(window.action_buttons['pause'], Qt.MouseButton.LeftButton)
            assert claude.calls[-1][1] == 'pause'
            QTest.mouseDClick(window.conversation_heading, Qt.MouseButton.LeftButton)
            assert window.title_stack.currentWidget() == window.title_editor
            assert window.title_editor.selectedText() == 'Claude 运行测试'
            before = len(claude.calls)
            window.title_editor.setText('这笔不落款')
            QTest.keyClick(window.title_editor, Qt.Key.Key_Escape)
            assert window.title_stack.currentWidget() == window.conversation_heading
            assert len(claude.calls) == before
            QTest.mouseDClick(window.conversation_heading, Qt.MouseButton.LeftButton)
            window.title_editor.setText('我自己的任务名')
            window.refresh()
            assert window.title_editor.text() == '我自己的任务名'
            QTest.qWait(260)
            window.grab().save(str(args.output / 'inline-rename-desktop.png'))
            QTest.keyClick(window.title_editor, Qt.Key.Key_Return)
            assert claude.calls[-1][:3] == (CLAUDE, 'rename', '我自己的任务名')
            claude.data['names'][CLAUDE] = '我自己的任务名'
            window.refresh()
            assert window.task_title() == '我自己的任务名'
            # A newer draft must never be overwritten by failure of an earlier send.
            window.message_edit.setPlainText('新的未发送草稿')
            op = next(o for o in claude.data['operations'] if o['id'] == message_id)
            op.update(status='failed', message='Synthetic send failure ' + CLAUDE)
            window.refresh()
            QTest.qWait(40)
            popups = window.findChildren(QMessageBox)
            assert len([p for p in popups if p.isVisible()]) == 1
            assert CLAUDE not in popups[0].text()
            assert window.message_edit.toPlainText() == '新的未发送草稿'
            popups[0].accept()
            QTest.qWait(30)
            window.refresh()
            QTest.qWait(30)
            assert not any(p.isVisible() for p in window.findChildren(QMessageBox)), 'Do not repeat a failure popup'
            # Pause is in progress until the actual turn stops, not merely its ack.
            task = claude.data['tasks'][0]
            task['paused'] = True
            window.refresh()
            assert not window.send_button.isEnabled()
            assert not window.action_buttons['resume'].isEnabled()
            task['threadStatus'] = {'type': 'idle'}
            window.refresh()
            assert window.action_buttons['resume'].isEnabled()
            # New execution errors show a real popup; historical errors do not repeat.
            assert wait_until(lambda: ('claude', CLAUDE) in window._error_events)
            data['events'] = [{'method': 'error', 'time': 100, 'status': {'error': 'Synthetic execution error ' + CLAUDE}}]
            window.refresh_conversation()
            assert wait_until(lambda: any('Synthetic execution error' in p.text()
                                         for p in window.findChildren(QMessageBox) if p.isVisible()))
            popups = [p for p in window.findChildren(QMessageBox) if p.isVisible()]
            assert len(popups) == 1 and 'Synthetic execution error' in popups[0].text()
            assert CLAUDE not in window.conversation.toPlainText()
            popups[0].accept()
            QTest.qWait(30)
            window.message_edit.setPlainText('Claude 草稿')
            window.grab().save(str(args.output / 'claude-desktop.png'))
            QTest.mouseClick(window.client_buttons['codex'], Qt.MouseButton.LeftButton)
            assert window.message_edit.toPlainText() == 'Codex 草稿'
            QTest.mouseClick(window.client_buttons['claude'], Qt.MouseButton.LeftButton)
            assert window.message_edit.toPlainText() == 'Claude 草稿'
            assert window.task_title() == '我自己的任务名'
            assert not any(p.isVisible() for p in window.findChildren(QMessageBox))
            labels = '\n'.join(w.text() for w in window.findChildren(QLabel))
            assert CODEX not in labels and CLAUDE not in labels
            # Enter adds a newline; Ctrl+Enter submits once through the same route.
            task['paused'] = False
            task['threadStatus'] = {'type': 'active'}
            monitor.update(claude={'connection': 'connected', 'threads': [
                {'id': CLAUDE, 'title': '我自己的任务名', 'status': {'type': 'active'}}],
                'controls': claude.snapshot()})
            window.refresh()
            before = len(claude.calls)
            window.message_edit.setPlainText('键盘发送')
            window.message_edit.moveCursor(QTextCursor.MoveOperation.End)
            QTest.keyClick(window.message_edit, Qt.Key.Key_Return)
            assert len(claude.calls) == before and window.message_edit.toPlainText().endswith('\n')
            QTest.keyClick(window.message_edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
            assert len(claude.calls) == before + 1
            assert claude.calls[-1][:3] == (CLAUDE, 'message', '键盘发送\n')
            assert window.message_edit.toPlainText() == ''
            task['paused'] = True
            task['threadStatus'] = {'type': 'idle'}
            window.refresh()
            window.message_edit.setPlainText('暂停时不能快捷发送')
            QTest.keyClick(window.message_edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
            assert len(claude.calls) == before + 1
            # Exercise a long reply at the minimum window size. The send button
            # remains in view while conversation and details scroll independently.
            sample = '已完成 **桌面布局优化**，保留双客户端独立任务。\n\n```python\nprint("中文消息与代码")\n```\n\n' + '继续核对结果。 ' * 100
            assert '&lt;script&gt;' in message_html('Codex', '<script>alert(1)</script>', True)
            data['messages'] = [{'role': 'assistant', 'text': sample}]
            data['events'] = []
            window.resize(1020, 640)
            window.refresh()
            QTest.qWait(80)
            send_corner = window.send_button.mapTo(window.centralWidget(), window.send_button.rect().bottomRight())
            assert window.centralWidget().rect().contains(send_corner), 'Send must stay inside the small window'
            assert window.conversation.height() >= 130
            assert window.conversation.horizontalScrollBar().maximum() == 0, 'Replies must wrap without horizontal scrolling'
            before_position = window.message_edit.mapTo(window.centralWidget(), QPoint(0, 0))
            window.conversation.verticalScrollBar().setValue(0)
            window.refresh_conversation()
            assert window.message_edit.mapTo(window.centralWidget(), QPoint(0, 0)) == before_position
            window.grab().save(str(args.output / 'compact-desktop.png'))
            # Final previews show normal production copy, not synthetic errors.
            window.resize(1180, 760)
            data['messages'] = [
                {'role': 'user', 'text': '检查当前进度，继续下一步，并保留处理结果。'},
                {'role': 'assistant', 'text': '已整理当前进度，接下来会：\n\n1. 核对任务状态与待办事项\n2. 推进下一项可执行工作\n3. 保存结果并汇报变化\n\n你可以随时补充方向，或暂停当前任务。'}]
            task['paused'] = False
            task['threadStatus'] = {'type': 'active'}
            claude.data['operations'].clear()
            window.message_edit.setPlainText('')
            window.control_message.setText('已连接，准备接收下一步指令')
            window.refresh()
            QTest.qWait(60)
            window.conversation.verticalScrollBar().setValue(0)
            window.grab().save(str(args.output / 'claude-desktop.png'))
            QTest.mouseClick(window.client_buttons['codex'], Qt.MouseButton.LeftButton)
            window.control_message.setText('任务正在运行，你可以随时补充指令')
            window.refresh()
            QTest.qWait(270)
            window.conversation.verticalScrollBar().setValue(0)
            window.grab().save(str(args.output / 'codex-desktop.png'))
            window.task_search.setFocus()
            header = window.conversation_header
            point = QPoint(10, 10)
            app.sendEvent(header, QEnterEvent(QPointF(point), QPointF(header.mapTo(window, point)),
                                              QPointF(header.mapToGlobal(point))))
            QTest.qWait(200)
            assert window.conversation_header.opacity.opacity() == 1.0
            window.grab().save(str(args.output / 'hover-desktop.png'))
            app.sendEvent(header, QEvent(QEvent.Type.Leave))
            QTest.qWait(200)
            assert window.conversation_header.opacity.opacity() == 0.0
            # A request from the inactive client must still open the correct native dialog.
            token = approvals.add(approvals_rpc, {
                'id': 37, 'method': 'item/commandExecution/requestApproval',
                'params': {'threadId': CLAUDE, 'cwd': 'D:/Projects/Demo', 'command': 'npm run test'},
            })
            window.refresh()
            QTest.qWait(70)
            dialog = window.approval_dialog
            assert dialog and dialog.isVisible() and window._review_token == token
            assert any('D:/Projects/Demo' in node.text() for node in dialog.findChildren(QLabel))
            dialog.grab().save(str(args.output / 'approval-desktop.png'))
            dialog.reject()
            window.refresh()
            assert window.approval_dialog is None and not approvals_rpc.sent
            QTest.mouseClick(window.review_button, Qt.MouseButton.LeftButton)
            QTest.qWait(30)
            assert window.approval_dialog is not None
            allow = window.approval_dialog.findChild(QPushButton, 'approvalAllow')
            QTest.mouseClick(allow, Qt.MouseButton.LeftButton)
            assert approvals_rpc.sent == [{'id': 37, 'result': {'decision': 'accept'}}]
            assert window.approval_dialog is None and window.stat_pending.text() == '0'
            for expected_index, name in ((1, 'big-sur'), (2, 'catalina')):
                ocean.rotation.timeout.emit()
                QTest.qWait(700)
                assert ocean.index == expected_index and not ocean.current[1].isNull()
                assert ocean.previous is None and ocean.fade == 1.0
                window.grab().save(str(args.output / f'{name}-desktop.png'))
            ocean.rotation.timeout.emit()
            QTest.qWait(700)
            assert ocean.index == 0
            # A quota wait is separate from a manual pause and has a real reset
            # timestamp. The current task makes that distinction visible.
            codex.data['tasks'][0]['threadStatus'] = {'type': 'idle'}
            reset_time = int(time.time()) + 1800
            monitor.update(threads=[{'id': CODEX, 'title': '中文界面测试', 'status': {'type': 'idle'}, 'cwd': project_directory}],
                           quotaStatus='official_api', quota={'ordinaryUsageAllowed': False, 'default': {
                               'primary': {'usedPercent': 100, 'resetsAt': reset_time},
                               'secondary': {'usedPercent': 62, 'resetsAt': reset_time + 3600}}, 'models': {}},
                           quotaWaits={CODEX: {'state': 'waiting', 'resetsAt': reset_time, 'due': reset_time + 2,
                                               'message': '额度已用完，等待刷新后自动继续。'}})
            window.control_message.setText('额度等待是自动暂停，不会反复重发指令')
            window.refresh()
            QTest.qWait(40)
            assert not window.send_button.isEnabled()
            assert '刷新后自动继续' in window.message_hint.text()
            assert '下次检查' in window.guard_text.toPlainText()
            assert window.action_buttons['pause'].isEnabled()
            window.grab().save(str(args.output / 'quota-wait-desktop.png'))
            codex.data['tasks'][0]['paused'] = True
            window.refresh()
            QTest.qWait(20)
            assert window.task_items[CODEX].text(0).endswith('已暂泊 · 待续航')
            assert window.message_hint.text() == '已暂泊，点「续航」后即可发送。'
            assert window.action_buttons['resume'].isEnabled()
            codex.data['tasks'][0]['paused'] = False
            monitor.update(quotaWaits={})
            window.refresh()
            assert window.send_button.isEnabled()
            window.close()
            assert not window.isVisible() and not window.stop.is_set(), 'Closing should keep workers in the tray'
            approvals.add(approvals_rpc, {
                'id': 38, 'method': 'item/commandExecution/requestApproval', 'params': {'command': 'npm run test'},
            })
            window.refresh()
            QTest.qWait(45)
            assert window.approval_dialog is not None and window.approval_dialog.isVisible()
            assert not window.isVisible()
            reject = next(button for button in window.approval_dialog.findChildren(QPushButton) if button.text() == '拒绝')
            QTest.mouseClick(reject, Qt.MouseButton.LeftButton)
            assert approvals_rpc.sent[-1] == {'id': 38, 'result': {'decision': 'decline'}}
            window.show_window()
            QTest.qWait(30)
            assert window.isVisible(), 'The tray action must reopen the workspace'
            check_notebook(window, args.output)
            print('Qt smoke passed: glass, rotation, hover/focus, approval, dual-client controls, drafts, rename, failure dialogs, Chinese glyphs, layout and tray.')
    finally:
        if window is not None:
            window._quitting = True
            for dialog in window.findChildren(QDialog):
                dialog.reject()
            window.close()
            app.processEvents()
        monitor.update(**original_state)


if __name__ == '__main__':
    main()
