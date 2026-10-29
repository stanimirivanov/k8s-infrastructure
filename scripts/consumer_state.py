"""Private ownership records for independently selected local consumer targets."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

import yaml

from .consumer_profile import Target, configuration, identity

FIELDS = {"nonce", "endpoint", "phase", "nodeIds", "incarnation"}
PHASES = {"creating", "provisioned", "ready", "partial", "deleting"}


def read_bounded(path: Path, maximum: int = 65536) -> str:
    """Read an ordinary owned file without disclosing private bytes in errors."""
    stat = path.lstat()
    if not path.is_file() or path.is_symlink() or stat.st_size > maximum:
        raise ValueError("Expected bounded ordinary consumer state")
    return path.read_text(encoding="utf-8")


def reserve(target: Target, endpoint: str) -> dict[str, Any]:
    """Exclusive files establish a claim before kind can create the unique target."""
    from .profile_transition_state import reject_pending

    reject_pending(target)
    paths = [target.path(suffix) for suffix in ("owner.json", "kind.yaml", "kubeconfig")]
    if any(path.exists() for path in paths):
        raise ValueError("Consumer state exists; refusing replacement or implicit adoption")
    target.directory.mkdir(mode=0o700, exist_ok=True)
    record = {
        **identity(target),
        "nonce": str(uuid.uuid4()),
        "endpoint": endpoint,
        "phase": "creating",
        "nodeIds": {},
        "incarnation": None,
    }
    # The record is reserved first; interrupted reservations remain explicit partial state.
    for path, text in [
        (paths[0], json.dumps(record, sort_keys=True)),
        (paths[1], yaml.safe_dump(configuration(target))),
        (paths[2], ""),
    ]:
        with path.open("x", encoding="utf-8") as handle:
            path.chmod(0o600)
            handle.write(text)
    return record


def load(target: Target) -> dict[str, Any]:
    """A matching name/kubeconfig without this exact ownership record is never adopted."""
    from .profile_transition_state import reject_pending

    reject_pending(target)
    record = json.loads(read_bounded(target.path("owner.json"), 16384))
    expected = identity(target)
    if (
        not isinstance(record, dict)
        or set(record) != set(expected) | FIELDS
        or any(record.get(key) != value for key, value in expected.items())
        or record.get("phase") not in PHASES
        or not isinstance(record.get("nodeIds"), dict)
    ):
        raise ValueError("Consumer ownership/profile/context mismatch")
    try:
        uuid.UUID(record["nonce"])
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError("Invalid consumer creation identity") from error
    if yaml.safe_load(read_bounded(target.path("kind.yaml"))) != configuration(target):
        raise ValueError("Consumer configuration changed after reservation")
    return record


def save(target: Target, record: dict[str, Any]) -> None:
    """Atomic same-directory replacement preserves the owner claim on interruption."""
    path = target.path("owner.json")
    current = load(target)
    if current["nonce"] != record["nonce"]:
        raise ValueError("Consumer creation identity changed")
    descriptor, temporary = tempfile.mkstemp(prefix=f"{target.name}.", dir=target.directory)
    pending = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(record, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


def remove(target: Target) -> None:
    """Remove only the three verified target files, preserving all other consumer state."""
    load(target)
    for suffix in ("kubeconfig", "kind.yaml", "owner.json"):
        target.path(suffix).unlink(missing_ok=True)
