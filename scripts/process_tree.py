"""Bounded termination of only a subprocess tree created by this command adapter."""

from __future__ import annotations

import os
import signal
import subprocess


def terminate(process: subprocess.Popen[bytes]) -> None:
    """Close inherited child pipes without waiting forever after killing the parent.

    Callers create a new POSIX session. Never enumerate
    or kill by executable name, Docker context or infrastructure resource label.
    """
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if process.poll() is None:
        process.kill()
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        # Reader threads may still hold the pipe locks if process tree
        # termination failed. Do not let Popen.__exit__ block closing those
        # handles; report incomplete cleanup and let process exit reclaim them.
        process.stdout = None
        process.stderr = None
        process.stdin = None
        raise ValueError(
            "Owned subprocess output drain timed out; inspect retained child state"
        ) from None
