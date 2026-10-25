"""Explicit disposable-only stops at real transition effect boundaries."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from . import profile_transition_state as store
from .consumer_profile import Target, identity
from .profile_transition_model import (
    PREDECESSOR,
    ROLES,
    SUCCESSOR,
    SUCCESSORS,
    paired,
    validate_request,
)
from .profile_transition_model import preview as transition_preview

CHECKPOINTS = tuple(
    f"{role}-{effect}" for role in ROLES for effect in ("cap", "write", "restart")
) + ("owner-commit",)


class _Stopped(Exception):
    """Only a selected successful effect can produce an expected drill stop."""


def preview(target: Target, successor_profile: str, checkpoint: str) -> dict:
    """Keep planning offline: no request, owner, journal, config or native reads."""
    if not target.consumer.startswith("qual-") or checkpoint not in CHECKPOINTS:
        raise ValueError("Drill requires a disposable qual- target and a fixed checkpoint")
    return {
        **transition_preview(target, successor_profile=successor_profile),
        "checkpoint": checkpoint,
        "effects": "Run the guarded transition on a fresh disposable predecessor; stop after the selected successful native effect before advancing its journal intention",
        "recovery": "Not performed; preserve state, inspect and obtain separate exact recovery approval",
        "liveInterruptionQualification": "pending",
        "exclusions": ["automatic recovery", "rollback", "cleanup", "cluster creation/deletion"],
    }


def interrupt(
    target: Target, request: dict, successor_profile: str, checkpoint: str, confirmation: str
) -> dict:
    """Use the ordinary protocol, with a callback scoped to this one execution.

    This is a controlled exception, not SIGKILL, host loss or a storage crash.
    Native commands must return successfully before the callback is invoked.
    An unrelated command failure is never converted into qualification evidence.
    """
    preview(target, successor_profile, checkpoint)
    if confirmation != target.name:
        raise ValueError(
            "Exact disposable scheduling/configuration quiescence confirmation required"
        )
    request = validate_request(target, request)
    if (
        request["successorDigest"]
        != identity(paired(target, successor_profile=successor_profile))["profileDigest"]
    ):
        raise ValueError("Drill successor differs from the reviewed request")
    if any(store.exists(target.path(suffix)) for suffix in ("transition.json", "transition.lock")):
        raise ValueError("Drill requires fresh transition history; preserve existing state")
    from . import profile_transition_runtime as transition

    reached = False

    def stop(effect: str, role: str | None) -> None:
        nonlocal reached
        observed = f"{role}-{effect}" if role is not None else effect
        if observed == checkpoint:
            reached = True
            raise _Stopped

    try:
        transition.execute(target, request, "apply", after_effect=stop)
    except _Stopped:
        if not reached:
            raise ValueError("Drill did not reach its selected checkpoint") from None
        journal = store.load(target, request)
        if checkpoint == "owner-commit":
            valid = journal["phase"] == "finalizing" and all(
                stage == "verified" for stage in journal["stages"].values()
            )
            valid = valid and store.read(target.path("owner.json")) == transition.successor_owner(
                target, journal
            )
        else:
            role, effect = checkpoint.rsplit("-", 1)
            expected = {"cap": "cap-intent", "write": "write-intent", "restart": "restart-intent"}
            valid = journal["phase"] == "nodes" and journal["stages"][role] == expected[effect]
            valid = (
                valid
                and store.read(target.path("owner.json")) == journal["predecessorOwner"].encode()
            )
        if not valid:
            raise ValueError(
                "Drill journal or ownership differs from the selected boundary"
            ) from None
        return {
            "execution": "interrupted-as-requested",
            "cluster": target.name,
            "operationId": request["operationId"],
            "providerRevision": request["providerRevision"],
            "requestHash": journal["requestHash"],
            "checkpoint": checkpoint,
            "phase": journal["phase"],
            "stages": journal["stages"],
            "journalReference": str(target.path("transition.json")),
            "liveRecoveryQualification": "pending; separate inspect and recovery approval required",
            "fullInstallationQualification": "pending",
        }
    raise ValueError("Selected drill checkpoint was not reached; preserve state")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preview", "interrupt"))
    parser.add_argument("--consumer", required=True)
    parser.add_argument("--docker-context", required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--https-port", type=int, required=True)
    parser.add_argument("--successor-profile", choices=SUCCESSORS, default=SUCCESSOR)
    parser.add_argument("--checkpoint", choices=CHECKPOINTS, required=True)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--confirm-quiesced")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        target = Target(
            args.consumer, args.docker_context, args.state_root, PREDECESSOR, args.https_port
        )
        result = preview(target, args.successor_profile, args.checkpoint)
        if args.execute:
            if (
                args.action != "interrupt"
                or args.request is None
                or args.confirm_quiesced != target.name
            ):
                raise ValueError("Explicit interruption, request and exact quiescence required")
            request = json.loads(store.read(args.request, 16384))
            result = interrupt(
                target, request, args.successor_profile, args.checkpoint, args.confirm_quiesced
            )
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError):
        print(
            "Disposable transition drill refused or incomplete; preserve private state; details withheld."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
