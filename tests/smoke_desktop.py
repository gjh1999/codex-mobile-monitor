"""Click real production Qt widgets against synthetic state (no model requests)."""
import argparse
import copy
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QPoint, QTimer, Qt
from PySide6.QtGui import QFontDatabase, QRawFont, QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QInputDialog, QLabel, QMessageBox, QPushButton

import monitor
from desktop_qt import MainWindow, OceanCover
from desktop_theme import configure_text, message_html, set_reading_html, ui_font

CODEX = '00000000-0000-4000-8000-000000000001'
CLAUDE = '11111111-2222-4333-8444-555555555555'


class NoWorkers:
    def join(self, timeout=None):
        pass


class Shared:
    def detach(self):
        pass


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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    codex = FakeControl(CODEX, '中文界面测试', True)
    claude = FakeControl(CLAUDE, 'Claude 运行测试', False)
    original_state = monitor.snapshot()
    window = None
    app = QApplication.instance() or QApplication(sys.argv[:1])
    data = {'messages': [{'role': 'user', 'text': '先查看进度，再继续下一步。'},
                         {'role': 'assistant', 'text': '当前任务正在执行，可以继续发送新指令。'}], 'events': []}
    monitor.update(connection='connected', threads=[{'id': CODEX, 'title': '中文界面测试', 'status': {'type': 'active'}}],
                   controls=codex.snapshot(), claude={'connection': 'connected', 'threads': [
                       {'id': CLAUDE, 'title': 'Claude 运行测试', 'status': {'type': 'active'}}], 'controls': claude.snapshot()})
    try:
        with patch.object(monitor, 'controller', codex), patch.object(monitor, 'claude_controller', claude), \
             patch.object(monitor, 'conversation_for', side_effect=lambda *_: copy.deepcopy(data)), \
             patch.object(monitor, 'start_background_service', side_effect=lambda stop: (stop, NoWorkers())):
            window = MainWindow(Shared())
            window.resize(1020, 640)
            window.show()
            window.timer.stop()  # Explicit refreshes make assertions deterministic.
            QTest.qWait(120)
            assert window.windowTitle() == '澜汐'
            cover = window.findChild(OceanCover)
            assert cover is not None and not cover.image.isNull(), 'Ocean cover must load'
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
            assert window.task_choice.currentText() == '中文界面测试'
            assert not window.task_action_buttons[CODEX]['manage'].isEnabled()
            assert window.task_action_buttons[CODEX]['unmanage'].isEnabled()
            assert window.task_action_buttons[CODEX]['pause'].isEnabled()
            # A running Codex turn can coexist with an older local pause after
            # reattach. Continue must stay clickable; actual routing is covered
            # with a persistent controller in test_codex_desktop.py.
            codex.data['tasks'][0]['paused'] = True
            window.refresh()
            assert not window.send_button.isEnabled()
            assert window.task_action_buttons[CODEX]['resume'].isEnabled()
            QTest.mouseClick(window.task_action_buttons[CODEX]['resume'], Qt.MouseButton.LeftButton)
            assert codex.calls[-1][:2] == (CODEX, 'resume')
            codex.data['tasks'][0]['paused'] = False
            window.refresh()
            assert window.send_button.isEnabled()
            button = window.task_action_buttons[CODEX]['pause']
            button.animate_to(12)
            QTest.qWait(180)
            assert button.effect.blurRadius() > 10, 'The hover animation must actually run'
            assert button.animation.duration() == 160
            if app.platformName() == 'windows':
                assert QFontDatabase.families(), 'Native Windows fonts must be available'
                brand = next(w for w in window.findChildren(QLabel) if w.objectName() == 'brand')
                assert brand.text() == '澜汐'
                assert QRawFont.fromFont(brand.font()).supportsCharacter(ord('澜')), 'Chinese glyphs are missing'
            window.message_edit.setPlainText('Codex 草稿')
            window.grab().save(str(args.output / 'codex-desktop.png'))
            QTest.mouseClick(window.client_buttons['claude'], Qt.MouseButton.LeftButton)
            assert window.client == 'claude' and window.selected_thread == CLAUDE
            assert window.message_edit.toPlainText() == ''
            assert window.send_button.isEnabled(), 'An active controlled Claude session must accept input'
            assert window.task_action_buttons[CLAUDE]['manage'].isEnabled(), 'Disabled only after explicit enable, not for the whole Claude client'
            QTest.mouseClick(window.task_action_buttons[CLAUDE]['manage'], Qt.MouseButton.LeftButton)
            assert claude.calls[-1][1] == 'manage'
            claude.data['tasks'][0]['managed'] = True
            claude.data['tasks'][0]['autoProgress'] = {'state': 'scheduled', 'due': 2000000000}
            window.refresh()
            assert not window.task_action_buttons[CLAUDE]['manage'].isEnabled()
            assert window.task_action_buttons[CLAUDE]['manage'].text() == '已开启自动推进'
            assert window.task_action_buttons[CLAUDE]['unmanage'].isEnabled()
            QTest.mouseClick(window.task_action_buttons[CLAUDE]['unmanage'], Qt.MouseButton.LeftButton)
            assert claude.calls[-1][1] == 'unmanage'
            claude.data['tasks'][0]['managed'] = False
            window.refresh()
            assert window.task_action_buttons[CLAUDE]['manage'].isEnabled()
            assert window.task_action_buttons[CLAUDE]['pause'].isEnabled()
            window.message_edit.setPlainText('请优先处理这个新指令')
            QTest.mouseClick(window.send_button, Qt.MouseButton.LeftButton)
            assert claude.calls[-1][:3] == (CLAUDE, 'message', '请优先处理这个新指令')
            message_id = claude.calls[-1][3]
            QTest.mouseClick(window.task_action_buttons[CLAUDE]['pause'], Qt.MouseButton.LeftButton)
            assert claude.calls[-1][1] == 'pause'
            # Verify the actual QInputDialog and target, not just a mock getText().
            def accept_name():
                dialog = app.activeModalWidget()
                assert isinstance(dialog, QInputDialog)
                dialog.setTextValue('我自己的任务名')
                dialog.accept()
            QTimer.singleShot(40, accept_name)
            QTest.mouseClick(window.rename_button, Qt.MouseButton.LeftButton)
            assert claude.calls[-1][:3] == (CLAUDE, 'rename', '我自己的任务名')
            claude.data['names'][CLAUDE] = '我自己的任务名'
            window.refresh()
            assert window.task_choice.currentText() == '我自己的任务名'
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
            assert not window.task_action_buttons[CLAUDE]['resume'].isEnabled()
            task['threadStatus'] = {'type': 'idle'}
            window.refresh()
            assert window.task_action_buttons[CLAUDE]['resume'].isEnabled()
            # New execution errors show a real popup; historical errors do not repeat.
            data['events'] = [{'method': 'error', 'time': 100, 'status': {'error': 'Synthetic execution error ' + CLAUDE}}]
            window.refresh_conversation()
            QTest.qWait(40)
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
            assert window.task_choice.currentText() == '我自己的任务名'
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
            QTest.qWait(60)
            window.conversation.verticalScrollBar().setValue(0)
            window.grab().save(str(args.output / 'codex-desktop.png'))
            # A quota wait is separate from a manual pause and has a real reset
            # timestamp. The current task makes that distinction visible.
            codex.data['tasks'][0]['threadStatus'] = {'type': 'idle'}
            reset_time = int(time.time()) + 1800
            monitor.update(threads=[{'id': CODEX, 'title': '中文界面测试', 'status': {'type': 'idle'}}],
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
            assert any(w.text() == '◷ 等待额度刷新' for w in window.findChildren(QLabel))
            assert window.task_action_buttons[CODEX]['pause'].isEnabled()
            window.grab().save(str(args.output / 'quota-wait-desktop.png'))
            codex.data['tasks'][0]['paused'] = True
            window.refresh()
            QTest.qWait(20)
            assert any(w.text() == 'Ⅱ 已暂停' for w in window.findChildren(QLabel))
            assert window.message_hint.text() == '任务已暂停，请先继续。'
            assert window.task_action_buttons[CODEX]['resume'].isEnabled()
            codex.data['tasks'][0]['paused'] = False
            monitor.update(quotaWaits={})
            window.refresh()
            assert window.send_button.isEnabled()
            window.close()
            assert not window.isVisible() and not window.stop.is_set(), 'Closing should keep workers in the tray'
            window.show_window()
            QTest.qWait(30)
            assert window.isVisible(), 'The tray action must reopen the workspace'
            print('Qt smoke passed: dual-client controls, drafts, rename, failures, animation, Chinese fonts, keyboard send, compact layout, wrapped messages and tray reopen.')
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
