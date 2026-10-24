"""Preview, inspect, apply or explicitly recover the reviewed retained enlargement."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from .consumer_profile import Target
from .profile_transition_model import PREDECESSOR, preview
from .profile_transition_state import read


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preview", "inspect", "apply", "recover"))
    parser.add_argument("--consumer", required=True)
    parser.add_argument("--docker-context", required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--https-port", type=int, required=True)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-quiesced")
    args = parser.parse_args(argv)
    try:
        target = Target(
            args.consumer, args.docker_context, args.state_root, PREDECESSOR, args.https_port
        )
        if not args.execute:
            result = preview(target)
        else:
            if args.action == "preview" or args.request is None:
                raise ValueError(
                    "Executed transition requires inspect/apply/recover and reviewed request"
                )
            if args.action in {"apply", "recover"} and args.confirm_quiesced != target.name:
                raise ValueError(
                    "Explicit exact-target scheduling/configuration quiescence confirmation required"
                )
            from .profile_transition_runtime import execute

            result = execute(target, json.loads(read(args.request, 16384)), args.action)
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError):
        print(
            "Retained transition refused or incomplete; preserve private journal/backups and inspect the exact target; private details withheld."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
