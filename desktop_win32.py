"""Small tkinter-free Windows UI fallback.

It uses only user32/kernel32 through ctypes, so the packaged EXE starts even on
machines where Python was installed without Tcl/Tk.
"""
import ctypes
import threading
import time
import uuid
from ctypes import wintypes

import monitor

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
WM_TIMER = 0x0113
WM_SETFONT = 0x0030
WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_CHILD = 0x40000000
WS_VISIBLE = 0x10000000
WS_BORDER = 0x00800000
WS_VSCROLL = 0x00200000
ES_MULTILINE = 0x0004
ES_READONLY = 0x0800
ES_AUTOVSCROLL = 0x0040
LBS_NOTIFY = 0x0001
LB_RESETCONTENT = 0x0184
LB_ADDSTRING = 0x0180
LB_GETCURSEL = 0x0188
LB_ERR = -1
SW_SHOW = 5
ID_TASKS, ID_LOG, ID_MESSAGE, ID_SEND, ID_REFRESH, ID_EXIT, ID_STATUS = range(100, 107)


class WNDCLASS(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", ctypes.WINFUNCTYPE(ctypes.c_long, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


class NativeApp:
    def __init__(self):
        self.stop = threading.Event()
        self.worker = None
        self.task_ids = []
        self.hwnd = None
        self.controls = {}
        self._proc = ctypes.WINFUNCTYPE(ctypes.c_long, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)(self._window_proc)

    def _text(self, hwnd, value):
        user32.SetWindowTextW(hwnd, str(value))

    def _create(self, cls, text, style, x, y, w, h, ident):
        hwnd = user32.CreateWindowExW(0, cls, text, style, x, y, w, h, self.hwnd, ident, kernel32.GetModuleHandleW(None), None)
        self.controls[ident] = hwnd
        return hwnd

    def _window_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TIMER:
            self.refresh()
        elif msg == WM_COMMAND:
            ident = wparam & 0xFFFF
            if ident == ID_REFRESH:
                self.refresh()
            elif ident == ID_SEND:
                self.send_message()
            elif ident == ID_EXIT:
                user32.DestroyWindow(hwnd)
        elif msg == WM_CLOSE:
            user32.DestroyWindow(hwnd)
        elif msg == WM_DESTROY:
            self.stop.set()
            user32.PostQuitMessage(0)
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def refresh(self):
        snap = monitor.snapshot()
        status = f"后台：{snap.get('connection', 'starting')}"
        self._text(self.controls.get(ID_STATUS), status)
        task_box = self.controls.get(ID_TASKS)
        if task_box:
            user32.SendMessageW(task_box, LB_RESETCONTENT, 0, 0)
            self.task_ids = []
            for task in (snap.get('controls') or {}).get('tasks', []):
                tid = task.get('threadId', '')
                self.task_ids.append(tid)
                label = f"{task.get('title') or tid[:12]} | {task.get('status', 'connecting')} | {task.get('mode', '-') }"
                user32.SendMessageW(task_box, LB_ADDSTRING, 0, label)
        lines = []
        for op in (snap.get('controls') or {}).get('operations', []):
            stamp = time.strftime('%m-%d %H:%M', time.localtime(op.get('stamp', 0)))
            lines.append(f"{stamp}  {op.get('action', '')}  {op.get('status', '')}  {op.get('message', '')}")
        self._text(self.controls.get(ID_LOG), '\r\n'.join(lines) or '暂无操作记录。')

    def send_message(self):
        box = self.controls.get(ID_TASKS)
        pos = user32.SendMessageW(box, LB_GETCURSEL, 0, 0) if box else LB_ERR
        edit = self.controls.get(ID_MESSAGE)
        length = user32.GetWindowTextLengthW(edit) if edit else 0
        buf = ctypes.create_unicode_buffer(length + 1)
        if edit:
            user32.GetWindowTextW(edit, buf, length + 1)
        if pos == LB_ERR or pos >= len(self.task_ids):
            self._text(self.controls.get(ID_STATUS), '请先选择任务')
            return
        controller = monitor.controller
        if controller is None:
            self._text(self.controls.get(ID_STATUS), '后台仍在启动')
            return
        try:
            controller.submit(self.task_ids[pos], 'message', buf.value, str(uuid.uuid4()))
            self._text(edit, '')
            self._text(self.controls.get(ID_STATUS), '消息已排队')
        except Exception as exc:
            self._text(self.controls.get(ID_STATUS), str(exc)[:180])

    def run(self):
        class_name = 'CodexMobileMonitorNative'
        wc = WNDCLASS()
        wc.lpfnWndProc = self._proc
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = class_name
        wc.hbrBackground = ctypes.cast(ctypes.c_void_p(6), wintypes.HBRUSH)
        user32.RegisterClassW(ctypes.byref(wc))
        self.hwnd = user32.CreateWindowExW(0, class_name, 'Codex 本机守护', WS_OVERLAPPEDWINDOW, 100, 100, 900, 620, None, None, wc.hInstance, None)
        self.controls[ID_STATUS] = self._create('STATIC', '后台：正在启动', WS_CHILD | WS_VISIBLE, 20, 18, 820, 24, ID_STATUS)
        self._create('LISTBOX', '', WS_CHILD | WS_VISIBLE | WS_BORDER | WS_VSCROLL | LBS_NOTIFY, 20, 52, 840, 270, ID_TASKS)
        self._create('EDIT', '', WS_CHILD | WS_VISIBLE | WS_BORDER | ES_MULTILINE | ES_READONLY | ES_AUTOVSCROLL | WS_VSCROLL, 20, 335, 840, 130, ID_LOG)
        self._create('EDIT', '', WS_CHILD | WS_VISIBLE | WS_BORDER, 20, 480, 650, 28, ID_MESSAGE)
        self._create('BUTTON', '发送消息', WS_CHILD | WS_VISIBLE, 680, 480, 90, 28, ID_SEND)
        self._create('BUTTON', '刷新', WS_CHILD | WS_VISIBLE, 780, 480, 80, 28, ID_REFRESH)
        self._create('BUTTON', '退出', WS_CHILD | WS_VISIBLE, 780, 520, 80, 28, ID_EXIT)
        user32.ShowWindow(self.hwnd, SW_SHOW)
        user32.SetTimer(self.hwnd, 1, 1200, None)
        _stop, self.worker = monitor.start_background_service(self.stop)
        self.refresh()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        self.stop.set()
        if self.worker:
            self.worker.join(timeout=8)
        return 0


def main():
    return NativeApp().run()


if __name__ == '__main__':
    raise SystemExit(main())
