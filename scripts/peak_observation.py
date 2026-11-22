"""Read-only physical store and Kubernetes attempt adapters for resource peaks."""

from __future__ import annotations

import json
import os
import re
import selectors
import subprocess
import time
from datetime import datetime
from pathlib import Path

from . import consumer_runtime as runtime
from . import host_observation as host
from . import minimal_bootstrap as bootstrap
from .consumer_profile import Target
from .host_admission import digest, integer
from .process_tree import terminate

SECOND = 1_000_000_000
POD_LIMIT = 256
ATTEMPT_LIMIT = 512
SCOPE_LIMIT = 4096
API_LIMIT = 8 * 1024**2
ID = re.compile(r"[0-9a-f]{64}")
UID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
NAME = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?")
POD_SLICE = re.compile(r"kubepods(?:-(?:burstable|besteffort))?(?:-pod[a-f0-9_]+)?\.slice")
SCOPE = re.compile(r"cri-containerd-([0-9a-f]{64})\.scope")
CONTAINERS = (
    ("app", "containers", "containerStatuses"),
    ("init", "initContainers", "initContainerStatuses"),
    ("ephemeral", "ephemeralContainers", "ephemeralContainerStatuses"),
)


def token(value: object, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError("Malformed resource observation identity")
    return value


def api_timestamp(value: object) -> int:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z", value) is None
    ):
        raise ValueError("Malformed Kubernetes attempt timestamp")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (int(stamp.timestamp()) * SECOND) + stamp.microsecond * 1000


def attempt(state: dict, status_id: str | None) -> dict:
    if len(state) != 1 or next(iter(state)) not in {"running", "terminated", "waiting"}:
        raise ValueError("Invalid Kubernetes attempt state")
    kind, value = next(iter(state.items()))
    if kind == "waiting":
        return {"state": "waiting", "containerId": None, "startedAtNs": None, "finishedAtNs": None}
    reference = value.get("containerID") or status_id
    started = api_timestamp(value.get("startedAt"))
    finished = api_timestamp(value.get("finishedAt")) if kind == "terminated" else None
    if finished is not None and finished < started:
        raise ValueError("Kubernetes attempt ended before it started")
    return {
        "state": kind,
        "containerId": token((reference or "").removeprefix("containerd://"), ID)
        if isinstance(reference, str) and reference.startswith("containerd://")
        else None,
        "startedAtNs": started,
        "finishedAtNs": finished,
    }


def pod_attempts(payload: dict, nodes: set[str], *, window_start_ns: int) -> list[dict]:
    """Project only scheduled Pod identity, declarations and current/last attempt status."""
    items = payload.get("items")
    if (
        not isinstance(items, list)
        or len(items) > POD_LIMIT
        or payload.get("metadata", {}).get("continue")
    ):
        raise ValueError("Pod inventory missing or exceeds collection bound")
    rows: list[dict] = []
    seen_pods: set[str] = set()
    seen_ids: set[str] = set()
    for pod in items:
        spec = pod["spec"]
        node = spec.get("nodeName")
        if not node:
            continue
        if node not in nodes:
            raise ValueError("Pod inventory contains an unexpected node")
        meta, status = pod["metadata"], pod.get("status", {})
        uid = token(meta["uid"], UID)
        if uid in seen_pods:
            raise ValueError("Duplicate Pod incarnation")
        seen_pods.add(uid)
        location = {
            "podUid": uid,
            "podName": token(meta["name"], NAME),
            "namespace": token(meta["namespace"], NAME),
            "node": node,
        }
        for kind, declarations, statuses in CONTAINERS:
            declared = spec.get(declarations, [])
            names = [token(container["name"], NAME) for container in declared]
            reported = status.get(statuses, [])
            by_name = {container["name"]: container for container in reported}
            if (
                len(by_name) != len(reported)
                or len(names) != len(set(names))
                or not by_name.keys() <= set(names)
            ):
                raise ValueError("Inconsistent container inventory")
            for name, declaration in zip(names, declared, strict=True):
                record = by_name.get(name)
                selected = []
                restart = None
                if record:
                    restart = integer(record.get("restartCount"))
                    if record.get("state"):
                        selected.append(attempt(record["state"], record.get("containerID")))
                    if record.get("lastState"):
                        selected.append(attempt(record["lastState"], None))
                if not selected:
                    selected.append(
                        {
                            "state": "missing-status",
                            "containerId": None,
                            "startedAtNs": None,
                            "finishedAtNs": None,
                        }
                    )
                for item in selected:
                    finished = item["finishedAtNs"]
                    # API timestamps can have only second precision; keep the opening second.
                    if finished is not None and finished + SECOND <= window_start_ns:
                        continue
                    container_id = item["containerId"]
                    if container_id is not None:
                        if container_id in seen_ids:
                            raise ValueError("Duplicate container attempt identity")
                        seen_ids.add(container_id)
                    rows.append(
                        {
                            **location,
                            **item,
                            "containerName": name,
                            "kind": kind,
                            "restartableInit": kind == "init"
                            and declaration.get("restartPolicy") == "Always",
                            "restartCount": restart,
                        }
                    )
                    if len(rows) > ATTEMPT_LIMIT:
                        raise ValueError("Container inventory exceeds collection bound")
    return rows


