"""An OS lock prevents two workers from sharing one storage directory."""

import os


class WorkerLock:
    def __init__(self, path):
        self.path = path
        self.handle = None

    def acquire(self):
        handle = self.path.open("a+b")
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"1")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError("已有服务正在使用此 storage 目录，请仅启动一个服务进程。") from exc
        self.handle = handle

    def release(self):
        if self.handle:
            self.handle.close()
            self.handle = None
