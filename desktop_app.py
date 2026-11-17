"""Windows desktop entry point: Qt workspace with limited Tk/Win32 fallbacks.

The desktop process owns the background workers and their shutdown lifecycle.
"""
import ctypes
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

if getattr(sys, "frozen", False):
    _bundle = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    os.environ.setdefault("TCL_LIBRARY", str(_bundle / "tcl" / "tcl8.6"))
    os.environ.setdefault("TK_LIBRARY", str(_bundle / "tcl" / "tk8.6"))

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except ImportError:  # Some Python installations omit Tk; use the Win32 fallback.
    tk = None
    messagebox = None
    ttk = None

import monitor


class SingleInstance:
    """A named Windows mutex prevents two desktop monitors from racing."""
    def __init__(self, name="CodexMobileMonitor.Desktop"):
        self.handle = None
        if sys.platform == "win32":
            self.handle = ctypes.windll.kernel32.CreateMutexW(None, False, name)
            if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
                if self.handle:
                    ctypes.windll.kernel32.CloseHandle(self.handle)
                self.handle = None
                raise RuntimeError("Codex Mobile Monitor 已经在运行。")

    def close(self):
        if self.handle:
            ctypes.windll.kernel32.ReleaseMutex(self.handle)
            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None


class DesktopApp:
    def __init__(self, root):
        self.root = root
        self.stop = threading.Event()
        self.selected_thread = None
        self.instance = None
        self.worker = None
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh()

    def _build_ui(self):
        self.root.title("Codex 本机守护")
        self.root.geometry("1020x700")
        self.root.minsize(820, 560)
        self.root.option_add("*Font", ("Microsoft YaHei UI", 10))

        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer)
        header.pack(fill="x")
        ttk.Label(header, text="Codex 本机守护", font=("Microsoft YaHei UI", 18, "bold")).pack(side="left")
        self.status = ttk.Label(header, text="正在启动…")
        self.status.pack(side="right", pady=5)

        note = ttk.Label(outer, text="原生桌面程序运行中；关闭 Codex 窗口不会关闭此守护进程。", foreground="#555")
        note.pack(anchor="w", pady=(4, 12))

        body = ttk.Panedwindow(outer, orient="vertical")
        body.pack(fill="both", expand=True)
        top = ttk.Frame(body, padding=(0, 0, 0, 8))
        bottom = ttk.Frame(body)
        body.add(top, weight=4)
        body.add(bottom, weight=2)

        task_box = ttk.LabelFrame(top, text="任务")
        task_box.pack(fill="both", expand=True)
        columns = ("title", "id", "state", "mode", "goal")
        self.tasks = ttk.Treeview(task_box, columns=columns, show="headings", selectmode="browse")
        headings = {"title": "任务", "id": "任务 ID", "state": "状态", "mode": "连接模式", "goal": "目标"}
        widths = {"title": 220, "id": 270, "state": 130, "mode": 110, "goal": 130}
        for col in columns:
            self.tasks.heading(col, text=headings[col])
            self.tasks.column(col, width=widths[col], anchor="w")
        scroll = ttk.Scrollbar(task_box, orient="vertical", command=self.tasks.yview)
        self.tasks.configure(yscrollcommand=scroll.set)
        self.tasks.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tasks.bind("<<TreeviewSelect>>", self._select_task)

        controls = ttk.Frame(top)
        controls.pack(fill="x", pady=(8, 0))
        for label, action in (("接入", "attach"), ("移除守护", "detach"), ("暂停", "pause"), ("继续", "resume")):
            ttk.Button(controls, text=label, command=lambda a=action: self.submit(a)).pack(side="left", padx=(0, 6))
        ttk.Button(controls, text="创建测试任务", command=lambda: self.submit("create_test")).pack(side="left", padx=(10, 6))
        ttk.Button(controls, text="刷新", command=self.refresh).pack(side="right")

        message_bar = ttk.Frame(top)
        message_bar.pack(fill="x", pady=(8, 0))
        ttk.Label(message_bar, text="发送消息").pack(side="left")
        self.message = ttk.Entry(message_bar)
        self.message.pack(side="left", fill="x", expand=True, padx=8)
        self.message.bind("<Return>", lambda _event: self.submit("message"))
        ttk.Button(message_bar, text="发送", command=lambda: self.submit("message")).pack(side="right")
        self.action_message = ttk.Label(top, text="")
        self.action_message.pack(anchor="w", pady=(5, 0))

        approval_bar = ttk.Frame(top)
        approval_bar.pack(fill="x", pady=(6, 0))
        ttk.Label(approval_bar, text="待审批").pack(side="left")
        self.approval_choice = ttk.Combobox(approval_bar, state="readonly", width=44)
        self.approval_choice.pack(side="left", padx=8)
        ttk.Button(approval_bar, text="批准", command=lambda: self.decide_approval(True)).pack(side="left", padx=(0, 6))
        ttk.Button(approval_bar, text="拒绝", command=lambda: self.decide_approval(False)).pack(side="left")

        log_box = ttk.LabelFrame(bottom, text="最近操作")
        log_box.pack(fill="both", expand=True)
        self.log = tk.Text(log_box, height=7, state="disabled", wrap="word", relief="flat", background="#f7f7f7")
        self.log.pack(fill="both", expand=True, padx=6, pady=6)

    def _select_task(self, _event=None):
        selected = self.tasks.selection()
        self.selected_thread = selected[0] if selected else None

    def _set_log(self, text):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.insert("end", text or "暂无操作记录。")
        self.log.configure(state="disabled")

    def _task_data(self):
        snap = monitor.snapshot()
        controls = snap.get("controls") or {}
        return snap, controls.get("tasks") or []

    def refresh(self):
        if not self.root.winfo_exists():
            return
        snap, tasks = self._task_data()
        self.tasks.delete(*self.tasks.get_children())
        for task in tasks:
            tid = task.get("threadId", "")
            status = task.get("status", "connecting")
            mode = task.get("mode", "-")
            goal = task.get("goalStatus") or "未知"
            title = task.get("title") or tid[:12] or "未命名任务"
            self.tasks.insert("", "end", iid=tid, values=(title, tid, status, mode, goal))
        if self.selected_thread in self.tasks.get_children():
            self.tasks.selection_set(self.selected_thread)
        else:
            self.selected_thread = None
        connection = snap.get("connection", "starting")
        service = snap.get("serviceStatus", "")
        self.status.configure(text=f"后台：{connection}" + (f"（{service}）" if service else ""))
        operations = (snap.get("controls") or {}).get("operations") or []
        lines = []
        for op in operations:
            stamp = time.strftime("%m-%d %H:%M", time.localtime(op.get("stamp", 0)))
            lines.append(f"{stamp}  {op.get('action', '')}  {op.get('status', '')}  {op.get('message', '')}")
        self._set_log("\n".join(lines))
        approvals = monitor.approvals.public()
        approval_values = [f"{a.get('token', '')}  {a.get('method', '权限请求')}" for a in approvals if a.get('decision') is None]
        self.approval_choice["values"] = approval_values
        if approval_values and self.approval_choice.get() not in approval_values:
            self.approval_choice.current(0)
        elif not approval_values:
            self.approval_choice.set("")
        self.root.after(1200, self.refresh)

    def decide_approval(self, allowed):
        value = self.approval_choice.get().strip()
        token = value.split("  ", 1)[0] if value else ""
        if not token:
            self.action_message.configure(text="当前没有待审批请求。")
            return
        result = monitor.approvals.decide(token, allowed)
        self.action_message.configure(text=("已批准。" if result else "已拒绝或请求已失效。"))

    def submit(self, action):
        if action != "create_test" and not self.selected_thread:
            self.action_message.configure(text="请先选择一个任务。")
            return
        if action == "message" and not self.message.get().strip():
            self.action_message.configure(text="请输入消息。")
            return
        controller = monitor.controller
        if controller is None:
            self.action_message.configure(text="后台仍在启动，请稍候。")
            return
        try:
            operation = controller.submit(
                self.selected_thread,
                action,
                self.message.get() if action == "message" else "",
                str(uuid.uuid4()),
            )
            if action == "message":
                self.message.delete(0, "end")
            self.action_message.configure(text=f"操作已排队：{operation}")
        except Exception as exc:
            self.action_message.configure(text=str(exc))

    def close(self):
        self.stop.set()
        self._close_deadline = time.monotonic() + 8
        self.root.after(50, self._finish_close)

    def _finish_close(self):
        if self.worker and self.worker.is_alive() and time.monotonic() < self._close_deadline:
            self.root.after(50, self._finish_close)
            return
        if self.instance:
            self.instance.close()
        self.root.destroy()


def main():
    try:
        from desktop_qt import main as qt_main
        return qt_main()
    except ImportError:
        pass
    if tk is None:
        from desktop_win32 import main as native_main
        return native_main()
    try:
        instance = SingleInstance()
    except RuntimeError as exc:
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo("Codex 本机守护", str(exc))
        root.destroy()
        return 1
    root = tk.Tk()
    app = DesktopApp(root)
    app.instance = instance
    _stop, app.worker = monitor.start_background_service(app.stop)
    try:
        root.mainloop()
    finally:
        app.stop.set()
        if app.worker:
            app.worker.join(timeout=8)
        instance.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
