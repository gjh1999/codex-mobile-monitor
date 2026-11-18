"""Real Qt controls + persistent controller for Codex reattach/continue regressions."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import monitor
from task_control import TaskControl
from tests.test_task_control import Mock, TARGET

try:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QMessageBox
    from desktop_qt import MainWindow
except ImportError:
    MainWindow = None


class Shared:
    def detach(self):
        pass


class Workers:
    def join(self, timeout=None):
        pass


@unittest.skipIf(MainWindow is None, 'Qt desktop dependencies not installed')
class Tests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.control = TaskControl(Path(temporary.name) / 'control.db')
        self.addCleanup(self.control.db.close)
        self.rpc = Mock()
        self.control.seed([TARGET])
        self.control.tick(self.rpc)
        self.control.submit(TARGET, 'pause', request_id='saved-pause')
        self.control.tick(self.rpc)
        self.rpc.active = True
        self.control.submit(TARGET, 'attach', request_id='reattach-already-running')
        self.control.tick(self.rpc)
        self.control.submit(TARGET, 'unmanage', request_id='keep-manual-control-only')
        self.control.tick(self.rpc)
        self.rpc.calls.clear()
        self.app = QApplication.instance() or QApplication([])
        original = monitor.snapshot()
        self.addCleanup(lambda: monitor.update(**original))
        for name, value in (('controller', self.control), ('claude_controller', None),
                            ('conversation_for', lambda *_: {'messages': [], 'events': []}),
                            ('start_background_service', lambda stop: (stop, Workers()))):
            patcher = patch.object(monitor, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        monitor.update(connection='connected',
                       threads=[{'id': TARGET, 'title': 'Codex reattached task', 'status': {'type': 'active'}}],
                       controls=self.control.snapshot(), quotaWaits={})
        self.window = MainWindow(Shared())
        self.addCleanup(self.close_window)
        self.window.timer.stop()
        self.window.show()
        QTest.qWait(30)

    def close_window(self):
        self.window._quitting = True
        for box in self.window.findChildren(QMessageBox):
            box.reject()
        self.window.close()
        self.app.processEvents()

    def test_continue_unlocks_messages_without_starting_or_steering_another_turn(self):
        self.window.message_edit.setPlainText('补充当前任务方向')
        self.assertFalse(self.window.send_button.isEnabled())
        resume = self.window.action_buttons['resume']
        self.assertTrue(resume.isEnabled(), 'A controlled running Codex task must allow local Continue')
        self.assertIn('后台运行', self.window.message_hint.text())
        QTest.mouseClick(resume, Qt.MouseButton.LeftButton)
        self.control.tick(self.rpc)
        self.window.refresh()
        QTest.qWait(30)
        self.assertFalse(self.control.snapshot()['tasks'][0]['paused'])
        self.assertFalse(self.control.snapshot()['tasks'][0]['managed'])
        self.assertEqual(self.window.message_edit.toPlainText(), '补充当前任务方向')
        self.assertTrue(self.window.send_button.isEnabled())
        self.assertTrue(self.window.action_buttons['pause'].isEnabled())
        self.assertFalse(self.window.action_buttons['resume'].isEnabled())
        self.assertFalse([m for m, p in self.rpc.calls if m in ('turn/start', 'turn/steer', 'thread/goal/set')])
        QTest.mouseClick(self.window.send_button, Qt.MouseButton.LeftButton)
        self.control.tick(self.rpc)
        self.window.refresh()
        writes = [(m, p) for m, p in self.rpc.calls if m in ('turn/start', 'turn/steer')]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][0], 'turn/steer')
        self.assertEqual(writes[0][1]['expectedTurnId'], 'turn-1')
        self.assertEqual(writes[0][1]['input'][0]['text'], '补充当前任务方向')
        self.assertFalse(any(box.isVisible() for box in self.window.findChildren(QMessageBox)))
        self.assertFalse(self.control.scheduled_tasks(self.rpc))

    def test_observed_running_task_keeps_continue_and_messages_disabled(self):
        self.rpc.writer = True
        self.control.submit(TARGET, 'attach', request_id='external-writer-owns-task')
        self.control.tick(self.rpc)
        self.window.refresh()
        self.assertFalse(self.control.snapshot()['tasks'][0]['canControl'])
        self.assertFalse(self.window.action_buttons['resume'].isEnabled())
        self.assertFalse(self.window.send_button.isEnabled())
        self.assertFalse([m for m, p in self.rpc.calls if m in ('turn/start', 'turn/steer')])


if __name__ == '__main__':
    unittest.main()
