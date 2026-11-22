"""Preview, install, observe and explicitly qualify local database capabilities."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from urllib.error import URLError

import yaml

from . import bootstrap_failure
from .consumer_profile import MINIMAL_PROFILE, NETWORK_PROFILES, Target
from .database_assets import CONTROLLERS, download, render, selection, storage_class
from .delegated_argo_config import load_delegation


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("asset-check", "apply", "health", "handoff", "qualify"))
    parser.add_argument("--consumer")
    parser.add_argument("--profile", choices=(*NETWORK_PROFILES, MINIMAL_PROFILE))
    parser.add_argument("--docker-context")
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--https-port", type=int)
    parser.add_argument("--delegation", type=Path)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--host-id")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.action == "asset-check":
            from .minimal_database_resources import rendered_bounds, required_images

            assets = download()
            with tempfile.TemporaryDirectory(prefix="database-assets-") as directory:
                target = Target(
                    "validation", "local-engine", Path(directory), "isolated-network-local", 18444
                )
                groups = render(target, assets)
                minimal = Target(
                    "qual-validation", "local-engine", Path(directory), MINIMAL_PROFILE, 18444
                )
                minimal_groups = render(minimal, assets)
                print(
                    json.dumps(
                        {
                            "minimalDemoRequiredImages": required_images(minimal_groups),
                            "minimalDemoResourceEnvelope": rendered_bounds(minimal, minimal_groups),
                        },
                        sort_keys=True,
                    )
                )
            print(
                f"Verified database assets and {sum(len(docs) for docs in groups.values())} resources; no cluster access."
            )
            return 0
        if any(
            value is None
            for value in (
                args.consumer,
                args.profile,
                args.docker_context,
                args.state_root,
                args.https_port,
            )
        ):
            raise ValueError(
                "Explicit consumer, network profile, Docker context, private state and HTTPS port required"
            )
        target = Target(
            args.consumer, args.docker_context, args.state_root, args.profile, args.https_port
        )
        if target.profile == MINIMAL_PROFILE:
            if (
                args.action != "qualify"
                or not target.consumer.startswith("qual-")
                or args.delegation
            ):
                raise ValueError(
                    "Minimal-demo supports only disposable database controller qualification"
                )
            if args.execute:
                if args.request is None or not args.host_id:
                    raise ValueError(
                        "Minimal-demo requires the original admission request and current host ID"
                    )
                from .consumer_state import read_bounded
                from .minimal_database import qualify

                result = qualify(target, json.loads(read_bounded(args.request)), args.host_id)
            else:
                result = {
                    "action": args.action,
                    "execution": "not-performed",
                    "cluster": target.name,
                    "context": target.context,
                    "dockerContext": target.docker_context,
                    "assets": selection(),
                    "storageClass": storage_class(target),
                    "controllerPlacement": "worker",
                    "controllers": list(CONTROLLERS),
                    "qualification": "Continue the exact successful minimal-demo network qualification. Stage five bounded controllers, verify webhook and certificate issuance, then verify individual stop-before-start replacements. No persisted database instance or PVC.",
                }
            print(json.dumps(result, indent=2))
            return 0
        if args.request is not None or args.host_id is not None:
            raise ValueError("Host admission inputs are only supported for minimal-demo")
        delegation = load_delegation(args.delegation, target) if args.delegation else None
        spaces = delegation.database_namespaces if delegation else ()
        if delegation is not None and not spaces:
            raise ValueError("Database delegation requires explicit databaseNamespaces opt-in")
        if not args.execute:
            result = {
                "action": args.action,
                "execution": "not-performed",
                "cluster": target.name,
                "context": target.context,
                "dockerContext": target.docker_context,
                "assets": selection(),
                "storageClass": storage_class(target),
                "databaseNamespaces": spaces,
                "controllers": list(CONTROLLERS),
                "controllerRequests": {"cpu": "300m", "memory": "448Mi"},
                "qualification": "One neutral namespace, one single-instance PostgreSQL Cluster and one mount probe; two 1Gi claims. Restart its database Pod, delete/recreate only its CNPG operator Deployment, delete neutral owner/PVC and explicitly rebind the exact retained PV. No consumer backup or bucket created.",
                "disposal": "Whole-target removal destroys node-local data; requires separate exact confirmation and authorization.",
            }
        else:
            from . import database_policy, database_runtime

            if args.action == "apply":
                result = database_runtime.install(target)
                if spaces:
                    result.update(database_policy.configure(target, spaces))
            elif args.action == "qualify":
                from .database_qualification import qualify

                if spaces:
                    raise ValueError(
                        "Neutral qualification must not select consumer database namespaces"
                    )
                result = qualify(target)
            else:
                result = database_runtime.health(target)
                if spaces:
                    result.update(database_policy.health(target, spaces))
        print(json.dumps(result, indent=2))
        return 0
    except bootstrap_failure.Failure as error:
        print(
            json.dumps(
                {
                    "failure": error.evidence,
                    "receipt": "Check the selected target's private database qualification receipt if reserved.",
                }
            )
        )
    except ValueError as error:
        print(
            "Database response parsing failed; private details withheld."
            if isinstance(error, json.JSONDecodeError)
            else f"Database capability refused or incomplete: {error}"
        )
    except (OSError, URLError, TypeError, KeyError, yaml.YAMLError):
        print(
            "Database prerequisite or command failed; partial state retained; private details withheld."
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
