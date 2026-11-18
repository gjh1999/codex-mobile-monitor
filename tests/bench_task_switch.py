"""Measure task selection with a repeatable journal delay and long conversations."""
import argparse
import faulthandler
import json
import statistics
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

import monitor
from desktop_qt import MainWindow
from smoke_desktop import FakeControl, NoWorkers, Shared


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--delay-ms', type=int, default=140)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    tasks = {f'00000000-0000-4000-8000-{i:012}': f'切换基准 {i}' for i in range(1, 4)}
    ids = list(tasks)
    controller = FakeControl(ids[0], tasks[ids[0]], False)
    payloads = {tid: {'messages': [{'role': 'assistant', 'text': f'{title}：第 {i} 条\n\n' + ('项目记录 ' * 680)}
                                  for i in range(35)], 'events': []} for tid, title in tasks.items()}

    def history(tid, _):
        time.sleep(args.delay_ms / 1000)
        return payloads[tid]

    app = QApplication.instance() or QApplication([])
    snapshot = monitor.snapshot()
    monitor.update(connection='connected', threads=[{'id': tid, 'title': title} for tid, title in tasks.items()])
    window = None
    samples = []
    try:
        with patch.object(monitor, 'controller', controller), patch.object(monitor, 'conversation_for', history), \
             patch.object(monitor, 'start_background_service', side_effect=lambda stop: (stop, NoWorkers())), \
             patch.dict('os.environ', CODEX_HOME=str(args.output.resolve() / 'fixture-home')):
            window = MainWindow(Shared())
            window.timer.stop()
            window.show()
            initial = QTimer()
            initial.setSingleShot(True)
            initial.timeout.connect(app.quit)
            initial.start(180)
            app.exec()
            for tid in (ids[1], ids[2], ids[0], ids[1], ids[2], ids[0]):
                began = time.perf_counter()
                event_loop = []
                QTimer.singleShot(0, lambda: event_loop.append((time.perf_counter() - began) * 1000))
                window.task_choice.setCurrentItem(window.task_items[tid])
                selection = (time.perf_counter() - began) * 1000
                poll = QTimer()
                def check():
                    if ((event_loop and tasks[tid] in window.conversation.toPlainText())
                            or time.perf_counter() - began > 8):
                        app.quit()
                poll.timeout.connect(check)
                poll.start(5)
                app.exec()
                poll.stop()
                if tasks[tid] not in window.conversation.toPlainText():
                    faulthandler.dump_traceback()
                    raise RuntimeError(f'conversation did not finish loading for {tasks[tid]}: '
                                       f'{window.conversation.toPlainText()[:160]}')
                samples.append(dict(selection_ms=round(selection, 2), event_loop_ms=round(event_loop[0], 2),
                                    history_ms=round((time.perf_counter() - began) * 1000, 2)))
    finally:
        if window is not None:
            window.shutdown()
            window.close()
        monitor.update(**snapshot)
    medians = {key: round(statistics.median(s[key] for s in samples), 2) for key in samples[0]}
    data = dict(delay_ms=args.delay_ms, messages=35, samples=samples, median=medians)
    (args.output / 'result.json').write_text(json.dumps(data, indent=2), encoding='utf-8')
    print(json.dumps(data))


if __name__ == '__main__':
    main()
