"""OS-held lock shared by the Qt and web workers; released on process exit."""
import os


class ServiceLock:
    def __init__(self, path):
        self.file = open(path, 'a+b')
        try:
            if os.fstat(self.file.fileno()).st_size == 0:
                self.file.write(b'0')
                self.file.flush()
            self.file.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.file.close()
            raise RuntimeError('此目录已有监控后台运行，请退出 Qt 或其他 Web 实例后再启动。') from error

    def close(self):
        if not self.file.closed:
            self.file.close()
