"""One network-heavy job at a time (poll, discover-batch, details): two processes would each
keep their own per-host spacing and so double the request rate to the same ATS hosts."""
import fcntl
import os
import time

from . import config


class Busy(Exception):
    pass


class RunLock:
    def __init__(self, name="run", wait=0):
        self.path = os.path.join(config.DATA, name + ".lock")
        self.wait = wait
        self.f = None

    def __enter__(self):
        config.ensure_dirs()
        self.f = open(self.path, "a+")
        deadline = time.time() + self.wait
        while True:
            try:
                fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.f.seek(0)
                self.f.truncate()
                self.f.write("%d\n" % os.getpid())
                self.f.flush()
                return self
            except BlockingIOError:
                if time.time() >= deadline:
                    self.f.close()
                    raise Busy("another JobAtlas job holds %s" % self.path)
                time.sleep(10)

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.f, fcntl.LOCK_UN)
        finally:
            self.f.close()
