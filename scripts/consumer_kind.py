"""Preview or explicitly operate the reviewed isolated-local consumer profile."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from . import consumer_lifecycle as lifecycle
from .consumer_profile import Target, configuration, load_profile


def preview(action: str, target: Target) -> dict:
    """Pure plan: no subprocess, kubeconfig, ownership-record or cluster access."""
    load_profile()
    for suffix in ("kubeconfig", "owner.json", "kind.yaml"):
        target.path(suffix)
    return {
        "action": action,
        "consumer": target.consumer,
        "cluster": target.name,
        "context": target.context,
        "dockerContext": target.docker_context,
        "kubeconfigReference": str(target.path("kubeconfig")),
        "kindConfiguration": configuration(target),
        "resourceBudgets": load_profile(),
        "mutations": {
            "up": "Create only the absent named cluster and reserve its private identity files; then bound owned nodes",
            "ensure": "Create only when cluster and state are absent, otherwise verify exact healthy ownership",
            "health": "Observe owned cluster identity, readiness and capacity; no desired-state mutation",
            "handoff": "Observe public capability metadata after exact ownership/readiness checks",
            "down": "Permanently delete only the confirmed owned cluster and its dedicated state files",
        }[action],
        "execution": "not-performed",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("up", "ensure", "health", "handoff", "down"))
    parser.add_argument("--profile", choices=("isolated-local",), required=True)
    parser.add_argument("--consumer", required=True)
    parser.add_argument("--docker-context", required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-delete")
    args = parser.parse_args(argv)
    try:
        target = Target(args.consumer, args.docker_context, args.state_root)
        if not args.execute:
            result = preview(args.action, target)
        elif args.action == "down":
            lifecycle.down(target, args.confirm_delete)
            result = {"cluster": target.name, "outcome": "removed"}
        elif args.action in {"health", "handoff"}:
            result = lifecycle.health(target)
        elif args.action == "ensure":
            result = lifecycle.ensure(target)
        else:
            result = lifecycle.create(target)
        print(json.dumps(result, indent=2))
    except (OSError, json.JSONDecodeError, yaml.YAMLError):
        # Parser/JSON/YAML exceptions may quote private state. Only safe classification exits.
        print(
            "Consumer lifecycle refused or incomplete; inspect selected target and retained private state."
        )
        return 1
    except ValueError as error:
        print(f"Consumer lifecycle: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
