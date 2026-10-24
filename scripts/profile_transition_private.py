"""Fixed native node scripts for versioned backups and recoverable guarded writes."""

from __future__ import annotations

import base64
import re

import yaml

from . import consumer_runtime as runtime
from .consumer_capacity import read_configuration
from .consumer_profile import Target
from .profile_transition_model import digest, encoded

# Only fixed scripts and validated positional hex arguments enter the shell.
# No private configuration is interpolated into shell text or printed by callers.
PREFIX = """set -eu
umask 077
set -C
case "$1" in *[!a-f0-9]*|'') exit 1;; esac
test "${#1}" -eq 32
p=/var/lib/kubelet/config.yaml
b=/var/lib/kubelet/config.profile-transition-$1.before.yaml
n=/var/lib/kubelet/config.profile-transition-$1.next
test ! -L /var/lib/kubelet
test ! -L "$p"
test -f "$p"
test "$(stat -c %s "$p")" -le 65536
test ! -L "$b"
test ! -L "$n"
"""
REPAIR_HASH = (
    PREFIX
    + """o=/var/lib/kubelet/config.before-allocatable.yaml
test ! -L "$o"
test -f "$o"
test "$(stat -c %s "$o")" -le 65536
sha256sum "$o"
"""
)
PREFLIGHT = (
    PREFIX
    + """test ! -e "$b"
test ! -e "$n"
test "$(sha256sum "$p" | cut -d ' ' -f 1)" = "$2"
"""
)
BACKUP = (
    PREFIX
    + """test ! -e "$n"
test "$(sha256sum "$p" | cut -d ' ' -f 1)" = "$2"
if test -e "$b"; then
    test "$3" = recover
    test -f "$b"
    test "$(stat -c %s "$b")" -le 65536
    test "$(stat -c %a "$b")" = 600
    test "$(sha256sum "$b" | cut -d ' ' -f 1)" = "$2"
else
    cat "$p" > "$b"
    chmod 600 "$b"
    test "$(sha256sum "$b" | cut -d ' ' -f 1)" = "$2"
fi
"""
)
WRITE = (
    PREFIX
    + """test -f "$b"
test "$(stat -c %s "$b")" -le 65536
test "$(stat -c %a "$b")" = 600
test "$(sha256sum "$b" | cut -d ' ' -f 1)" = "$2"
current=$(sha256sum "$p" | cut -d ' ' -f 1)
if test "$current" = "$3"; then
    test ! -e "$n"
    exit 0
fi
test "$current" = "$2"
if test -e "$n"; then
    test "$4" = recover
    test -f "$n"
else
    cat > "$n"
    chmod 600 "$n"
fi
test "$(stat -c %s "$n")" -le 65536
test "$(sha256sum "$n" | cut -d ' ' -f 1)" = "$3"
chmod 600 "$n"
test ! -L "$p"
test "$(sha256sum "$p" | cut -d ' ' -f 1)" = "$2"
mv -T -- "$n" "$p"
"""
)
VERIFY_PATHS = (
    PREFIX
    + """test -f "$b"
test "$(stat -c %s "$b")" -le 65536
test "$(stat -c %a "$b")" = 600
test "$(sha256sum "$b" | cut -d ' ' -f 1)" = "$2"
if test "$3" != write-intent; then
    test ! -e "$n"
fi
"""
)
READ_BACKUP = (
    PREFIX
    + """test -f "$b"
test "$(stat -c %s "$b")" -le 65536
test "$(stat -c %a "$b")" = 600
sha256sum "$b"
base64 --wrap=0 "$b"
"""
)


def execute(
    target: Target,
    node_id: str,
    script: str,
    operation: str,
    *arguments: str,
    data: bytes | None = None,
) -> str:
    if not re.fullmatch("[a-f0-9]{64}", node_id) or not re.fullmatch("[a-f0-9]{32}", operation):
        raise ValueError("Invalid immutable node/operation identity")
    return runtime.command(
        target,
        [
            "docker",
            "--context",
            target.docker_context,
            "exec",
            *(["-i"] if data is not None else []),
            node_id,
            "sh",
            "-ceu",
            script,
            "profile-transition",
            operation,
            *arguments,
        ],
        input_data=data,
    )


def backup_configuration(target: Target, node_id: str, operation: str, expected: str) -> dict:
    raw = execute(target, node_id, READ_BACKUP, operation)
    line, separator, body = raw.partition("\n")
    if not separator or not line.split() or line.split()[0] != expected:
        raise ValueError("Versioned predecessor backup does not match reviewed hash")
    content = base64.b64decode(body, validate=True)
    if len(content) > 65536 or digest(content) != expected:
        raise ValueError("Versioned predecessor backup changed during read")
    value = yaml.safe_load(content)
    if not isinstance(value, dict):
        raise ValueError("Invalid predecessor backup configuration")
    return value


def current_hash(target: Target, node_id: str) -> str:
    return read_configuration(target, node_id)[0]


def write(target: Target, node: dict, operation: str, desired: dict, *, recovery: bool) -> None:
    execute(
        target,
        node["containerId"],
        WRITE,
        operation,
        node["oldConfigSha256"],
        digest(encoded(desired)),
        "recover" if recovery else "apply",
        data=encoded(desired),
    )


def repair_hash(target: Target, node_id: str, operation: str) -> str:
    output = execute(target, node_id, REPAIR_HASH, operation).split()
    if not output or not re.fullmatch("[a-f0-9]{64}", output[0]):
        raise ValueError("Cannot bind original repair backup")
    return output[0]
