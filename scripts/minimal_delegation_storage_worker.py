"""Neutral filesystem witness executed inside the capped disposable probe Pod."""

import errno
import fcntl
import os
import sqlite3
import stat
import sys
import time
from contextlib import closing
from pathlib import Path

NO_LINK = os.O_NOFOLLOW | os.O_NONBLOCK


def regular(info: os.stat_result) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_gid != os.getegid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise ValueError("Neutral checkpoint requires an owned private single-link regular file")


def open_regular(directory: int, name: str, flags: int) -> int:
    descriptor = os.open(name, flags | NO_LINK, 0o600, dir_fd=directory)
    try:
        regular(os.fstat(descriptor))
    except ValueError:
        os.close(descriptor)
        raise
    return descriptor


def write_private(directory: int, name: str, data: bytes) -> None:
    with os.fdopen(
        open_regular(directory, name, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "wb"
    ) as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())


def contend(database: sqlite3.Connection, lock: int) -> None:
    try:
        fcntl.lockf(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno not in {errno.EACCES, errno.EAGAIN}:
            raise
    else:
        raise ValueError("A competing POSIX writer acquired the held lock")
    try:
        database.execute("BEGIN EXCLUSIVE")
    except sqlite3.OperationalError as error:
        if error.sqlite_errorcode not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
            raise
    else:
        raise ValueError("A competing SQLite writer acquired the held lock")


def exercise(root: Path, action: str, marker: str) -> None:
    """Check lock exclusion, strict file identity and replacement durability."""
    path = root / "qualification"
    if action == "writer":
        path.mkdir(mode=0o700)
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        details = os.fstat(directory)
        if (
            details.st_uid != os.geteuid()
            or details.st_gid != os.getegid()
            or stat.S_IMODE(details.st_mode) != 0o700
        ):
            raise ValueError("Neutral checkpoint directory ownership or mode differs")
        if action == "release":
            write_private(directory, "release", b"")
            return
        if action == "writer":
            write_private(directory, "writer.sqlite", b"")
            write_private(directory, "posix.lock", b"")
        before = os.stat("writer.sqlite", dir_fd=directory, follow_symlinks=False)
        regular(before)
        # Do not open the SQLite file through any other connection/descriptor in
        # this process: unrelated closes can release SQLite's POSIX locks.
        uri = f"file:/proc/self/fd/{directory}/writer.sqlite?mode=rw"
        with (
            closing(sqlite3.connect(uri, uri=True, timeout=0, isolation_level=None)) as database,
            os.fdopen(open_regular(directory, "posix.lock", os.O_RDWR), "rb+") as posix,
        ):
            after = os.stat("writer.sqlite", dir_fd=directory, follow_symlinks=False)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise ValueError("SQLite writer inode changed")
            if action == "contender":
                contend(database, posix.fileno())
                return
            fcntl.lockf(posix.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            database.execute("PRAGMA locking_mode=EXCLUSIVE")
            database.execute("PRAGMA synchronous=FULL")
            database.execute("BEGIN EXCLUSIVE")
            if action == "writer":
                database.execute("CREATE TABLE neutral_state (marker TEXT NOT NULL)")
                database.execute("INSERT INTO neutral_state VALUES (?)", (marker,))
                database.execute("COMMIT")
                database.execute("BEGIN EXCLUSIVE")
                write_private(directory, "checkpoint.next", marker.encode())
                os.replace(
                    "checkpoint.next", "checkpoint", src_dir_fd=directory, dst_dir_fd=directory
                )
                os.fsync(directory)
                write_private(directory, "ready", b"")
                deadline = time.monotonic() + 240
                while not (path / "release").exists():
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Neutral writer did not receive the bounded release")
                    time.sleep(0.1)
            elif action == "replacement":
                with os.fdopen(
                    open_regular(directory, "checkpoint", os.O_RDONLY), "rb"
                ) as checkpoint:
                    if checkpoint.read(1024) != marker.encode() or database.execute(
                        "SELECT marker FROM neutral_state"
                    ).fetchall() != [(marker,)]:
                        raise ValueError("Replacement did not read the persisted neutral state")
            else:
                raise ValueError("Unknown filesystem probe action")
            database.execute("ROLLBACK")
    finally:
        os.close(directory)


def main() -> None:
    if (os.geteuid(), os.getegid()) != (65532, 65532):
        raise ValueError("Expected the simulator UID and GID")
    mounts = {
        after.split()[0]
        for before, after in (
            line.split(" - ", 1) for line in Path("/proc/self/mountinfo").read_text().splitlines()
        )
        if before.split()[4] == "/state"
    }
    if mounts != {"ext4"}:
        raise ValueError("Mounted state requires the reviewed local Linux ext4 filesystem")
    os.umask(0o077)
    exercise(Path("/state"), sys.argv[1], sys.argv[2])


if __name__ == "__main__":
    main()
