"""Pinned kind v0.31 Docker adapter: inspect enforced caps before the first start.

This is a private operation-scoped executable, not a generic Docker replacement.
A protocol change fails closed. Journals precede start and survive every failure.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import bootstrap_failure as failure
from .consumer_profile import MINIMAL_PROFILE, Target, load_profile
from .consumer_state import read_bounded

INSPECT = (
    '{"id":{{json .Id}},"name":{{json .Name}},'
    '"cluster":{{json (index .Config.Labels "io.x-k8s.kind.cluster")}},'
    '"role":{{json (index .Config.Labels "io.x-k8s.kind.role")}},'
    '"image":{{json .Config.Image}},"memory":{{json .HostConfig.Memory}},'
    '"swap":{{json .HostConfig.MemorySwap}},"cpu":{{json .HostConfig.NanoCpus}},'
    '"pids":{{json .HostConfig.PidsLimit}},"running":{{json .State.Running}},'
    '"startedAt":{{json .State.StartedAt}},"restartCount":{{json .RestartCount}}}'
)


def policy(target: Target, nonce: str, journal_dir: Path) -> dict[str, Any]:
    if target.profile != MINIMAL_PROFILE:
        raise ValueError("Bounded adapter requires minimal-demo")
    profile = load_profile(name=target.profile)
    return {
        "version": 1,
        "target": target.name,
        "context": target.docker_context,
        "image": profile["nodeImage"],
        "nonce": nonce,
        "journalDirectory": str(journal_dir.resolve()),
        "nodes": {
            f"{target.name}-{role}": {"role": role, **budget}
            for role, budget in profile["nodes"].items()
        },
    }


def write_receipt(path: Path, value: dict, *, exclusive: bool = False) -> None:
    """Flush evidence before start; atomic replacement never truncates prior evidence."""
    if exclusive:
        with path.open("x", encoding="utf-8") as stream:
            path.chmod(0o600)
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
    else:
        temporary = path.with_suffix(".pending")
        write_receipt(temporary, value, exclusive=True)
        os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_journals(directory: Path) -> list[dict]:
    return [json.loads(read_bounded(path)) for path in sorted(directory.glob("*-local-*.json"))]


def node_arguments(args: list[str], selection: dict) -> tuple[str, list[str]]:
    """Accept the exact native Linux IPv4, no-proxy, two-node pinned protocol."""
    if len(args) < 4 or args[:2] != ["run", "--name"]:
        raise ValueError("Unsupported Docker creation protocol")
    name = args[2]
    if name not in selection["nodes"]:
        raise ValueError("Foreign or helper container creation refused")
    node = selection["nodes"][name]
    expected = [
        "run",
        "--name",
        name,
        "--hostname",
        name,
        "--label",
        f"io.x-k8s.kind.role={node['role']}",
        "--privileged",
        "--security-opt",
        "seccomp=unconfined",
        "--security-opt",
        "apparmor=unconfined",
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/run",
        "--volume",
        "/var",
        "--volume",
        "/lib/modules:/lib/modules:ro",
        "-e",
        "KIND_EXPERIMENTAL_CONTAINERD_SNAPSHOTTER",
        "--detach",
        "--tty",
        "--label",
        f"io.x-k8s.kind.cluster={selection['target']}",
        "--net",
        "kind",
        "--restart=on-failure:1",
        "--init=false",
        "--cgroupns=private",
    ]
    if node["role"] == "control-plane":
        index = len(expected)
        if (
            len(args) <= index
            or not re.fullmatch(r"--publish=127\.0\.0\.1:[1-9][0-9]{0,4}:6443/tcp", args[index])
            or int(args[index].split(":")[1]) > 65535
        ):
            raise ValueError("Unexpected API listener protocol")
        expected += [args[index], "-e", "KUBECONFIG=/etc/kubernetes/admin.conf"]
    expected.append(selection["image"])
    if args != expected:
        raise ValueError("Pinned kind node creation protocol changed; no uncapped fallback")
    converted = ["create", *args[1:-1]]
    converted.remove("--detach")
    converted[converted.index("--restart=on-failure:1")] = "--restart=no"
    converted[converted.index("/tmp")] = "/tmp:size=268435456"
    converted[converted.index("/run")] = "/run:size=134217728"
    converted += [
        "--pull=never",
        "--cpus",
        str(node["cpus"]),
        "--memory",
        str(node["memoryBytes"]),
        "--memory-swap",
        str(node["memoryBytes"]),
        "--pids-limit",
        "2048",
        "--log-driver",
        "local",
        "--log-opt",
        "max-size=10m",
        "--log-opt",
        "max-file=3",
        selection["image"],
    ]
    return name, converted


def intercept(args: list[str], selection: dict, invoke: Callable[[list[str]], str]) -> str:
    with failure.phase("adapter-protocol", "unsupported-capability"):
        name, create = node_arguments(args, selection)
    directory = Path(selection["journalDirectory"])
    node = selection["nodes"][name]
    base = {
        "target": selection["target"],
        "nonce": selection["nonce"],
        "name": name,
        "role": node["role"],
        "caps": node,
    }
    write_receipt(
        directory / f"{name}.claim", {**base, "phase": "create-requested"}, exclusive=True
    )
    with failure.phase("docker-create", "dependency-failure"):
        container_id = invoke(create).strip()
    if not re.fullmatch(r"[a-f0-9]{64}", container_id):
        raise ValueError("Creation returned no immutable identity; retain claim for diagnosis")
    path = directory / f"{name}.json"
    receipt = {**base, "id": container_id, "phase": "created"}
    write_receipt(path, receipt, exclusive=True)
    with failure.phase("docker-inspect", "dependency-failure"):
        observed = json.loads(invoke(["inspect", "--format", INSPECT, container_id]))
    expected = {
        "id": container_id,
        "name": f"/{name}",
        "cluster": selection["target"],
        "role": node["role"],
        "image": selection["image"],
        "memory": node["memoryBytes"],
        "swap": node["memoryBytes"],
        "cpu": node["cpus"] * 10**9,
        "pids": 2048,
        "running": False,
        "startedAt": "0001-01-01T00:00:00Z",
        "restartCount": 0,
    }
    if observed != expected:
        raise ValueError("Stopped node identity or creation limits mismatch; start refused")
    receipt.update(beforeStart=observed, phase="prestart-verified", startRequestedAt=time.time_ns())
    write_receipt(path, receipt)
    with failure.phase("docker-start", "dependency-failure"):
        invoke(["start", container_id])
    receipt.update(phase="started", startCompletedAt=time.time_ns())
    write_receipt(path, receipt)
    return container_id + "\n"


def recorded_id(reference: str, selection: dict) -> str:
    for entry in read_journals(Path(selection["journalDirectory"])):
        if (
            reference in {entry.get("name"), entry.get("id")}
            and entry.get("nonce") == selection["nonce"]
            and entry.get("target") == selection["target"]
            and entry.get("phase") == "started"
            and re.fullmatch(r"[a-f0-9]{64}", entry.get("id", ""))
        ):
            return entry["id"]
    raise ValueError("Command does not address a started immutable owned node")


def forward(args: list[str], selection: dict) -> list[str]:
    """Delegate only bounded creation's observations and owned in-node bootstrap."""
    if not args:
        raise ValueError("Missing Docker operation")
    if args == ["-v"]:
        return args
    operation = args[0]
    if operation in {"info", "version", "ps", "inspect"}:
        return args
    if operation == "image" and len(args) > 2 and args[1] == "inspect":
        return args
    if args == ["network", "ls", "--filter=name=^kind$", "--format={{.ID}}"]:
        return args
    if args[:2] == ["network", "inspect"] and args[-1] == "kind":
        return args
    # Require an existing kind bridge: network lifecycle is a separate operation.
    # This avoids an unowned shared network mutation during a partial node create.
    if operation in {"exec", "logs"}:
        index = 1
        flags = (
            {"-i", "-t", "-it", "--privileged"}
            if operation == "exec"
            else {"-f", "--follow", "--timestamps"}
        )
        while index < len(args):
            if args[index] in flags:
                index += 1
            elif operation == "exec" and args[index] == "-e":
                if index + 1 >= len(args) or not re.fullmatch(
                    r"[A-Za-z_][A-Za-z0-9_]*=.*", args[index + 1], re.DOTALL
                ):
                    raise ValueError("Unsupported node exec environment")
                index += 2
            else:
                break
        if index >= len(args):
            raise ValueError("Missing owned node reference")
        return [*args[:index], recorded_id(args[index], selection), *args[index + 1 :]]
    if operation == "cp" and len(args) == 3:
        result = list(args)
        found = 0
        for index in (1, 2):
            if ":" in args[index]:
                reference, path = args[index].split(":", 1)
                result[index] = f"{recorded_id(reference, selection)}:{path}"
                found += 1
        if found == 1:
            return result
    raise ValueError("Unsupported Docker operation; no unbounded fallback")


