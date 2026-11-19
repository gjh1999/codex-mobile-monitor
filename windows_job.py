"""Windows process-tree ownership for the compute supervisor."""
import ctypes
import time
from ctypes import wintypes


class BasicLimit(ctypes.Structure):
    _fields_ = [
        ('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
        ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
        ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
        ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD), ('SchedulingClass', wintypes.DWORD),
    ]


class ExtendedLimit(ctypes.Structure):
    _fields_ = [('BasicLimitInformation', BasicLimit), ('IoInfo', ctypes.c_uint64 * 6),
                ('MemoryLimitsAndPeaks', ctypes.c_size_t * 4)]


class Accounting(ctypes.Structure):
    _fields_ = [('Times', ctypes.c_int64 * 4), ('PageFaults', wintypes.DWORD),
                ('TotalProcesses', wintypes.DWORD), ('ActiveProcesses', wintypes.DWORD),
                ('TotalTerminatedProcesses', wintypes.DWORD)]


class WindowsJob:
    def __init__(self):
        api = self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        api.CreateJobObjectW.restype = wintypes.HANDLE
        api.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        api.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        api.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        api.QueryInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                                   ctypes.c_void_p)
        api.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        api.OpenProcess.restype = wintypes.HANDLE
        api.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.handle = api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimit()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, pid):
        process = self.api.OpenProcess(0x101, False, pid)  # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self.api.AssignProcessToJobObject(self.handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self.api.CloseHandle(process)

    def stop(self):
        if not self.api.TerminateJobObject(self.handle, 124):
            raise ctypes.WinError(ctypes.get_last_error())
        deadline = time.monotonic() + 5
        counts = Accounting()
        while True:
            if not self.api.QueryInformationJobObject(self.handle, 1, ctypes.byref(counts), ctypes.sizeof(counts), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if not counts.ActiveProcesses:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError('计算进程树尚未退出')
            time.sleep(0.02)

    def close(self):
        self.api.CloseHandle(self.handle)
