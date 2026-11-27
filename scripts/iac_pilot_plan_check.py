"""Verify native kind create plans with an empty backend and no Docker socket."""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from .iac_pilot import PROFILES
from .iac_pilot_runtime import MODULE, validate_plan
from .iac_pilot_state import mapping
from .local_kind import run


def check(tofu: str, mirror_config: Path) -> None:
    with TemporaryDirectory(prefix="kind-iac-plan-") as directory:
        scratch = Path(directory)
        environment = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": directory,
            "LANG": "C.UTF-8",
            "DOCKER_HOST": f"unix://{scratch / 'no-docker.sock'}",
            "KUBECONFIG": str(scratch / "no-kubeconfig"),
            "TF_CLI_CONFIG_FILE": str(mirror_config.resolve(strict=True)),
            "TF_IN_AUTOMATION": "1",
            "CHECKPOINT_DISABLE": "1",
        }
        for name, _ in PROFILES.values():
            workspace = scratch / name
            workspace.mkdir()
            for filename in ("main.tf", ".terraform.lock.hcl"):
                shutil.copyfile(MODULE / filename, workspace / filename)
            prefix = [tofu, f"-chdir={workspace}"]
            state = workspace / "cluster.tfstate"
            plan = workspace / "create.tfplan"
            run(
                [
                    *prefix,
                    "init",
                    "-input=false",
                    "-lockfile=readonly",
                    f"-backend-config=path={state}",
                ],
                isolated_environment=environment,
            )
            run(
                [
                    *prefix,
                    "plan",
                    "-refresh=false",
                    "-input=false",
                    f"-var=cluster_name={name}",
                    f"-out={plan}",
                ],
                isolated_environment=environment,
            )
            result = run([*prefix, "show", "-json", str(plan)], isolated_environment=environment)
            validate_plan(mapping(result), "create", name)
            if state.exists():
                raise ValueError("The plan check unexpectedly created state")
            print(f"Verified one kind create and no other plan actions for {name}.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tofu", default="tofu")
    args = parser.parse_args()
    config = os.environ.get("TF_CLI_CONFIG_FILE")
    if not config:
        print("Set TF_CLI_CONFIG_FILE to the verified filesystem mirror configuration.")
        return 1
    try:
        check(args.tofu, Path(config))
    except (OSError, ValueError):
        print("Kind plan verification failed. Check the pinned CLI, mirror, and module.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
