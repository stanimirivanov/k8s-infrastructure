"""Explicit delegated Argo preview/apply/health/handoff/removal for I01 consumers."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.error import URLError

import yaml

from .consumer_profile import PROFILES, Target
from .delegated_argo_config import download_assets, load_delegation, selection


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("asset-check", "apply", "health", "handoff", "remove", "qualify")
    )
    parser.add_argument("--profile", choices=PROFILES)
    parser.add_argument("--consumer")
    parser.add_argument("--docker-context")
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--delegation", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-remove")
    args = parser.parse_args(argv)
    try:
        if args.action == "asset-check":
            from .delegated_argo_config import Delegation
            from .delegated_argo_render import render

            render(
                Delegation("validation", ("validation-apps",), ("reviewed-template",)),
                download_assets(),
            )
            print("Delegated Argo assets verified by SHA-256; no cluster access.")
            return 0
        if any(
            value is None
            for value in (
                args.profile,
                args.consumer,
                args.docker_context,
                args.state_root,
                args.delegation,
            )
        ):
            raise ValueError(
                "Explicit profile, consumer, Docker context, state root and delegation required"
            )
        target = Target(args.consumer, args.docker_context, args.state_root, args.profile)
        delegation = load_delegation(args.delegation, target)
        if not args.execute:
            result = {
                "action": args.action,
                "execution": "not-performed",
                "cluster": target.name,
                "context": target.context,
                "dockerContext": target.docker_context,
                "delegationDigest": delegation.digest,
                "gitopsNamespace": delegation.gitops,
                "controllerNamespace": delegation.controllers,
                "managedNamespace": delegation.managed,
                "requiredConsumerOwnedNamespaces": [
                    *delegation.workload_namespaces,
                    delegation.managed,
                ],
                "approvedTemplates": delegation.approved_templates,
                "assets": selection(),
                "mutations": "Provider-owned controller resources, shared APIs when absent, delegated roles and native admission; no consumer Namespace/Project/Application/template/credential installation. Qualify explicitly creates a neutral test template/workflow and restarts only this controller. Removal retains shared APIs, namespaces, Secrets and consumer records.",
            }
        else:
            from . import delegated_argo_runtime as runtime

            if args.action == "apply":
                result = runtime.apply(target, delegation)
            elif args.action == "remove":
                result = runtime.remove(target, delegation, args.confirm_remove)
            elif args.action == "qualify":
                from .delegated_argo_qualification import qualify

                result = qualify(target, delegation)
            else:
                result = runtime.health(target, delegation)
        print(json.dumps(result, indent=2))
        return 0
    except ValueError as error:
        # JSON/YAML parsing can quote arbitrary input; never echo those details.
        if isinstance(error, json.JSONDecodeError):
            print("Delegated Argo input or response could not be parsed; private details withheld.")
        else:
            print(f"Delegated Argo refused or incomplete: {error}")
    except (OSError, URLError, TypeError, KeyError, yaml.YAMLError):
        print(
            "Delegated Argo prerequisite, input or command failed; partial state retained and private details withheld."
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
