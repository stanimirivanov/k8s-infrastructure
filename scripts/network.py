"""Networking previews, explicit partial bootstrap recovery and native qualification."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.error import URLError

import yaml

from .consumer_profile import NETWORK_PROFILES, Target, configuration
from .network_assets import download, render, selection
from .network_tls import validate_hostname


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("asset-check", "apply", "health", "handoff", "qualify", "remove")
    )
    parser.add_argument("--profile", choices=NETWORK_PROFILES)
    parser.add_argument("--consumer")
    parser.add_argument("--docker-context")
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--https-port", type=int)
    parser.add_argument("--hostname")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-delete")
    args = parser.parse_args(argv)
    try:
        if args.action == "asset-check":
            import tempfile

            with tempfile.TemporaryDirectory(prefix="network-assets-") as directory:
                target = Target(
                    "validation", "local-engine", Path(directory), "isolated-network-local", 18443
                )
                shared, documents = render(target, *download())
            print(
                f"Verified pinned networking assets and Helm render: {len(shared)} APIs, {len(documents)} resources; no cluster access."
            )
            return 0
        if any(
            value is None
            for value in (
                args.profile,
                args.consumer,
                args.docker_context,
                args.state_root,
                args.https_port,
            )
        ):
            raise ValueError(
                "Explicit profile, consumer, Docker context, private state root and HTTPS port required"
            )
        target = Target(
            args.consumer, args.docker_context, args.state_root, args.profile, args.https_port
        )
        hostname = validate_hostname(args.hostname or f"{target.consumer}.localhost")
        if not args.execute:
            result = {
                "action": args.action,
                "execution": "not-performed",
                "cluster": target.name,
                "context": target.context,
                "dockerContext": target.docker_context,
                "kindConfiguration": configuration(target),
                "origin": f"https://{hostname}:{target.https_port}",
                "assets": selection(),
                "mutations": "Create only fresh-profile CNI/Gateway resources; apply explicitly recovers this profile's partial bootstrap. Qualify creates neutral namespaces, policies, TLS leaf and routes. Removal destroys only the confirmed whole target; no in-place CNI uninstall or trust-store change.",
            }
        else:
            from . import network_runtime as runtime

            if args.action == "apply":
                result = runtime.resume(target)
            elif args.action in {"health", "handoff"}:
                result = runtime.health(target)
            elif args.action == "qualify":
                from .network_qualification import qualify

                result = qualify(target, hostname)
            else:
                from .consumer_lifecycle import down

                down(target, args.confirm_delete)
                result = {
                    "cluster": target.name,
                    "outcome": "removed",
                    "hostTrustStoreChanged": False,
                }
        print(json.dumps(result, indent=2))
        return 0
    except ValueError as error:
        print(
            "Networking input/response parsing failed; private details withheld."
            if isinstance(error, json.JSONDecodeError)
            else f"Networking refused or incomplete: {error}"
        )
    except (OSError, URLError, TypeError, KeyError, yaml.YAMLError):
        print(
            "Networking prerequisite or command failed; partial state retained; private details withheld."
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
