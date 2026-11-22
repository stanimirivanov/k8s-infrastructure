"""Explicit, admitted minimal-demo cold bootstrap; no CNI or consumer handoff."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import struct
import subprocess
import threading
import time
import tomllib
from datetime import datetime
from pathlib import Path

from . import consumer_runtime as runtime
from . import consumer_state as state
from . import host_admission as admission
from . import host_observation as host
from .consumer_profile import Target, identity, load_profile
from .local_kind import ROOT


def validate_request(target, request: dict, artifact_digest: str) -> None:
    """Bind the fresh admission to this artifact and the full enclosing node budget."""
    admission.validate(request)
    if request["target"] != target.name or request["artifactDigest"] != artifact_digest:
        raise ValueError("Admission target/artifact mismatch")
    workloads = [item for item in request["memory"] if item["kind"] == "workload"]
    if sum(item["limitBytes"] for item in workloads) != 12 * admission.GIB or any(
        "cgroup" in item for item in workloads
    ):
        raise ValueError("Fresh nodes require an uncredited full 12GiB workload reservation")
    if any(
        sum(item["limitBytes"] for item in request["disk"] if item["category"] == category) != bound
        for category, bound in admission.CATEGORIES.items()
    ):
        raise ValueError("Reserve the full reviewed disk category envelopes for bootstrap")


def preview(target, action: str = "create") -> dict:
    return {
        "action": action,
        "target": target.name,
        "profile": target.profile,
        "stage": "pre-cilium",
        "installedAcceptance": False,
        "execution": "not-performed",
        "effects": (
            "Delete exact journaled container IDs; retain evidence tombstone"
            if action == "down"
            else "Create absent owned nodes with caps before start; retain on failure"
        ),
    }


def environment(context: str, overlay: dict[str, str]) -> dict[str, str]:
    result = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("DOCKER_", "KIND_EXPERIMENTAL_", "HELM_KUBE", "BOUNDED_DOCKER_"))
        and key not in {"KUBECONFIG", "KUBERNETES_MASTER"}
    }
    return {**result, **overlay, "DOCKER_CONTEXT": context, "KIND_EXPERIMENTAL_PROVIDER": "docker"}


def validate_engine(info: dict) -> None:
    if (
        info.get("Driver") not in {"overlay2", "overlayfs"}
        or info.get("CgroupVersion") != "2"
        or info.get("CgroupDriver") != "systemd"
        or "desktop" in info.get("OperatingSystem", "").lower()
        or any(
            not info.get(key) for key in ("MemoryLimit", "SwapLimit", "CpuCfsQuota", "PidsLimit")
        )
    ):
        raise ValueError(
            "Only native systemd cgroup-v2 dockerd with verified overlay2/overlayfs stores is supported"
        )


def qualify(target, request: dict, expected_host_id: str) -> dict:
    if not target.consumer.startswith("qual-"):
        raise ValueError("Cold bootstrap qualification requires disposable qual- consumer")
    return create(target, request, expected_host_id, qualification=True)


def kernel_metrics(path, memory: int) -> dict:
    """Read kernel lifetime peaks without resetting them; refuse absent controllers."""

    def pairs(name: str) -> dict[str, int]:
        return {
            key: int(value)
            for key, value in (line.split() for line in (path / name).read_text().splitlines())
        }

    quota, period = map(int, (path / "cpu.max").read_text().split())
    if (
        int((path / "memory.max").read_text()) != memory
        or int((path / "memory.swap.max").read_text()) != 0
        or quota != 2 * period
        or period <= 0
        or int((path / "pids.max").read_text()) != 2048
    ):
        raise ValueError("Kernel controller caps differ from creation policy")
    peak = int((path / "memory.peak").read_text())
    events, cpu = pairs("memory.events"), pairs("cpu.stat")
    if not 0 < peak <= memory or any(events[key] != 0 for key in ("oom", "oom_kill")):
        raise ValueError("Kernel peak exceeded cap or OOM observed")
    return {
        "memoryPeakBytes": peak,
        "memoryEvents": events,
        "cpuStat": cpu,
        "cgroupIdentity": admission.digest([path.stat().st_dev, path.stat().st_ino]),
    }


def source_digest() -> str:
    """Bind actual executable source, profiles and lock, including uncommitted changes."""
    paths = sorted(
        [
            *(ROOT / "scripts").glob("*.py"),
            *(ROOT / "clusters/local-kind").rglob("*.yaml"),
            ROOT / "uv.lock",
        ]
    )
    return admission.digest(
        {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    )


def create(
    target: Target, request: dict, expected_host_id: str, *, qualification: bool = False
) -> dict:
    with runtime.deadline(900):
        return _create(target, request, expected_host_id, qualification=qualification)


def _create(
    target: Target, request: dict, expected_host_id: str, *, qualification: bool = False
) -> dict:
    if target.profile != "minimal-demo":
        raise ValueError("Dedicated bootstrap requires minimal-demo profile")
    validate_request(target, request, source_digest())
    # Imports follow pure refusal, so preview and invalid requests never need a provider.
    from . import bounded_docker

    endpoint = runtime.prerequisites(target, creating=True)
    versions = runtime.tools(target)
    from .consumer_lifecycle import source_identity

    provenance = source_identity(target)
    runtime.command(
        target, ["docker", "--context", target.docker_context, "network", "inspect", "kind"]
    )
    if any(
        artifact_path(target, suffix).exists()
        for suffix in ("bootstrap.json", "bootstrap.pending", "bounded-docker")
    ):
        raise ValueError("Prior bootstrap evidence exists; use a distinct absent target")
    if runtime.remaining_timeout(15) < 15:
        raise ValueError("Insufficient deadline for physical daemon proof")
    stores = physical_engine(target, endpoint)
    if target.name in runtime.clusters(target):
        raise ValueError("Existing target cannot be adopted")
    verify_stores(request, None, stores)
    if runtime.remaining_timeout(30) < 30:
        raise ValueError("Insufficient deadline for fresh host observation")
    observation = host.observe(request, expected_host_id=expected_host_id)
    verify_stores(request, observation, stores)
    decision = admission.evaluate(
        request, observation, host_id=host.host_id(), now=int(time.time())
    )
    if not decision["admitted"]:
        raise ValueError("Fresh physical host admission refused")
    runtime.remaining_timeout()
    record = state.reserve(target, endpoint)
    receipt = {
        **identity(target),
        "nonce": record["nonce"],
        "artifactDigest": request["artifactDigest"],
        "sourceProvenance": provenance,
        "nodeImage": load_profile(name=target.profile)["nodeImage"],
        "toolVersions": versions,
        "admission": decision,
        "stage": "pre-cilium",
        "phase": "creating",
        "installedAcceptance": False,
        "coldBootstrapQualified": False,
        "startedAt": int(time.time()),
        "nodes": [],
        "failure": None,
    }
    directory = artifact_path(target, "bounded-docker")
    started = time.monotonic()
    floor = [host.mem_available()]
    stop = threading.Event()

    def sample() -> None:
        while not stop.wait(0.5):
            try:
                floor.append(host.mem_available())
            except (OSError, ValueError):
                floor.append(0)

    sampler = threading.Thread(target=sample, daemon=True)
    try:
        write_receipt(target, receipt)
        overlay = bounded_docker.prepare(target, record["nonce"], directory)
        sampler.start()
        run_kind(target, overlay, runtime.remaining_timeout(600))
        journals = bounded_docker.read_journals(directory)
        verify_journals(target, record, journals)
        receipt["nodes"] = journals
        runtime.verify_context(target)
        nodes = runtime.node_identity(target)
        ids = {item["name"]: item["id"] for item in journals}
        if {name: node["id"] for name, node in nodes.items()} != ids:
            raise ValueError("Created node identities differ from prestart journals")
        record.update(nodeIds=ids, incarnation=runtime.incarnation(target), phase="provisioned")
        state.save(target, record)
        initial = node_metrics(target, journals)
        # Reconciliation restarts kubelet only; it never restarts the enclosing cgroup.
        receipt["allocatable"] = configure_capacity(target, runtime.remaining_timeout(280))
        check_allocatable(receipt["allocatable"], target, record["incarnation"])
        final = node_metrics(target, journals)
        if any(
            initial[name]["cgroupIdentity"] != final[name]["cgroupIdentity"]
            or initial[name]["startedAt"] != final[name]["startedAt"]
            for name in initial
        ):
            raise ValueError("Node cgroup lifetime changed during qualification")
        receipt["kernelEvidence"] = final
        receipt["toolingEvidence"] = tooling_metrics(request)
        final_memory = host.memory_observation(request)
        if any(
            final_memory[item["id"]].get("cgroupId")
            != observation["memory"][item["id"]].get("cgroupId")
            for item in request["memory"]
            if "cgroup" in item
        ):
            raise ValueError("Admitted memory account lifetime changed")
        stop.set()
        sampler.join(timeout=2)
        if (
            host.host_id() != expected_host_id
            or time.monotonic() - started > 900
            or source_digest() != receipt["artifactDigest"]
            or min(floor) < 4 * admission.GIB
        ):
            raise ValueError("Host lifetime or total bootstrap duration exceeded")
        runtime.remaining_timeout()
        receipt.update(phase="pre-cilium-allocatable", coldBootstrapQualified=qualification)
        return receipt
    except BaseException:
        record["phase"] = "partial"
        state.save(target, record)
        receipt.update(phase="partial", failure="bootstrap-refused-or-incomplete")
        raise
    finally:
        stop.set()
        if sampler.is_alive():
            sampler.join(timeout=2)
        receipt["sampledHostMemoryFloorBytes"] = min(floor)
        receipt["hostMemorySamplingIntervalSeconds"] = 0.5
        receipt["elapsedSeconds"] = round(time.monotonic() - started, 3)
        receipt["finishedAt"] = int(time.time())
        # The shim independently retained immutable IDs before start even if kind failed.
        if directory.exists():
            receipt["nodes"] = bounded_docker.read_journals(directory)
        write_receipt(target, receipt)


def artifact_path(target: Target, suffix: str) -> Path:
    if suffix not in {"bounded-docker", "bootstrap.json", "bootstrap.pending"}:
        raise ValueError("Unknown bootstrap artifact")
    path = target.directory / f"{target.name}.{suffix}"
    if any(p.is_symlink() for p in (path, path.parent)):
        raise ValueError("Redirected bootstrap artifact")
    return path


def write_receipt(target: Target, receipt: dict) -> None:
    path = artifact_path(target, "bootstrap.json")
    temporary = artifact_path(target, "bootstrap.pending")
    with temporary.open("x", encoding="utf8") as handle:
        temporary.chmod(0o600)
        json.dump(receipt, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def run_kind(target: Target, overlay: dict[str, str], timeout: int) -> None:
    from .process_tree import terminate

    args = [
        "kind",
        "create",
        "cluster",
        "--name",
        target.name,
        "--config",
        str(target.path("kind.yaml")),
        "--kubeconfig",
        str(target.path("kubeconfig")),
        "--wait",
        "0s",
        "--retain",
    ]
    with subprocess.Popen(
        args,
        env=environment(target.docker_context, overlay),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    ) as process:
        try:
            code = process.wait(timeout=timeout)
        except BaseException:
            terminate(process)
            raise
    if code:
        raise ValueError("Bounded kind failed; retain private target evidence")


def configure_capacity(target: Target, timeout: int) -> dict:
    """Reuse reconciliation under the same deadline and owned subprocess cleanup."""
    from .consumer_capacity import configure

    with runtime.deadline(timeout):
        return configure(target)


def verify_journals(target: Target, owner: dict, journals: list[dict]) -> None:
    expected = {f"{target.name}-{role}" for role in ("control-plane", "worker")}
    if (
        len(journals) != 2
        or {item["name"] for item in journals} != expected
        or any(
            item["nonce"] != owner["nonce"]
            or item["target"] != target.name
            or item["phase"] != "started"
            for item in journals
        )
    ):
        raise ValueError("Incomplete or foreign prestart journal")


def node_metrics(target: Target, journals: list[dict]) -> dict:
    result = {}
    for journal in journals:
        node = runtime.inspect_node(target, journal["id"], journal["name"])
        runtime.verify_nodes({journal["name"]: node}, profile=target.profile)
        data = json.loads(
            runtime.command(
                target,
                [
                    "docker",
                    "--context",
                    target.docker_context,
                    "inspect",
                    "--format",
                    '{"pid":{{.State.Pid}},"running":{{.State.Running}},"restarts":{{.RestartCount}},"startedAt":{{json .State.StartedAt}}}',
                    journal["id"],
                ],
            )
        )
        if not data["running"] or data["restarts"] != 0 or data["pid"] <= 0:
            raise ValueError("Fresh node lifetime is incomplete or restarted")
        lines = Path(f"/proc/{data['pid']}/cgroup").read_text().splitlines()
        instant = (
            datetime.fromisoformat(data["startedAt"].replace("Z", "+00:00")).timestamp()
            * 1_000_000_000
        )
        if (
            not journal["startRequestedAt"] - 1_000_000
            <= instant
            <= journal["startCompletedAt"] + 1_000_000
        ):
            raise ValueError("Container start lifetime differs from first journaled start")
        paths = [line[4:] for line in lines if line.startswith("0::/")]
        if len(paths) != 1 or journal["id"] not in paths[0]:
            raise ValueError("Cannot bind physical node cgroup to immutable container")
        path = host.CGROUP_ROOT / paths[0]
        memory = load_profile(name=target.profile)["nodes"][journal["role"]]["memoryBytes"]
        if node["swap"] != memory:
            raise ValueError("Node swap cap differs")
        result[journal["name"]] = {
            **kernel_metrics(path, memory),
            "id": journal["id"],
            "startedAt": data["startedAt"],
        }
    return result


def tooling_metrics(request: dict) -> list[dict]:
    result = []
    for item in request["memory"]:
        if item["kind"] == "tooling":
            path = Path(item["cgroup"])
            peak = int((path / "memory.peak").read_text())
            events = dict(
                line.split() for line in (path / "memory.events").read_text().splitlines()
            )
            if peak > item["limitBytes"] or any(int(events[key]) for key in ("oom", "oom_kill")):
                raise ValueError("Tooling lifetime peak or OOM exceeds admission")
            result.append(
                {"id": item["id"], "lifetimeMemoryPeakBytes": peak, "memoryEvents": events}
            )
    return result


def native_process(pid: int, executable: str) -> list[str]:
    """Privileged observation may be required; unknown namespaces never imply locality."""
    proc = Path(f"/proc/{pid}")
    if (
        proc.joinpath("exe").resolve(strict=True).name != executable
        or proc.joinpath("ns/mnt").stat().st_ino != Path("/proc/self/ns/mnt").stat().st_ino
        or proc.joinpath("root").stat().st_dev != Path("/").stat().st_dev
        or proc.joinpath("root").stat().st_ino != Path("/").stat().st_ino
    ):
        raise ValueError("Daemon is not on the observed physical host")
    return [value.decode() for value in proc.joinpath("cmdline").read_bytes().split(b"\0") if value]


def peer(socket_path: str) -> int:
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(5)
        connection.connect(socket_path)
        pid, uid, _ = struct.unpack(
            "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )
    if uid != 0:
        raise ValueError("Only rootful native daemons supported")
    return pid


def option(args: list[str], name: str, default: str) -> str:
    matches = [arg.split("=", 1)[1] for arg in args if arg.startswith(name + "=")]
    matches += [args[index + 1] for index, arg in enumerate(args[:-1]) if arg == name]
    if len(matches) > 1:
        raise ValueError("Ambiguous daemon configuration")
    return matches[0] if matches else default


def physical_engine(target: Target, endpoint: str) -> dict[str, Path]:
    if not endpoint.startswith("unix://"):
        raise ValueError("Native Unix Docker socket required")
    socket_path = endpoint.removeprefix("unix://")
    pid = peer(socket_path)
    if pid == 1:
        # systemd's activated listener retains PID1 credentials. Bind the selected
        # listener inode to the service MainPID rather than trusting the service name.
        pid = int(
            runtime.command(
                target, ["systemctl", "show", "docker.service", "--property=MainPID", "--value"]
            ).strip()
        )
        inodes = {
            row.split()[6]
            for row in Path("/proc/net/unix").read_text().splitlines()[1:]
            if len(row.split()) == 8
            and Path(row.split()[7]).resolve() == Path(socket_path).resolve()
        }
        owned = {os.readlink(path) for path in Path(f"/proc/{pid}/fd").iterdir()}
        if not inodes or not any(f"socket:[{inode}]" in owned for inode in inodes):
            raise ValueError("Cannot bind activated Docker listener to native daemon")
    args = native_process(pid, "dockerd")
    info = json.loads(
        runtime.command(
            target, ["docker", "--context", target.docker_context, "info", "--format", "{{json .}}"]
        )
    )
    validate_engine(info)
    if any(
        "rootless" in option or "userns" in option for option in info.get("SecurityOptions", [])
    ) or any(info.get(key) for key in ("HttpProxy", "HttpsProxy")):
        raise ValueError("Rootless, userns and proxy Docker paths unsupported")
    root = host.ordinary_path(info["DockerRootDir"])
    stores = {"runtime": root, "images": root}
    if info["Driver"] == "overlayfs":
        containerd_socket = option(
            args, "--containerd", "/var/run/docker/containerd/containerd.sock"
        )
        containerd_args = native_process(peer(containerd_socket), "containerd")
        config = Path(
            option(
                containerd_args,
                "--config",
                option(containerd_args, "-c", "/etc/containerd/config.toml"),
            )
        )
        data = tomllib.loads(config.read_text()) if config.exists() else {}
        if data.get("imports"):
            raise ValueError("Imported containerd store configuration unsupported")
        stores["images"] = host.ordinary_path(
            option(containerd_args, "--root", str(data.get("root", "/var/lib/containerd")))
        )
    for path in stores.values():
        host.physical_filesystem(path)
    return stores


def verify_stores(request: dict, observation: dict | None, stores: dict[str, Path]) -> None:
    """Charge shared cache through free space; never credit its existing images."""
    for category, store in stores.items():
        accounts = [item for item in request["disk"] if item["category"] == category]
        if not accounts:
            raise ValueError("Missing physical Docker store account")
        for item in accounts:
            path = host.ordinary_path(item["path"])
            if path.stat().st_dev != store.stat().st_dev or any(
                path.is_relative_to(other) or other.is_relative_to(path)
                for other in stores.values()
            ):
                raise ValueError(
                    "Use disjoint reservation directories on the Docker store filesystem"
                )
            if observation is not None:
                # Even empty directories occupy blocks. Deliberately discard that
                # credit so reservations cover the entire future image/runtime growth.
                observation["disk"][item["id"]]["residentBytes"] = 0


def check_allocatable(result: dict, target: Target, incarnation: str) -> None:
    expected = {
        "control-plane": {"cpuMillicores": 1750, "memoryBytes": 3484 * 1024**2},
        "worker": {"cpuMillicores": 1750, "memoryBytes": 7580 * 1024**2},
    }
    if (
        result.get("cluster") != target.name
        or result.get("incarnation") != incarnation
        or {role: node.get("schedulerEnvelope") for role, node in result.get("nodes", {}).items()}
        != expected
    ):
        raise ValueError("Observed allocatable differs from the candidate envelope")


def down(target: Target, confirmation: str | None) -> dict:
    with runtime.deadline(180):
        return _down(target, confirmation)


def _down(target: Target, confirmation: str | None) -> dict:
    """Delete only immutable journal IDs; API absence does not invalidate partial ownership."""
    from . import bounded_docker

    if confirmation != target.name:
        raise ValueError("Deletion requires exact --confirm-delete target name")
    if target.profile != "minimal-demo":
        raise ValueError("Dedicated removal requires minimal-demo profile")
    owner = state.load(target)
    if runtime.prerequisites(target) != owner["endpoint"]:
        raise ValueError("Docker endpoint changed")
    directory = artifact_path(target, "bounded-docker")
    journals = bounded_docker.read_journals(directory)
    names = {f"{target.name}-{role}" for role in ("control-plane", "worker")}
    if any(
        item.get("name") not in names
        or item.get("target") != target.name
        or item.get("nonce") != owner["nonce"]
        for item in journals
    ):
        raise ValueError("Foreign partial receipt")
    if any(not (directory / f"{claim.stem}.json").exists() for claim in directory.glob("*.claim")):
        raise ValueError("Uncertain Docker create lacks immutable ID; explicit diagnosis required")
    recorded = {item["id"] for item in journals}
    present = set(
        runtime.command(
            target,
            [
                "docker",
                "--context",
                target.docker_context,
                "ps",
                "--all",
                "--no-trunc",
                "--filter",
                f"label=io.x-k8s.kind.cluster={target.name}",
                "--format",
                "{{.ID}}",
            ],
        ).splitlines()
    )
    if not present.issubset(recorded):
        raise ValueError("Unrecorded container exists; refusing adoption or removal")
    # Preflight every survivor before the first destructive command.
    for item in journals:
        if item["id"] in present:
            node = runtime.inspect_node(target, item["id"], item["name"])
            if node["id"] != item["id"]:
                raise ValueError("Immutable removal identity mismatch")
    owner["phase"] = "deleting"
    state.save(target, owner)
    for item in journals:
        if item["id"] in present:
            runtime.command(
                target,
                [
                    "docker",
                    "--context",
                    target.docker_context,
                    "rm",
                    "--force",
                    "--volumes",
                    item["id"],
                ],
            )
    remaining = runtime.command(
        target,
        [
            "docker",
            "--context",
            target.docker_context,
            "ps",
            "--all",
            "--filter",
            f"label=io.x-k8s.kind.cluster={target.name}",
            "--format",
            "{{.ID}}",
        ],
    )
    if remaining.strip():
        raise ValueError("Target containers remain; retain deletion evidence")
    receipt = json.loads(state.read_bounded(artifact_path(target, "bootstrap.json")))
    receipt.update(phase="removed", coldBootstrapQualified=False, removedAt=int(time.time()))
    write_receipt(target, receipt)
    # Keep owner/config and receipts as a tombstone. Requalification uses a new name.
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "qualify", "down", "source-digest"))
    parser.add_argument("--consumer")
    parser.add_argument("--docker-context")
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--host-id")
    parser.add_argument("--confirm-delete")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.action == "source-digest":
            print(source_digest())
            return 0
        if not args.consumer or not args.docker_context or not args.state_root:
            raise ValueError("Explicit target required")
        target = Target(args.consumer, args.docker_context, args.state_root, "minimal-demo")
        if not args.execute:
            result = preview(target, args.action)
        elif args.action == "down":
            result = down(target, args.confirm_delete)
        else:
            if not args.request or not args.host_id:
                raise ValueError("Fresh admission inputs required")
            request = json.loads(state.read_bounded(args.request))
            result = (qualify if args.action == "qualify" else create)(
                target, request, args.host_id
            )
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, TimeoutError, subprocess.SubprocessError):
        print(
            "Minimal bootstrap refused or incomplete; retain private owner, shim journals and bootstrap receipt. No cleanup performed."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
