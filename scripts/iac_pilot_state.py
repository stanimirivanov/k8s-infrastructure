"""Private files and one operation lock for the laptop kind rehearsal."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from .profile_transition_model import digest as digest
from .profile_transition_model import encoded as encoded
from .profile_transition_state import sync_directory

NAME = "perfeng-iac-pilot-74"
DOCKER_ENDPOINT = "unix:///var/run/docker.sock"
PACKAGE_DIGEST = "7c9d7a8817d0a3293e4a358d9ab88f16c69ef13c6479130e5a790c0b2d1a4e46"


def check_path(path: Path, *, directory: bool, owner: int, prohibited: int) -> None:
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("Expected a canonical #74 path without redirects")
    metadata = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        not kind(metadata.st_mode)
        or metadata.st_uid != owner
        or metadata.st_mode & prohibited
        or (not directory and metadata.st_nlink != 1)
    ):
        raise ValueError("Unexpected ownership or permissions on a #74 path")


def read_file(path: Path, maximum: int = 64 * 1024 * 1024) -> bytes:
    if path.resolve() != path:
        raise ValueError("Refusing redirected #74 file")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as file:
        metadata = os.fstat(file.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size > maximum
        ):
            raise ValueError("Expected a bounded ordinary #74 file")
        data = file.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("The #74 file exceeds its size limit")
    return data


def mapping(data: bytes | str) -> dict[str, Any]:
    result = json.loads(data)
    if not isinstance(result, dict):
        raise ValueError("Expected a #74 JSON object")
    return result


def uid(value: str) -> str:
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("Expected a canonical Kubernetes UID")
    return value


def fingerprint(path: Path) -> str:
    return digest(read_file(path)) if os.path.lexists(path) else "absent"


def store(path: Path, data: bytes) -> None:
    fd, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        if os.path.lexists(path):
            check_path(path, directory=False, owner=os.geteuid(), prohibited=0o077)
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def private_directory(path: Path) -> None:
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("Expected a canonical #74 state directory")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    check_path(path, directory=True, owner=os.geteuid(), prohibited=0o077)


@dataclass(frozen=True)
class Target:
    directory: Path
    name: str = NAME

    @property
    def state(self) -> Path:
        return self.directory / "cluster.tfstate"

    @property
    def kubeconfig(self) -> Path:
        return self.directory / "kubeconfig"

    @property
    def context(self) -> str:
        return f"kind-{self.name}"

    @contextmanager
    def locked(self) -> Iterator[int]:
        private_directory(self.directory)
        path = self.directory / "operation.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            check_path(path, directory=False, owner=os.geteuid(), prohibited=0o077)
            if not os.path.samestat(os.fstat(fd), path.lstat()):
                raise ValueError("The #74 operation lock changed")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("Another operation holds the #74 target lock") from None
            # The native mutation also holds the descriptor while it runs.
            yield fd
        finally:
            os.close(fd)
