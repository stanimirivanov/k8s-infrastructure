"""Operate the reviewed local-kind substrate profile."""

import argparse
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
NAME = "perfeng-local"
CONTEXT = f"kind-{NAME}"
PROGRESS_INTERVAL = 10
COMMAND_TIMEOUT = 600
PROFILE = ROOT / "clusters/local-kind/perfeng-local/cluster-config.yaml"
ACTIONS = ("up", "ensure", "health", "down")
NODE_IMAGE = (
    "kindest/node:v1.35.0@sha256:452d707d4862f52530247495d180205e029056831160e22870e37e3f6c1ac31f"
)


def load_profile(path: Path = PROFILE) -> dict[str, Any]:
    """Load the exact reviewed local-kind profile without external access."""
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    expected = {
        "kind": "Cluster",
        "apiVersion": "kind.x-k8s.io/v1alpha4",
        "name": NAME,
        "networking": {"apiServerAddress": "127.0.0.1"},
        "nodes": [
            {
                "role": "control-plane",
                "image": NODE_IMAGE,
                "labels": {"workload": "control-plane"},
            },
            {
                "role": "worker",
                "image": NODE_IMAGE,
                "labels": {
                    "workload": "performance-generator",
                    "perfeng.io/node-type": "generator",
                },
            },
            {
                "role": "worker",
                "image": NODE_IMAGE,
                "labels": {"workload": "sut", "perfeng.io/node-type": "sut"},
            },
        ],
    }
    if value != expected:
        raise ValueError("Local kind profile differs from the reviewed compatibility identity")
    return value


def run(
    command: list[str],
    *,
    input_data: bytes | None = None,
    timeout: int = COMMAND_TIMEOUT,
    docker_context: str | None = None,
) -> str:
    """Run one external command with bounded, private output handling."""
    if timeout <= 0:
        raise ValueError("Command timeout must be positive")
    from .process_tree import terminate

    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"KUBECONFIG", "KUBERNETES_MASTER"} and not key.startswith("HELM_KUBE")
    }
    if docker_context is not None:
        # New isolated profiles pin kind's Docker child commands to the reviewed
        # named context; legacy Perfeng invocation retains its existing behavior.
        for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
            environment.pop(key, None)
        for key in list(environment):
            if key.startswith("KIND_EXPERIMENTAL_"):
                environment.pop(key)
        environment["DOCKER_CONTEXT"] = docker_context
    # Only show the executable, never arguments/output that may contain credentials.
    label = Path(command[0]).stem
    print(f"Starting {label} command...", flush=True)
    started = time.monotonic()
    deadline = started + timeout
    # Capture bytes: Windows' locale decoder can crash a subprocess reader thread
    # on kind's UTF-8 progress symbols, even when the child succeeds.
    with subprocess.Popen(
        command,
        stdin=subprocess.PIPE if input_data is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**environment, "KIND_EXPERIMENTAL_PROVIDER": "docker"},
        start_new_session=os.name != "nt",
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0,
    ) as process:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                terminate(process)
                raise ValueError(f"{label} timed out after {timeout}s; inspect the local target")
            try:
                stdout, _ = process.communicate(
                    input=input_data,
                    timeout=min(PROGRESS_INTERVAL, remaining),
                )
                break
            except subprocess.TimeoutExpired:
                # communicate() retains pending input; never submit credentials twice.
                input_data = None
                elapsed = int(time.monotonic() - started)
                print(f"Still running {label} command ({elapsed}s elapsed)...", flush=True)
    if process.returncode:
        # Never echo a kubeconfig or captured authentication data.
        raise ValueError(f"{label} failed (exit {process.returncode}); inspect the local cluster")
    try:
        output = stdout.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"{label} returned invalid UTF-8; output withheld") from None
    print(f"Completed {label} command.", flush=True)
    return output


def normalize_state_root(root: Path) -> Path:
    """Return an existing ordinary directory without following redirection."""
    absolute = Path(os.path.abspath(root))
    try:
        resolved = absolute.resolve(strict=True)
    except OSError as error:
        raise ValueError("State root must be an existing ordinary directory") from error
    if not resolved.is_dir():
        raise ValueError("State root must be an existing ordinary directory")
    if any(
        candidate.is_symlink() or candidate.is_junction()
        for candidate in (absolute, *absolute.parents)
    ):
        raise ValueError("Redirected local state is not allowed")
    return resolved


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
        load_profile()
        return [
            [
                "kind",
                "create",
                "cluster",
                "--name",
                NAME,
                "--config",
                str(PROFILE),
                "--kubeconfig",
                config,
                "--wait",
                "300s",
                "--retain",
            ],
            [*kubectl, "wait", "--for=condition=Ready", "nodes", "--all", "--timeout=300s"],
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
    if action not in {"up", "health", "down"}:
        raise ValueError("Unknown executable local-kind action")
    root = normalize_state_root(root)
    if action == "down" and confirmation != NAME:
        raise ValueError(
            "Deletion requires --confirm-delete perfeng-local; all cluster data is lost"
        )
    print("Checking kind version...", flush=True)
    version = run(["kind", "version"]).split()
    if len(version) < 2 or version[1] != "v0.31.0":
        raise ValueError("This local layout requires kind v0.31.0")
    print("Discovering local clusters...", flush=True)
    existing = run(["kind", "get", "clusters"]).splitlines()
    config = kubeconfig(root)
    if action == "up":
        if NAME in existing or config.exists():
            raise ValueError(
                "Cluster or kubeconfig already exists; startup never deletes or adopts it"
            )
        print("Checking local Docker endpoint and engine...", flush=True)
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
        print("Checking local Docker endpoint and cluster credentials...", flush=True)
        verify_local_docker()
        verify_context(root)
    steps = {
        "up": [
            "Creating kind cluster (initial image download may take several minutes)",
            "Waiting for nodes to become Ready",
        ],
        "health": [
            "Checking node readiness and placement",
            "Checking CoreDNS rollout",
        ],
        "down": ["Deleting the confirmed local cluster"],
    }
    for index, command in enumerate(commands(action, root)):
        print(f"[{index + 1}/{len(steps[action])}] {steps[action][index]}...", flush=True)
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


def ensure(root: Path = ROOT) -> None:
    """Create the named cluster when absent or validate the existing cluster."""
    root = normalize_state_root(root)
    print("Discovering the named local cluster...", flush=True)
    existing = run(["kind", "get", "clusters"]).splitlines()
    config = kubeconfig(root)
    if NAME in existing:
        if not config.exists():
            raise ValueError("Named local cluster exists without its dedicated kubeconfig")
        print("Reusing the existing named local cluster after health validation.", flush=True)
        execute("health", root)
        return
    if config.exists():
        raise ValueError("Dedicated kubeconfig exists without the named local cluster")
    execute("up", root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-delete")
    parser.add_argument("--state-root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        load_profile()
        state_root = normalize_state_root(args.state_root)
        if args.execute:
            if args.action == "ensure":
                ensure(state_root)
            else:
                execute(args.action, state_root, args.confirm_delete)
        else:
            if args.action == "ensure":
                print(json.dumps(["kind", "get", "clusters"]))
                print("Execution creates the missing named cluster or validates the existing one.")
            else:
                for command in commands(args.action, state_root):
                    print(json.dumps(command))
            print("Preview only. Add --execute to operate the selected local substrate.")
    except (OSError, ValueError, yaml.YAMLError) as error:
        print(f"Local kind lifecycle error: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
