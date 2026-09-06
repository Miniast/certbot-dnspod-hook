"""Private, atomic state files and a process lock for a single installation."""

import fcntl
import json
import os
import re
import stat
import tempfile
from pathlib import Path

from .config import HookError


class StateStore:
    def __init__(self, directory: Path):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
            raise HookError("State directory must be owned by the current user with mode 700")
        self.directory = directory

    def __enter__(self):
        fd = os.open(self.directory / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self.lock = os.fdopen(fd, "w")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise HookError(
                "Another hook is running with this state directory; retry later"
            ) from None
        return self

    def __exit__(self, *args):
        self.lock.close()

    def path(self, key: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            raise HookError("Invalid state ID; expected 64 lowercase hex characters")
        return self.directory / f"{key}.json"

    def read(self, key: str) -> dict | None:
        path = self.path(key)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        with os.fdopen(fd) as stream:
            return json.load(stream)

    def write(self, key: str, data: dict) -> None:
        fd, temporary = tempfile.mkstemp(prefix=".state-", dir=self.directory)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(data, stream)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path(key))
            self.sync()
        finally:
            Path(temporary).unlink(missing_ok=True)

    def remove(self, key: str) -> None:
        self.path(key).unlink(missing_ok=True)
        self.sync()

    def sync(self) -> None:
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
