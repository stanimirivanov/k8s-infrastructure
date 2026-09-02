"""Local-only kind lifecycle. Commands are previews unless --execute is supplied."""

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
NAME = "perfeng-local"
CONTEXT = f"kind-{NAME}"


def run(command: list[str]) -> str:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"KUBECONFIG", "KUBERNETES_MASTER"} and not key.startswith("HELM_KUBE")
    }
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env={**environment, "KIND_EXPERIMENTAL_PROVIDER": "docker"},
    )
    if result.returncode:
        # Never echo a kubeconfig or captured authentication data.
        raise ValueError(
            f"{command[0]} failed (exit {result.returncode}); inspect the local cluster"
        )
    return result.stdout


def kubeconfig(root: Path) -> Path:
    path = root / ".local" / "perfeng-local.kubeconfig"
    if path.parent.resolve() != path.parent or path.resolve() != path or path.is_symlink():
        raise ValueError("Redirected local state is not allowed")
    return path


def verify_local_docker() -> None:
    override = os.environ.get("DOCKER_HOST")
    if override and not override.startswith(("unix://", "npipe://")):
        raise ValueError("Remote/TCP Docker endpoints are not allowed by local cluster tooling")
    endpoint = json.loads(
        run(["docker", "context", "inspect", "--format", "{{json .Endpoints.docker.Host}}"])
    )
    if not isinstance(endpoint, str) or not endpoint.startswith(("unix://", "npipe://")):
        raise ValueError("Select a local Docker socket context before continuing")


def commands(action: str, root: Path = ROOT) -> list[list[str]]:
    config = str(kubeconfig(root))
    kubectl = ["kubectl", "--kubeconfig", config, "--context", CONTEXT]
    if action == "up":
        return [
            [
                "kind",
                "create",
                "cluster",
                "--name",
                NAME,
                "--config",
                str(root / "local/kind/cluster-config.yaml"),
                "--kubeconfig",
                config,
                "--wait",
                "300s",
                "--retain",
            ],
            [*kubectl, "wait", "--for=condition=Ready", "nodes", "--all", "--timeout=300s"],
        ]
    if action == "deploy":
        return [
            [*kubectl, "apply", "-f", str(root / "local/namespaces.yaml")],
            [
                "helm",
                "upgrade",
                "--install",
                "sample-sut",
                str(root / "charts/sample-sut"),
                "--kubeconfig",
                config,
                "--kube-context",
                CONTEXT,
                "--namespace",
                "perf-sut",
                "--wait",
                "--timeout",
                "180s",
            ],
        ]
    if action == "health":
        return [
            [*kubectl, "get", "nodes", "-o", "json"],
            [
                *kubectl,
                "-n",
                "kube-system",
                "rollout",
                "status",
                "deployment/coredns",
                "--timeout=60s",
            ],
            [
                *kubectl,
                "-n",
                "perf-sut",
                "rollout",
                "status",
                "deployment/sample-sut",
                "--timeout=60s",
            ],
        ]
    if action == "down":
        return [["kind", "delete", "cluster", "--name", NAME, "--kubeconfig", config]]
    raise ValueError("Unknown action")


def verify_context(root: Path) -> None:
    config = kubeconfig(root)
    expected = yaml.safe_load(run(["kind", "get", "kubeconfig", "--name", NAME]))
    actual = yaml.safe_load(config.read_text(encoding="utf-8"))
    if not isinstance(expected, dict) or not expected.get("clusters") or actual != expected:
        raise ValueError("Local kubeconfig does not match the named kind cluster; refusing access")


def check_nodes(data: dict[str, Any]) -> None:
    nodes = data.get("items", [])
    if len(nodes) != 3:
        raise ValueError("Expected exactly three local kind nodes")
    labels = [node.get("metadata", {}).get("labels", {}).get("workload") for node in nodes]
    if sorted(str(label) for label in labels) != ["control-plane", "performance-generator", "sut"]:
        raise ValueError("Missing or duplicate node placement labels")
    for node in nodes:
        conditions = {
            item["type"]: item["status"] for item in node.get("status", {}).get("conditions", [])
        }
        if conditions.get("Ready") != "True" or node.get("spec", {}).get("unschedulable", False):
            raise ValueError("Node is not ready or is cordoned")
        for condition in ["MemoryPressure", "DiskPressure", "PIDPressure"]:
            if conditions.get(condition) != "False":
                raise ValueError("Node pressure is active or unknown")
        if conditions.get("NetworkUnavailable", "False") != "False":
            raise ValueError("Node network is unavailable")


def execute(action: str, root: Path = ROOT, confirmation: str | None = None) -> None:
    if action == "down" and confirmation != NAME:
        raise ValueError(
            "Deletion requires --confirm-delete perfeng-local; all cluster data is lost"
        )
    version = run(["kind", "version"]).split()
    if len(version) < 2 or version[1] != "v0.31.0":
        raise ValueError("This local layout requires kind v0.31.0")
    existing = run(["kind", "get", "clusters"]).splitlines()
    config = kubeconfig(root)
    if action == "up":
        if NAME in existing or config.exists():
            raise ValueError(
                "Cluster or kubeconfig already exists; startup never deletes or adopts it"
            )
        verify_local_docker()
        run(["docker", "info"])
        config.parent.mkdir(mode=0o700, exist_ok=True)
        # Reserve only this dedicated file, never a user's global kubeconfig.
        with config.open("x", encoding="utf-8"):
            pass
        config.chmod(0o600)
    else:
        if NAME not in existing:
            raise ValueError("Named local cluster does not exist")
        verify_local_docker()
        verify_context(root)
    for index, command in enumerate(commands(action, root)):
        if action == "up" and index > 0:
            verify_context(root)
        output = run(command)
        if action == "health" and index == 0:
            check_nodes(json.loads(output))
    if action == "down":
        config.unlink(missing_ok=True)
        print(
            "Deleted perfeng-local and its dedicated kubeconfig. Cluster-local data is not recoverable."
        )
    else:
        print(f"Local cluster action completed: {action}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["up", "deploy", "health", "down"])
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-delete")
    args = parser.parse_args()
    try:
        if not args.execute:
            for command in commands(args.action):
                print(json.dumps(command))
            print(
                "Preview only. Add --execute to run; down also requires --confirm-delete perfeng-local."
            )
        else:
            execute(args.action, confirmation=args.confirm_delete)
    except (OSError, ValueError, yaml.YAMLError) as error:
        print(f"Local cluster error: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
