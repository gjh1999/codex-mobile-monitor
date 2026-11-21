"""Exercise the real Qt application loop in an isolated subprocess, without CLIs."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
import uuid
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]


def probe(mode):
    sys.path.insert(0, str(PROJECT))
    from PySide6.QtCore import QSharedMemory, QTimer
    from PySide6.QtWidgets import QApplication
    import desktop_qt
    import monitor

    report = {'mode': mode, 'fallbackNeeded': False, 'joinCalls': 0, 'startedWorkers': 0}
    shared_key = 'CodexMonitorShutdownTest.' + uuid.uuid4().hex
    holder = {}

    def start_workers(stop):
        entered = [threading.Event(), threading.Event()]
        finished = [threading.Event(), threading.Event()]

        def run(index):
            entered[index].set()
            stop.wait(3)
            finished[index].set()

        threads = [threading.Thread(target=run, args=(index,), daemon=True) for index in range(2)]
        for thread in threads:
            thread.start()
        assert all(event.wait(1) for event in entered)
        report['startedWorkers'] = len(threads)

        class Workers:
            def join(self, timeout=None):
                report['joinCalls'] += 1
                report['lockHeldDuringCleanup'] = holder['shared'].isAttached()
                for thread in threads:
                    thread.join(timeout=1)
                report['workersStopped'] = all(event.is_set() for event in finished)
        return stop, Workers()

    original_window = desktop_qt.MainWindow

    class ObservedWindow(original_window):
        def __init__(self, shared, workspace=None):
            holder['shared'] = shared
            holder['window'] = self
            super().__init__(shared, workspace)
            app = QApplication.instance()

            def about_to_quit():
                report['lockHeldUntilLoopExit'] = shared.isAttached()
                report['stopRequestedAtLoopExit'] = self.stop.is_set()
                report['trayHiddenAtLoopExit'] = not self.tray.isVisible()
            # main() connects shutdown after the window constructor returns.
            # Observe afterwards so direct QApplication.quit checks see the
            # completed aboutToQuit cleanup, not an earlier subscriber's state.
            QTimer.singleShot(0, lambda: app.aboutToQuit.connect(about_to_quit))

            def hide_first():
                self.close()
                report['hiddenBeforeExit'] = not self.isVisible()
                report['keptWorkersWhenHidden'] = not self.stop.is_set()
                report['lockHeldWhenHidden'] = shared.isAttached()
                report['trayPresentWhenHidden'] = self.tray.isVisible()

            def exit_from_menu():
                action = next(action for action in self.tray.contextMenu().actions()
                              if action.text() == '退出程序')
                action.trigger()
                # Cleanup is idempotent, including a second explicit close.
                self.close()

            def watchdog():
                report['fallbackNeeded'] = True
                app.quit()

            if mode == 'hidden':
                QTimer.singleShot(30, hide_first)
            QTimer.singleShot(90, app.quit if mode == 'application_quit' else exit_from_menu)
            QTimer.singleShot(500, watchdog)

    monitor.update(threads=[], controls={'ready': True, 'tasks': [], 'operations': []},
                   claude={'threads': [], 'controls': {'ready': True, 'tasks': [], 'operations': []}})
    with patch.object(desktop_qt, 'QSharedMemory', side_effect=lambda _: QSharedMemory(shared_key)), \
         patch.object(desktop_qt, 'MainWindow', ObservedWindow), \
         patch.object(monitor, 'start_background_service', side_effect=start_workers), \
         patch.object(monitor, 'conversation_for', return_value={'messages': [], 'events': []}), \
         patch.object(monitor, 'controller', None), patch.object(monitor, 'claude_controller', None):
        report['exitCode'] = desktop_qt.main()
    report['lockReleasedAfterMain'] = not holder['shared'].isAttached()
    replacement = QSharedMemory(shared_key)
    report['replacementCanAcquireLock'] = replacement.create(1)
    if replacement.isAttached():
        replacement.detach()
    report['stopRequested'] = holder['window'].stop.is_set()
    report['timerStopped'] = not holder['window'].timer.isActive()
    print(json.dumps(report, ensure_ascii=True), flush=True)


class Tests(unittest.TestCase):
    def test_application_and_tray_exit_finish_the_process_and_release_the_lock(self):
        try:
            import PySide6
        except ImportError:
            self.skipTest('Qt desktop dependencies not installed')
        for mode in ('hidden', 'visible', 'application_quit'):
            with self.subTest(mode=mode):
                environment = {**os.environ, 'QT_QPA_PLATFORM': 'offscreen'}
                result = subprocess.run([sys.executable, '-I', str(Path(__file__).resolve()), '--probe', mode],
                                        cwd=PROJECT, env=environment, capture_output=True, text=True,
                                        encoding='utf-8', timeout=12,
                                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                report = json.loads(result.stdout.strip().splitlines()[-1])
                self.assertFalse(report['fallbackNeeded'], report)
                self.assertEqual(report['exitCode'], 0)
                self.assertEqual(report['startedWorkers'], 2)
                self.assertEqual(report['joinCalls'], 1, report)
                for field in ('workersStopped', 'stopRequested', 'timerStopped', 'lockHeldDuringCleanup',
                              'lockHeldUntilLoopExit', 'stopRequestedAtLoopExit', 'trayHiddenAtLoopExit',
                              'lockReleasedAfterMain', 'replacementCanAcquireLock'):
                    self.assertTrue(report.get(field), (field, report))
                if mode == 'hidden':
                    for field in ('hiddenBeforeExit', 'keptWorkersWhenHidden', 'lockHeldWhenHidden',
                                  'trayPresentWhenHidden'):
                        self.assertTrue(report.get(field), (field, report))


if __name__ == '__main__':
    if len(sys.argv) > 2 and sys.argv[1] == '--probe':
        probe(sys.argv[2])
    else:
        unittest.main()
