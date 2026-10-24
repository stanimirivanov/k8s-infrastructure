"""Durable private transition journal, process locking and ordinary-lifecycle guard."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .consumer_profile import Target
from .profile_transition_model import (
    FORMAT,
    PHASES,
    ROLES,
    STAGES,
    digest,
    encoded,
    validate_request,
)


def read(path: Path, maximum: int = 131072) -> bytes:
    """Read bounded ordinary state without exposing contents in diagnostics."""
    metadata = path.lstat()
    if not path.is_file() or path.is_symlink() or path.is_junction() or metadata.st_size > maximum:
        raise ValueError("Expected bounded ordinary private transition state")
    with path.open("rb") as handle:
        data = handle.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("Private transition state grew during bounded read")
    return data


def sync_directory(path: Path) -> None:
    if os.name != "nt":
        directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def exists(path: Path) -> bool:
    return os.path.lexists(path)


def exclusive(path: Path, data: bytes) -> None:
    if len(data) > 131072:
        raise ValueError("Private transition state exceeds the bounded size")
    with path.open("xb") as handle:
        path.chmod(0o600)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    sync_directory(path.parent)


def replace(path: Path, data: bytes, expected: str) -> None:
    """Hash-guard a same-directory atomic replacement; never erase stale guard files."""
    if len(data) > 131072:
        raise ValueError("Private transition state exceeds the bounded size")
    if digest(read(path)) != expected:
        raise ValueError("Private state changed before guarded replacement")
    descriptor, name = tempfile.mkstemp(prefix=f"{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if digest(read(path)) != expected:
            raise ValueError("Private state changed during guarded replacement")
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def reject_pending(target: Target) -> None:
    """All ordinary lifecycle paths stop at an incomplete or malformed transition."""
    path = target.path("transition.json")
    if not exists(path):
        return
    value = json.loads(read(path))
    if (
        not isinstance(value, dict)
        or value.get("format") != FORMAT
        or value.get("phase") != "complete"
    ):
        raise ValueError("Retained profile transition is unfinished; explicit recovery required")
    from .consumer_profile import identity
    from .profile_transition_model import PREDECESSOR, paired

    predecessor = Target(
        target.consumer, target.docker_context, target.state_root, PREDECESSOR, target.https_port
    )
    try:
        journal = load(predecessor, value["request"])
    except (ValueError, TypeError, KeyError):
        raise ValueError(
            "Completed transition journal is malformed; inspect private state"
        ) from None
    before = json.loads(journal["predecessorOwner"])
    expected = {**before, **identity(paired(predecessor))}
    current = json.loads(read(target.path("owner.json")))
    # Normal deletion changes phase only. It must not invalidate retained history.
    current["phase"] = expected["phase"]
    if current != expected:
        raise ValueError("Completed transition no longer binds the original successor owner")


@contextmanager
def locked(target: Target):
    """OS lock releases on process death; lock files are retained, never force-removed."""
    path = target.path("transition.lock")
    if not path.parent.is_dir():
        raise ValueError("Transition requires an existing private ownership directory")
    if not exists(path):
        try:
            exclusive(path, b"0")
        except FileExistsError:
            pass
    if not path.is_file() or path.stat().st_size != 1:
        raise ValueError("Unexpected transition lock file")
    with path.open("r+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("Another transition operation holds the target lock") from None
        try:
            if handle.read(1) != b"0":
                raise ValueError("Unexpected transition lock file")
            handle.seek(0)
            yield
        finally:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load(target: Target, request: dict) -> dict:
    value = json.loads(read(target.path("transition.json")))
    required = {
        "format",
        "request",
        "requestHash",
        "phase",
        "stages",
        "predecessorOwner",
        "predecessorOwnerHash",
        "kindHash",
        "kubeconfigHash",
        "foundation",
        "desiredHashes",
        "repairBackupHashes",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value["format"] != FORMAT
        or value["phase"] not in PHASES
        or value["request"] != request
        or value["requestHash"] != digest(encoded(validate_request(target, request)))
        or set(value["stages"]) != set(ROLES)
        or any(s not in STAGES for s in value["stages"].values())
        or set(value["desiredHashes"]) != set(ROLES)
        or set(value["repairBackupHashes"]) != set(ROLES)
    ):
        raise ValueError("Transition journal does not match reviewed operation")
    import re

    if (
        digest(value["predecessorOwner"].encode()) != value["predecessorOwnerHash"]
        or any(
            not isinstance(h, str) or not re.fullmatch("[a-f0-9]{64}", h)
            for h in (
                value["kindHash"],
                value["kubeconfigHash"],
                *value["desiredHashes"].values(),
                *value["repairBackupHashes"].values(),
            )
        )
        or (
            value["phase"] in {"finalizing", "complete"}
            and any(stage != "verified" for stage in value["stages"].values())
        )
        or (
            value["phase"] in {"prepared", "backups"}
            and any(stage != "pending" for stage in value["stages"].values())
        )
    ):
        raise ValueError("Transition journal phase or preserved hashes are inconsistent")
    return value


def save(target: Target, journal: dict) -> None:
    path = target.path("transition.json")
    before = read(path)
    current = json.loads(before)
    if current["requestHash"] != journal["requestHash"]:
        raise ValueError("Transition operation changed")
    replace(path, encoded(journal), digest(before))