def prepare(target: Target, nonce: str, directory: Path) -> dict[str, str]:
    """Reserve a private adapter and immutable policy; never install a global shim."""
    if directory.is_symlink() or directory.parent.resolve() != directory.parent:
        raise ValueError("Redirected bounded adapter directory")
    real = shutil.which("docker")
    if real is None:
        raise ValueError("Docker executable is unavailable")
    real = str(Path(real).resolve(strict=True))
    directory.mkdir(mode=0o700)
    path = directory / "policy.json"
    write_receipt(path, policy(target, nonce, directory), exclusive=True)
    wrapper = directory / "docker"
    # The source checkout and interpreter remain pinned to this operation. No shell
    # interpolation is used for paths, context values or provider arguments.
    source = Path(__file__).resolve().parents[1]
    wrapper.write_text(
        f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(source)!r})\n"
        "from scripts.bounded_docker import main\nraise SystemExit(main())\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o700)
    return {
        "PATH": str(directory) + os.pathsep + os.environ.get("PATH", ""),
        "BOUNDED_DOCKER_POLICY": str(path),
        "BOUNDED_DOCKER_REAL": real,
    }


def main() -> int:
    selection = None
    try:
        path = Path(os.environ["BOUNDED_DOCKER_POLICY"])
        selection = json.loads(read_bounded(path))
        real = os.environ["BOUNDED_DOCKER_REAL"]
        if not Path(real).is_absolute() or Path(real).resolve() == Path(sys.argv[0]).resolve():
            raise ValueError("Invalid real Docker executable")
        environment = dict(os.environ)
        for key in ("DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
            environment.pop(key, None)
        environment["DOCKER_CONTEXT"] = selection["context"]
        args = sys.argv[1:]
        if args[:1] == ["run"]:

            def invoke(command: list[str]) -> str:
                process = subprocess.run(
                    [real, *command],
                    env=environment,
                    capture_output=True,
                    timeout=120,
                    check=True,
                )
                return process.stdout.decode("utf-8")

            print(intercept(args, selection, invoke), end="")
            return 0
        with failure.phase("adapter-protocol", "unsupported-capability"):
            command = forward(args, selection)
        os.execve(real, [real, *command], environment)
    except Exception as error:
        safe = failure.evidence(error, "adapter-initialization")
        if selection is not None:
            try:
                write_receipt(
                    Path(selection["journalDirectory"])
                    / f"failure-{os.getpid()}-{time.time_ns()}.json",
                    {"target": selection["target"], "nonce": selection["nonce"], "failure": safe},
                    exclusive=True,
                )
            except (OSError, ValueError):
                safe = {**safe, "journalWriteFailed": True}
        print(json.dumps({"failure": safe}), file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
