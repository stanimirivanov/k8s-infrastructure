"""Allowlisted bootstrap diagnostics: never serialize command arguments or output."""

import subprocess
from collections.abc import Iterator
from contextlib import contextmanager

from .local_kind import CommandFailure


class IdentityMismatch(ValueError):
    """An exact target identity differs from its verified reference."""


class Failure(ValueError):
    def __init__(self, evidence: dict) -> None:
        self.evidence = evidence
        super().__init__(f"{evidence['category']}: {evidence['phase']}")


def evidence(error: BaseException, phase: str, category: str = "internal-defect") -> dict:
    if isinstance(error, Failure):
        return error.evidence
    result = {"category": category, "phase": phase}
    if isinstance(error, IdentityMismatch):
        result["category"] = "identity-mismatch"
    elif isinstance(error, CommandFailure):
        if error.timed_out:
            result.update(category="timeout", timedOut=True)
        else:
            result.update(category="dependency-failure", exitCode=error.exit_code)
    elif isinstance(error, (TimeoutError, subprocess.TimeoutExpired)):
        result.update(category="timeout", timedOut=True)
    elif isinstance(error, subprocess.CalledProcessError):
        result.update(category="dependency-failure", exitCode=error.returncode)
    elif isinstance(error, FileNotFoundError):
        result["category"] = "missing-prerequisite"
    elif isinstance(error, KeyboardInterrupt):
        result["category"] = "cancelled"
    elif not isinstance(error, (ValueError, OSError)):
        result["category"] = "internal-defect"
    return result


@contextmanager
def phase(name: str, category: str) -> Iterator[None]:
    try:
        yield
    except (Exception, KeyboardInterrupt) as error:
        raise Failure(evidence(error, name, category)) from None
