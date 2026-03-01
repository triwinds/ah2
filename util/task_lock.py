import os
import threading
from contextlib import contextmanager
from pathlib import Path

if os.name == 'nt':
    import msvcrt
else:
    import fcntl


class TaskExecutionLock:
    """Cross-process + in-process non-blocking lock for scheduled tasks."""

    _locks_guard = threading.Lock()
    _locks = {}

    def __init__(self, lock_path):
        self.lock_path = Path(lock_path)
        key = str(self.lock_path.expanduser().resolve(strict=False))
        with self._locks_guard:
            self._thread_lock = self._locks.setdefault(key, threading.Lock())
        self._file = None
        self._acquired = False

    def acquire(self):
        if not self._thread_lock.acquire(blocking=False):
            return False

        handle = None
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(self.lock_path, 'a+b')
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b'\0')
                handle.flush()
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if handle is not None:
                handle.close()
            self._thread_lock.release()
            return False

        self._file = handle
        self._acquired = True
        return True

    def release(self):
        if not self._acquired:
            return

        try:
            if self._file is not None:
                try:
                    self._file.seek(0)
                    if os.name == 'nt':
                        msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
                finally:
                    self._file.close()
        finally:
            self._file = None
            self._acquired = False
            self._thread_lock.release()


@contextmanager
def task_execution_lock(lock_path):
    lock = TaskExecutionLock(lock_path)
    acquired = lock.acquire()
    try:
        yield acquired
    finally:
        if acquired:
            lock.release()