def read_api(target: Target, kubeconfig: str, route: str, *, timeout: int) -> dict:
    """Use a fixed raw GET, withholding private stdout/stderr and bounding pipe bytes."""
    if route not in {f"/api/v1/pods?limit={POD_LIMIT}", "/api/v1/namespaces/kube-system"}:
        raise ValueError("Unsupported resource peak API route")
    # Pass the exact verified bytes via an anonymous descriptor. A subsequent
    # edit to the persisted kubeconfig cannot redirect these raw GET requests.
    with os.fdopen(os.memfd_create("resource-peaks-kubeconfig"), "w+b") as configuration:
        configuration.write(kubeconfig.encode())
        configuration.seek(0)
        return api_get(target, route, timeout, configuration.fileno())


def api_get(target: Target, route: str, timeout: int, configuration: int) -> dict:
    command = [
        "kubectl",
        "--kubeconfig",
        f"/proc/self/fd/{configuration}",
        "--context",
        target.context,
        f"--request-timeout={timeout}s",
        "get",
        "--raw",
        route,
    ]
    end = time.monotonic() + timeout
    output = bytearray()
    with (
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=bootstrap.environment(target.docker_context, {}),
            pass_fds=(configuration,),
            start_new_session=True,
        ) as process,
        selectors.DefaultSelector() as selector,
    ):
        assert process.stdout is not None
        selector.register(process.stdout, selectors.EVENT_READ)
        try:
            while True:
                remaining = end - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise TimeoutError("Resource API request exceeded its deadline")
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > API_LIMIT:
                    raise ValueError("Resource API response exceeds collection bound")
            if process.wait(timeout=max(0, end - time.monotonic())):
                raise ValueError("Resource API request unavailable")
        except BaseException:
            terminate(process)
            raise
    payload = json.loads(output)
    if not isinstance(payload, dict):
        raise ValueError("Resource API response is not an object")
    return payload


def inventory(
    target: Target, kubeconfig: str, nodes: set[str], window_start_ns: int, *, timeout: int
) -> list[dict]:
    return pod_attempts(
        read_api(target, kubeconfig, f"/api/v1/pods?limit={POD_LIMIT}", timeout=timeout),
        nodes,
        window_start_ns=window_start_ns,
    )


def container_scopes(node: Path) -> dict[str, Path]:
    """Traverse only the bounded systemd kubepods hierarchy below the owned node."""
    root = node / "kubepods.slice"
    if not root.exists():
        return {}
    if root.is_symlink():
        raise ValueError("Unexpected kubepods cgroup path")
    pending = [(root, 0)]
    result = {}
    count = 0
    while pending:
        directory, depth = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                if count > SCOPE_LIMIT:
                    raise ValueError("Cgroup inventory exceeds collection bound")
                if not entry.is_dir(follow_symlinks=False):
                    continue
                match = SCOPE.fullmatch(entry.name)
                if match:
                    container_id = match.group(1)
                    if container_id in result:
                        raise ValueError("Duplicate container cgroup scope")
                    result[container_id] = Path(entry.path)
                elif depth < 2 and POD_SLICE.fullmatch(entry.name):
                    pending.append((Path(entry.path), depth + 1))
    return result


def physical_paths(target: Target, endpoint: str, request: dict) -> dict[str, Path]:
    stores = bootstrap.physical_engine(target, endpoint)
    bootstrap.verify_stores(request, None, stores)
    result = {f"account/{item['id']}": host.ordinary_path(item["path"]) for item in request["disk"]}
    result.update(
        {
            f"engine/{key}/{index}": path
            for key, paths in stores.items()
            for index, path in enumerate(paths)
        }
    )
    return result


def physical_ids(paths: dict[str, Path], host_id: str) -> dict:
    result = {}
    mounts = host.mount_records()
    for label, path in paths.items():
        if path.resolve(strict=True) != path:
            raise ValueError("Physical store path was redirected")
        host.physical_filesystem(path, mounts=mounts)
        info = path.stat()
        result[label] = {
            "filesystemId": digest([host_id, info.st_dev]),
            "physicalPathId": digest([host_id, info.st_dev, info.st_ino]),
        }
    return result


def observe_filesystems(paths: dict[str, Path], expected: dict, host_id: str) -> dict:
    """Use native block counters once per real filesystem and detect changed mounts."""
    if physical_ids(paths, host_id) != expected:
        raise ValueError("Physical store identity changed")
    result = {}
    for label, path in paths.items():
        key = expected[label]["filesystemId"]
        if key not in result:
            value = os.statvfs(path)
            if not 0 <= value.f_bavail <= value.f_bfree <= value.f_blocks or value.f_frsize <= 0:
                raise ValueError("Invalid physical filesystem counters")
            result[key] = {
                "usedBytes": (value.f_blocks - value.f_bfree) * value.f_frsize,
                "availableBytes": value.f_bavail * value.f_frsize,
            }
    if physical_ids(paths, host_id) != expected:
        raise ValueError("Physical store identity changed during observation")
    return result


def verify_incarnation(target: Target, kubeconfig: str, expected: str, timeout: int = 5) -> None:
    observed = read_api(target, kubeconfig, "/api/v1/namespaces/kube-system", timeout=timeout)
    if observed.get("metadata", {}).get("uid") != expected:
        raise ValueError("Cluster incarnation changed during collection")
    runtime.remaining_timeout()
