"""Bounded termination of only a subprocess tree created by this command adapter."""

from __future__ import annotations

import os
import signal
import subprocess


def terminate(process: subprocess.Popen[bytes]) -> None:
    """Close inherited child pipes without waiting forever after killing the parent.

    Callers create a new POSIX session or Windows process group. Never enumerate
    or kill by executable name, Docker context or infrastructure resource label.
    """
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            # Still reap the owned parent and bound output drain. A cleanup
            # failure is never reported as command success.
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        # Windows reader threads may still hold the pipe locks if OS tree
        # termination failed. Do not let Popen.__exit__ block closing those
        # handles; report incomplete cleanup and let process exit reclaim them.
        process.stdout = None
        process.stderr = None
        process.stdin = None
        raise ValueError(
            "Owned subprocess output drain timed out; inspect retained child state"
        ) from None
