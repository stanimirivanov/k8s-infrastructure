"""Observe bounded resource peaks on an explicitly selected disposable kind target."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, nullcontext, redirect_stdout, suppress
from dataclasses import dataclass, field
from pathlib import Path

from . import bootstrap_failure as failure
from . import bounded_docker, consumer_lifecycle, host_observation
from . import consumer_runtime as runtime
from . import consumer_state as state
from . import minimal_bootstrap as bootstrap
from . import peak_observation as observation
from .consumer_profile import MINIMAL_PROFILE, Target, identity, load_profile
from .host_admission import GIB, digest

SCHEMA = "resource-peaks.v1"
SECOND = observation.SECOND
MAX_DURATION_SECONDS = 1800
MAX_INTERVAL_SECONDS = 60
MAX_SAMPLES = 61
IDENTITY_TIMEOUT = 90
RECEIPT_SETUP_TIMEOUT = 15
RECEIPT_FINAL_TIMEOUT = 5
SAMPLING_GRACE = 20
MAX_RECEIPT_BYTES = 64 * 1024**2
RECEIPT_ALLOCATION_MARGIN = 1024**2

CPU_FIELDS = {
    "usage_usec": "usageUsec",
    "nr_periods": "periods",
    "nr_throttled": "throttledPeriods",
    "throttled_usec": "throttledUsec",
}


def counter(text: str) -> int:
    if re.fullmatch(r"[0-9]{1,20}", text) is None:
        raise ValueError("Invalid kernel counter")
    value = int(text)
    if value >= 2**64:
        raise ValueError("Kernel counter overflow")
    return value


def read_at(directory: int, name: str) -> str:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    with os.fdopen(descriptor, encoding="ascii") as file:
        text = file.read(8193)
    if len(text) > 8192:
        raise ValueError("Kernel counter document too large")
    return text


def file_identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def read_cgroup(path: Path, *, with_population: bool = False) -> dict:
    """Read cgroup-v2 native counters, holding the same directory for every file."""
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    population = {}
    try:
        identity = file_identity(os.fstat(descriptor))
        memory = counter(read_at(descriptor, "memory.current").strip())
        peak = counter(read_at(descriptor, "memory.peak").strip())
        cpu = {}
        for line in read_at(descriptor, "cpu.stat").splitlines():
            name, value = line.split()
            if name in CPU_FIELDS:
                if name in cpu:
                    raise ValueError("Duplicate kernel counter")
                cpu[name] = counter(value)
        if peak < memory or cpu.keys() != CPU_FIELDS.keys():
            raise ValueError("Missing or inconsistent cgroup counters")
        if with_population:
            events = dict(
                line.split() for line in read_at(descriptor, "cgroup.events").splitlines()
            )
            status = events.get("populated")
            if status not in {"0", "1"}:
                raise ValueError("Cgroup population unavailable")
            population["populated"] = status == "1"
        if file_identity(path.stat(follow_symlinks=False)) != identity:
            raise ValueError("Cgroup lifetime changed during observation")
    finally:
        os.close(descriptor)
    return {
        "cgroupIdentity": digest(list(identity)),
        "memoryCurrentBytes": memory,
        "lifetimeMemoryPeakBytes": peak,
        "cpu": {label: cpu[name] for name, label in CPU_FIELDS.items()},
        **population,
    }


def summarize_cgroup(samples: list[dict | None], *, max_gap_ns: int) -> dict:
    """Preserve observed memory when coverage is incomplete; withhold invalid CPU deltas."""
    observed = [sample for sample in samples if sample is not None]
    if not observed:
        return {
            "state": "missing",
            "reasons": ["missing-sample"],
            "sampledMemoryMaxBytes": None,
            "lifetimeMemoryPeakBytes": None,
            "intervalCpuDelta": None,
            "cpuIntervalElapsedNs": None,
        }
    reasons = []
    identities = {sample["cgroupIdentity"] for sample in observed}
    if len(identities) != 1:
        reasons.append("cgroup-lifetime-changed")
    if len(observed) != len(samples):
        reasons.append("missing-sample")
    if len(observed) < 2:
        reasons.append("missing-endpoint")
    if any(
        not 0 < after["elapsedNs"] - before["elapsedNs"] <= max_gap_ns
        for before, after in zip(observed, observed[1:], strict=False)
    ):
        reasons.append("stale-or-unordered-sample")
    peak_reset = any(
        after["lifetimeMemoryPeakBytes"] < before["lifetimeMemoryPeakBytes"]
        for before, after in zip(observed, observed[1:], strict=False)
    )
    if peak_reset or any(
        any(after["cpu"][key] < before["cpu"][key] for key in CPU_FIELDS.values())
        for before, after in zip(observed, observed[1:], strict=False)
    ):
        reasons.append("kernel-counter-regressed")
    first, last = observed[0], observed[-1]
    return {
        "state": "incomplete" if reasons else "observed",
        "reasons": reasons,
        "cgroupIdentity": next(iter(identities)) if len(identities) == 1 else None,
        "sampledMemoryMaxBytes": max(sample["memoryCurrentBytes"] for sample in observed),
        "lifetimeMemoryPeakBytes": last["lifetimeMemoryPeakBytes"]
        if len(identities) == 1 and not peak_reset
        else None,
        "intervalCpuDelta": (
            {key: last["cpu"][key] - first["cpu"][key] for key in CPU_FIELDS.values()}
            if not reasons
            else None
        ),
        "cpuIntervalElapsedNs": [first["elapsedNs"], last["elapsedNs"]] if not reasons else None,
    }


def observe_tooling(request: dict, expected: dict | None = None) -> dict:
    """Verify the current collector is charged to existing enforced tooling accounts."""
    identity = host_observation.memory_observation(request)
    result = {}
    for account in request["memory"]:
        if account["kind"] != "tooling":
            continue
        name = account["id"]
        path = Path(account["cgroup"])
        metric = read_cgroup(path)
        if metric["cgroupIdentity"] != identity[name]["cgroupId"] or (
            expected is not None and metric["cgroupIdentity"] != expected[name]
        ):
            raise failure.IdentityMismatch("Tooling account lifetime changed")
        events = dict(line.split() for line in (path / "memory.events").read_text().splitlines())
        if metric["lifetimeMemoryPeakBytes"] > account["limitBytes"] or any(
            counter(events[key]) for key in ("oom", "oom_kill")
        ):
            raise ValueError("Tooling peak or OOM exceeds admitted budget")
        result[name] = {
            **metric,
            "memoryLimitBytes": account["limitBytes"],
            "cpuLimitMillicores": account["cpuMillicores"],
        }
    return result


@dataclass
class Measurement:
    owner: dict
    kubeconfig: str = field(repr=False)
    node_paths: dict[str, Path]
    nodes: dict
    store_paths: dict[str, Path]
    stores: dict
    tooling_ids: dict
    initial_tooling: dict
    provenance: dict


def owned_state(target: Target) -> dict:
    try:
        return state.load(target)
    except ValueError:
        raise failure.IdentityMismatch("Exact consumer ownership is required") from None


def bootstrap_identity(target: Target, request: dict, host_id: str) -> tuple[dict, dict, str]:
    """Check exact admission and source using private local state only."""
    artifact = bootstrap.source_digest()
    bootstrap.validate_request(target, request, artifact)
    expected_host = host_observation.host_id()
    if expected_host != host_id:
        raise failure.IdentityMismatch("Host boot identity changed")
    owner = owned_state(target)
    cold = json.loads(state.read_bounded(bootstrap.artifact_path(target, "bootstrap.json")))
    admission = cold.get("admission", {})
    image = load_profile(name=target.profile)["nodeImage"]
    if (
        owner["phase"] != "provisioned"
        or any(cold.get(key) != value for key, value in identity(target).items())
        or cold.get("coldBootstrapQualified") is not True
        or cold.get("stage") != "pre-cilium"
        or cold.get("phase") != "pre-cilium-allocatable"
        or cold.get("nonce") != owner["nonce"]
        or cold.get("artifactDigest") != artifact
        or cold.get("nodeImage") != image
        or admission.get("requestDigest") != digest(request)
        or admission.get("hostId") != host_id
        or admission.get("admitted") is not True
    ):
        raise failure.IdentityMismatch(
            "Exact successful cold bootstrap and admitted request required"
        )
    return owner, cold, artifact


def tooling_endpoint(request: dict, expected: dict) -> dict:
    return {
        key: {**value, "elapsedNs": time.monotonic_ns()}
        for key, value in observe_tooling(request, expected).items()
    }


def bind(target: Target, request: dict, host_id: str) -> Measurement:
    """Refuse mismatched bootstrap admission before accessing the owned target."""
    owner, cold, artifact = bootstrap_identity(target, request, host_id)
    admission = cold["admission"]
    tooling_ids = cold["memoryAccountIdentities"]
    tooling = tooling_endpoint(request, tooling_ids)
    if runtime.prerequisites(target) != owner["endpoint"] or target.name not in runtime.clusters(
        target
    ):
        raise failure.IdentityMismatch("Owned Docker endpoint or target changed")
    runtime_nodes = runtime.node_identity(target)
    if {name: node["id"] for name, node in runtime_nodes.items()} != owner["nodeIds"]:
        raise failure.IdentityMismatch("Owned node containers changed")
    try:
        kubeconfig = runtime.verify_context(target)
    except runtime.CommandFailure:
        raise
    except ValueError:
        raise failure.IdentityMismatch("Exact consumer context is required") from None
    observation.verify_incarnation(target, kubeconfig, owner["incarnation"])
    journals = bounded_docker.read_journals(bootstrap.artifact_path(target, "bounded-docker"))
    bootstrap.verify_journals(target, owner, journals)
    if (
        journals != cold["nodes"]
        or {item["name"]: item["id"] for item in journals} != owner["nodeIds"]
    ):
        raise failure.IdentityMismatch("Cold-bootstrap node journals changed")
    bootstrap.check_allocatable(cold["allocatable"], target, owner["incarnation"])
    node_evidence, node_paths = bootstrap._node_metrics(target, journals)
    nodes = {
        name: {key: metric[key] for key in ("id", "startedAt", "cgroupIdentity")}
        for name, metric in node_evidence.items()
    }
    if any(
        any(cold["kernelEvidence"][name][key] != value for key, value in node.items())
        for name, node in nodes.items()
    ):
        raise failure.IdentityMismatch("Node lifetime differs from cold bootstrap")
    store_paths = observation.physical_paths(target, owner["endpoint"], request)
    stores = observation.physical_ids(store_paths, host_id)
    if {item["filesystemId"] for item in stores.values()} != {
        item["filesystemId"] for item in admission["filesystems"]
    }:
        raise failure.IdentityMismatch("Physical filesystems differ from cold bootstrap")
    return Measurement(
        owner,
        kubeconfig,
        node_paths,
        nodes,
        store_paths,
        stores,
        tooling_ids,
        tooling,
        {
            "artifactDigest": artifact,
            "provider": consumer_lifecycle.source_identity(target),
            "requestDigest": admission["requestDigest"],
            "bootstrapReceiptDigest": digest(cold),
            "hostId": host_id,
            "ownerNonce": owner["nonce"],
            "incarnation": owner["incarnation"],
            "nodeImage": cold["nodeImage"],
            "nodes": nodes,
            "stores": stores,
        },
    )


def cgroup_sample(
    path: Path, elapsed: Callable[[], int], *, with_population: bool = False
) -> dict | None:
    try:
        return {**read_cgroup(path, with_population=with_population), "elapsedNs": elapsed()}
    except (OSError, ValueError):
        return None


def sample(
    target: Target,
    request: dict,
    bound: Measurement,
    elapsed: Callable[[], int],
    window_start_ns: int,
    interval_seconds: int,
) -> dict:
    started = elapsed()
    check_timeout = min(5, interval_seconds)
    if (
        host_observation.host_id() != bound.provenance["hostId"]
        or owned_state(target) != bound.owner
    ):
        raise failure.IdentityMismatch("Host or consumer ownership changed")
    observation.verify_incarnation(
        target, bound.kubeconfig, bound.owner["incarnation"], check_timeout
    )
    rows = observation.inventory(
        target, bound.kubeconfig, set(bound.nodes), window_start_ns, timeout=check_timeout
    )
    if any(
        timestamp is not None and timestamp > time.time_ns() + SECOND
        for row in rows
        for timestamp in (row["startedAtNs"], row["finishedAtNs"])
    ):
        raise ValueError("Pod attempt timestamp is in the future")
    counters = {}
    for node, path in bound.node_paths.items():
        metric = cgroup_sample(path, elapsed)
        if metric is not None and metric["cgroupIdentity"] != bound.nodes[node]["cgroupIdentity"]:
            raise failure.IdentityMismatch("Node cgroup lifetime changed")
        counters[node] = metric
    scopes = {node: observation.container_scopes(path) for node, path in bound.node_paths.items()}
    containers = []
    for row in rows:
        cgroup = scopes[row["node"]].get(row["containerId"])
        metric = (
            cgroup_sample(cgroup, elapsed, with_population=True) if cgroup is not None else None
        )
        counter_state = (
            "observed"
            if metric is not None
            else "kernel-counter-unavailable"
            if cgroup is not None
            else "scope-unavailable"
            if row["containerId"] is not None
            else "attempt-unavailable"
        )
        containers.append({**row, "counters": metric, "counterState": counter_state})
    attributed = {(row["node"], row["containerId"]) for row in rows}
    unattributed = [
        {
            "node": node,
            "scopeId": reference,
            "attributionState": "unavailable",
            "counters": cgroup_sample(path, elapsed, with_population=True),
        }
        for node, node_scopes in scopes.items()
        for reference, path in sorted(node_scopes.items())
        if (node, reference) not in attributed
    ]
    host = {
        "memAvailableBytes": host_observation.mem_available(),
        "elapsedNs": elapsed(),
    }
    filesystems = observation.observe_filesystems(
        bound.store_paths, bound.stores, bound.provenance["hostId"]
    )
    disk_elapsed = elapsed()
    tooling = {
        key: {**metric, "elapsedNs": elapsed()}
        for key, metric in observe_tooling(request, bound.tooling_ids).items()
    }
    observation.verify_incarnation(
        target, bound.kubeconfig, bound.owner["incarnation"], check_timeout
    )
    if (
        host_observation.host_id() != bound.provenance["hostId"]
        or owned_state(target) != bound.owner
    ):
        raise failure.IdentityMismatch("Host or consumer ownership changed during sample")
    finished = elapsed()
    if abs(time.time_ns() - (window_start_ns + finished)) > SECOND:
        raise ValueError("Host wall clock changed relative to monotonic collection time")
    return {
        "startedElapsedNs": started,
        "finishedElapsedNs": finished,
        "nodes": counters,
        "containers": containers,
        "unattributedRuntimeScopes": unattributed,
        "tooling": tooling,
        "host": host,
        "diskElapsedNs": disk_elapsed,
        "filesystems": filesystems,
        "initOverlap": init_overlap(containers, counters),
    }


def init_overlap(containers: list[dict], nodes: dict) -> list[dict]:
    """Report same-frame, same-node co-observation without summing independent peaks."""
    active: dict[str, list[dict]] = {}
    for container in containers:
        if (
            container["state"] == "running"
            and container["counters"] is not None
            and container["counters"]["populated"]
        ):
            active.setdefault(container["node"], []).append(container)
    result = []
    for node, rows in active.items():
        metric = nodes[node]
        for row in rows:
            if row["kind"] == "init":
                result.append(
                    {
                        "node": node,
                        "initContainerId": row["containerId"],
                        "restartableInit": row["restartableInit"],
                        "coObservedContainerIds": [
                            other["containerId"] for other in rows if row is not other
                        ],
                        "nodeCountersElapsedNs": metric["elapsedNs"]
                        if metric is not None
                        else None,
                        "nodeMemoryCurrentBytes": metric["memoryCurrentBytes"]
                        if metric is not None
                        else None,
                    }
                )
    return result


def summarize_attempts(
    samples: list[dict], window_start_ns: int, max_gap: int, missing: set[str]
) -> tuple[dict, int]:
    attempts: dict[str, dict[int, dict]] = {}
    waiting: dict[tuple[str, str, str], int] = {}
    attempted: set[tuple[str, str, str]] = set()
    restarts: dict[tuple[str, str, str], int] = {}
    for index, frame in enumerate(samples):
        for row in frame["containers"]:
            logical = (row["podUid"], row["kind"], row["containerName"])
            restart_count = row["restartCount"]
            if restart_count is not None:
                if logical in restarts and restart_count not in {
                    restarts[logical],
                    restarts[logical] + 1,
                }:
                    missing.add("container-restart-history-gap")
                restarts[logical] = restart_count
            if row["state"] == "terminated" and (
                row["startedAtNs"] is None or row["finishedAtNs"] is None
            ):
                missing.add("container-attempt-chronology-unavailable")
            reference = row["containerId"]
            if reference is not None:
                attempts.setdefault(reference, {})[index] = row
                attempted.add(logical)
            elif row["state"] == "waiting" and restart_count == 0:
                waiting[logical] = index
            else:
                missing.add("container-attempt-unavailable")

    containers = {}
    for reference, rows in attempts.items():
        first = next(iter(rows.values()))
        metadata = {
            key: first[key]
            for key in (
                "node",
                "podUid",
                "podName",
                "namespace",
                "containerName",
                "kind",
                "restartableInit",
            )
        }
        starts = {row["startedAtNs"] for row in rows.values() if row["startedAtNs"] is not None}
        finishes = {row["finishedAtNs"] for row in rows.values() if row["finishedAtNs"] is not None}
        started = next(iter(starts)) if len(starts) == 1 else None
        finished = next(iter(finishes)) if len(finishes) == 1 else None
        if (
            len(starts) > 1
            or len(finishes) > 1
            or any(
                any(row[key] != value for key, value in metadata.items()) for row in rows.values()
            )
        ):
            missing.add("container-attempt-identity-changed")
        counters = []
        coverage_reasons = set()
        termination_seen = False
        for index, frame in enumerate(samples):
            if termination_seen:
                break
            row = rows.get(index)
            if row is not None:
                if row["state"] == "terminated":
                    if row["startedAtNs"] is None or row["finishedAtNs"] is None:
                        coverage_reasons.add("container-attempt-chronology-unavailable")
                    if row["counters"] is None or row["counters"].get("populated") is not False:
                        coverage_reasons.add("termination-native-coverage-missing")
                    termination_seen = True
                if row["state"] == "running" or row["counters"] is not None:
                    counters.append(row["counters"])
            elif (
                started is not None
                and started < window_start_ns + frame["finishedElapsedNs"]
                and (
                    finished is None
                    or finished + SECOND > window_start_ns + frame["startedElapsedNs"]
                )
            ):
                # A missing in-lifetime observation may signal stale status or a
                # deleted Pod. Known waiting/finished endpoints are outside the attempt.
                counters.append(None)
        summary = summarize_cgroup(counters, max_gap_ns=max_gap)
        if coverage_reasons:
            summary.update(
                state="incomplete",
                reasons=[*summary["reasons"], *sorted(coverage_reasons)],
                lastObservedLifetimeMemoryPeakBytes=summary["lifetimeMemoryPeakBytes"],
                observedCpuDelta=summary["intervalCpuDelta"],
                observedCpuIntervalElapsedNs=summary["cpuIntervalElapsedNs"],
                lifetimeMemoryPeakBytes=None,
                intervalCpuDelta=None,
                cpuIntervalElapsedNs=None,
            )
        containers[reference] = {
            **metadata,
            "startedAtNs": started,
            "finishedAtNs": finished,
            **summary,
        }

    pending = {key: index for key, index in waiting.items() if key not in attempted}
    if any(index != len(samples) - 1 for index in pending.values()):
        missing.add("pending-container-status-lost")
    return containers, sum(index == len(samples) - 1 for index in pending.values())


def summarize(samples: list[dict], interval_seconds: int, *, window_start_ns: int = 0) -> dict:
    max_gap = 2 * interval_seconds * SECOND
    groups = {
        kind: {
            name: summarize_cgroup(
                [sample[kind].get(name) for sample in samples], max_gap_ns=max_gap
            )
            for name in sorted(set().union(*(sample[kind].keys() for sample in samples)))
        }
        for kind in ("nodes", "tooling")
    }
    missing: set[str] = set()
    if any(
        frame["finishedElapsedNs"] - frame["startedElapsedNs"] > interval_seconds * SECOND
        for frame in samples
    ):
        missing.add("sample-scan-exceeded-interval")
    containers, pending = summarize_attempts(samples, window_start_ns, max_gap, missing)
    groups["containers"] = containers
    for kind, metrics in groups.items():
        if any(value["state"] != "observed" for value in metrics.values()):
            missing.add(f"{kind}-coverage-incomplete")
    if not containers:
        missing.add("no-container-attempts-observed")
    if (
        any(
            not 0 < after["startedElapsedNs"] - before["startedElapsedNs"] <= max_gap
            for before, after in zip(samples, samples[1:], strict=False)
        )
        or len(samples) < 2
    ):
        missing.add("stale-or-insufficient-samples")
    host_min = min((frame["host"]["memAvailableBytes"] for frame in samples), default=None)
    filesystems = {}
    for key in sorted(set().union(*(frame["filesystems"].keys() for frame in samples))):
        values = [frame["filesystems"][key] for frame in samples]
        filesystems[key] = {
            "sampledUsedMaxBytes": max(value["usedBytes"] for value in values),
            "sampledAvailableMinBytes": min(value["availableBytes"] for value in values),
        }
    overlap = [entry for frame in samples for entry in frame["initOverlap"]]
    concurrent = [
        item["nodeMemoryCurrentBytes"]
        for item in overlap
        if item["coObservedContainerIds"] and item["nodeMemoryCurrentBytes"] is not None
    ]
    missing_init = any(
        item["kind"] == "init" and item["state"] != "observed" for item in containers.values()
    ) or any(
        item["kind"] == "init" and item["state"] != "waiting" and item["containerId"] is None
        for frame in samples
        for item in frame["containers"]
    )
    unattributed_ids = set()
    unattributed_with_counters = 0
    unattributed_without_counters = 0
    for frame in samples:
        for scope in frame.get("unattributedRuntimeScopes", []):
            unattributed_ids.add((scope["node"], scope["scopeId"]))
            unattributed_with_counters += scope["counters"] is not None
            unattributed_without_counters += scope["counters"] is None

    return {
        "state": "incomplete" if missing else "observed",
        "reasons": sorted(missing),
        "runtimeAttribution": {
            "state": "incomplete" if unattributed_ids else "observed" if samples else "missing",
            "reasons": ["runtime-scope-attribution-unavailable"]
            if unattributed_ids
            else ["no-scope-inventory-samples"]
            if not samples
            else [],
            "unattributedScopeCount": len(unattributed_ids),
            "unattributedScopeObservationsWithCounters": unattributed_with_counters,
            "unattributedScopeObservationsWithoutCounters": unattributed_without_counters,
        },
        "sampleCount": len(samples),
        "pendingContainerCount": pending,
        **groups,
        "hostSampledMemAvailableMinBytes": host_min,
        "filesystems": filesystems,
        "sampledPressure": {
            "memoryFloorBreached": host_min < 4 * GIB if host_min is not None else None,
            "diskFloorBreachedFilesystemIds": [
                key
                for key, value in filesystems.items()
                if value["sampledAvailableMinBytes"] < 10 * GIB
            ]
            if filesystems
            else None,
        },
        "initOverlap": {
            "state": "incomplete"
            if missing_init
            else "co-observed"
            if concurrent
            else "not-observed",
            "initObservations": len(overlap),
            "sampledConcurrentNodeMemoryMaxBytes": max(concurrent, default=None),
        },
    }


def check_window(duration: int, interval: int) -> None:
    if not (
        10 <= duration <= MAX_DURATION_SECONDS
        and 2 <= interval <= MAX_INTERVAL_SECONDS
        and duration % interval == 0
        and 1 + duration // interval <= MAX_SAMPLES
    ):
        raise ValueError("A divisible bounded interval and at most 61 samples are required")


class OperationDeadline(BaseException):
    """Interrupt local reads even through fallback handlers for missing counters."""


@contextmanager
def operation_limit(seconds: int) -> Iterator[None]:
    """Give local reads a wall-clock limit and preserve partial receipts on SIGTERM."""

    def timed_out(_signum: int, _frame: object) -> None:
        raise OperationDeadline

    def stopped(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    old_alarm = signal.signal(signal.SIGALRM, timed_out)
    old_term = signal.signal(signal.SIGTERM, stopped)
    signal.alarm(seconds)
    try:
        with runtime.deadline(seconds):
            yield
    except OperationDeadline:
        raise TimeoutError("Peak collector deadline exhausted") from None
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_alarm)
        signal.signal(signal.SIGTERM, old_term)


def collect(
    target: Target,
    request: dict,
    host_id: str,
    *,
    duration: int,
    interval: int,
    write: Callable[[dict], None],
    charge_baseline: dict | None = None,
) -> dict:
    samples: list[dict] = []
    phase = "preflight"
    wall_start = 0
    try:
        check_window(duration, interval)
        with operation_limit(IDENTITY_TIMEOUT):
            measurement = bind(target, request, host_id)
        phase = "sampling"
        with operation_limit(duration + SAMPLING_GRACE):
            origin = time.monotonic_ns()
            wall_start = time.time_ns()

            def elapsed() -> int:
                return time.monotonic_ns() - origin

            write(
                {
                    "record": "window",
                    **measurement.provenance,
                    "windowStartUnixNs": wall_start,
                    "requestedDurationSeconds": duration,
                    "intervalSeconds": interval,
                }
            )
            for due in (*range(0, duration, interval), duration):
                delay = due - elapsed() / SECOND
                if delay > 0:
                    time.sleep(delay)
                if elapsed() - due * SECOND > interval * SECOND:
                    raise TimeoutError("Scheduled sample became stale")
                frame = sample(target, request, measurement, elapsed, wall_start, interval)
                write({"record": "sample", "index": len(samples), **frame})
                samples.append(frame)
                if frame["finishedElapsedNs"] - frame["startedElapsedNs"] > interval * SECOND:
                    raise TimeoutError("Sample exceeds the declared collection interval")
        phase = "final-identity"
        with operation_limit(IDENTITY_TIMEOUT):
            final = bind(target, request, host_id)
            if (
                final.owner != measurement.owner
                or final.nodes != measurement.nodes
                or final.stores != measurement.stores
                or final.provenance != measurement.provenance
                or final.kubeconfig != measurement.kubeconfig
            ):
                raise failure.IdentityMismatch(
                    "Target or store changed since the collection baseline"
                )
            tooling_after = tooling_endpoint(request, measurement.tooling_ids)
        baseline = measurement.initial_tooling if charge_baseline is None else charge_baseline
        charge_origin = min((item["elapsedNs"] for item in baseline.values()), default=0)
        charge_limit = (
            2 * IDENTITY_TIMEOUT
            + duration
            + SAMPLING_GRACE
            + (RECEIPT_SETUP_TIMEOUT if charge_baseline is not None else 0)
        )
        charge = {
            key: {
                **summarize_cgroup(
                    [
                        {**metric, "elapsedNs": metric["elapsedNs"] - charge_origin}
                        if metric is not None
                        else None
                        for metric in (before, tooling_after.get(key))
                    ],
                    max_gap_ns=charge_limit * SECOND,
                ),
                "cpuLimitMillicores": before["cpuLimitMillicores"],
                "memoryLimitBytes": before["memoryLimitBytes"],
            }
            for key, before in baseline.items()
        }
        summary = summarize(samples, interval, window_start_ns=wall_start)
        if (
            charge.keys() != tooling_after.keys()
            or not charge
            or any(metric["state"] != "observed" for metric in charge.values())
        ):
            summary["state"] = "incomplete"
            summary["reasons"] = sorted(
                [*summary["reasons"], "collector-tooling-charge-incomplete"]
            )
        return {
            **summary,
            "windowEndElapsedNs": samples[-1]["finishedElapsedNs"],
            "collectorToolingChargeElapsedFrom": "first-tooling-observation",
            "collectorToolingCharge": charge,
        }
    except (Exception, KeyboardInterrupt) as error:
        summary = summarize(samples, interval, window_start_ns=wall_start)
        return {
            **summary,
            "state": "incomplete",
            "failure": failure.evidence(error, phase, "invalid-observation"),
        }


def preparation_account(
    directory: Path, parent: int, request: dict, host_id: str, cold: dict
) -> dict:
    """Charge the capped output to the existing preparation allocation on the same store."""
    candidates = [
        item
        for item in request["disk"]
        if item["category"] == "preparation"
        and item["limitBytes"] > 0
        and directory.is_relative_to(Path(item["path"]))
    ]
    if len(candidates) != 1:
        raise ValueError("Receipt directory requires one containing admitted preparation account")
    account = candidates[0]
    path = host_observation.ordinary_path(account["path"])
    mounts = host_observation.mount_records()
    host_observation.physical_filesystem(path, mounts=mounts)
    host_observation.physical_filesystem(directory, mounts=mounts)
    if any(mount != path and mount.is_relative_to(path) for mount, _ in mounts):
        raise ValueError("A preparation account cannot contain a nested mount")
    directory_id = file_identity(os.fstat(parent))
    account_id = file_identity(path.stat())
    filesystem_id = digest([host_id, account_id[0]])
    if directory_id[0] != account_id[0] or filesystem_id not in {
        item["filesystemId"] for item in cold["admission"]["filesystems"]
    }:
        raise failure.IdentityMismatch("Receipt directory differs from its admitted physical store")
    used = host_observation.allocated_disk_bytes([path])[path]
    available = os.fstatvfs(parent)
    if (
        file_identity(path.stat()) != account_id
        or file_identity(directory.stat(follow_symlinks=False)) != directory_id
    ):
        raise failure.IdentityMismatch("Preparation account or receipt directory changed")
    receipt_bound = MAX_RECEIPT_BYTES + RECEIPT_ALLOCATION_MARGIN
    if (
        used + receipt_bound > account["limitBytes"]
        or available.f_bavail * available.f_frsize < 10 * GIB + receipt_bound
    ):
        raise failure.Failure({"category": "resource-bound-exceeded", "phase": "receipt-setup"})
    return {
        "account": account["id"],
        "filesystemId": filesystem_id,
        "maximumReceiptBytes": MAX_RECEIPT_BYTES,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("collect",))
    parser.add_argument("--consumer", required=True)
    parser.add_argument("--docker-context", required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--https-port", type=int)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--host-id")
    parser.add_argument("--duration-seconds", type=int, default=30)
    parser.add_argument("--interval-seconds", type=int, default=3)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--confirm-target")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    phase = "invocation"
    try:
        with operation_limit(RECEIPT_SETUP_TIMEOUT) if args.execute else nullcontext():
            target = Target(
                args.consumer,
                args.docker_context,
                args.state_root,
                MINIMAL_PROFILE,
                args.https_port,
            )
        if not target.consumer.startswith("qual-"):
            raise ValueError("Disposable target required")
        check_window(args.duration_seconds, args.interval_seconds)
        base = {
            "schema": SCHEMA,
            "target": target.name,
            "profile": target.profile,
            "installedAcceptance": False,
        }
        if not args.execute:
            print(
                json.dumps(
                    {
                        **base,
                        "execution": "not-performed",
                        "durationSeconds": args.duration_seconds,
                        "intervalSeconds": args.interval_seconds,
                    }
                )
            )
            return 0
        if (
            args.confirm_target != target.name
            or not args.request
            or not args.host_id
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", args.host_id)
            or not args.receipt
        ):
            raise ValueError("Exact confirmation, admission request, host and receipt required")
        with ExitStack() as resources:
            phase = "receipt-setup"
            with operation_limit(RECEIPT_SETUP_TIMEOUT):
                receipt = Path(os.path.abspath(args.receipt))
                if (
                    receipt.parent != target.directory
                    or target.directory.resolve(strict=True) != target.directory
                    or receipt.suffix != ".jsonl"
                ):
                    raise ValueError(
                        "New JSONL receipt must be inside the target's private state directory"
                    )
                request = json.loads(state.read_bounded(args.request))
                _, cold, _ = bootstrap_identity(target, request, args.host_id)
                charge_baseline = tooling_endpoint(request, cold["memoryAccountIdentities"])
                parent = os.open(target.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    disk_charge = preparation_account(
                        target.directory, parent, request, args.host_id, cold
                    )
                    descriptor = os.open(
                        receipt.name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=parent,
                    )
                    handle = resources.enter_context(os.fdopen(descriptor, "wb", buffering=0))
                    os.fsync(parent)
                finally:
                    os.close(parent)
                collection_id = str(uuid.uuid4())
                committed = 0

                def write(record: dict) -> None:
                    nonlocal committed
                    payload = (
                        json.dumps(
                            {**base, "collectionId": collection_id, **record},
                            separators=(",", ":"),
                        ).encode("utf-8")
                        + b"\n"
                    )
                    if committed + len(payload) > MAX_RECEIPT_BYTES:
                        raise failure.Failure(
                            {"category": "resource-bound-exceeded", "phase": "receipt-writing"}
                        )
                    if handle.tell() != committed:
                        handle.seek(committed)
                        handle.truncate()
                    remaining = memoryview(payload)
                    while remaining:
                        written = handle.write(remaining)
                        if written is None or written <= 0:
                            raise OSError("Receipt write did not make progress")
                        remaining = remaining[written:]
                    os.fsync(handle.fileno())
                    committed += len(payload)

                write(
                    {
                        "record": "start",
                        "createdAtUnixNs": time.time_ns(),
                        "preparationDiskCharge": disk_charge,
                    }
                )
            # Existing bounded helpers print fixed progress banners only.
            with redirect_stdout(sys.stderr):
                result = collect(
                    target,
                    request,
                    args.host_id,
                    duration=args.duration_seconds,
                    interval=args.interval_seconds,
                    write=write,
                    charge_baseline=charge_baseline,
                )
            phase = "receipt-finalization"
            try:
                with operation_limit(RECEIPT_FINAL_TIMEOUT):
                    write({"record": "result", **result})
            except (Exception, KeyboardInterrupt):
                with suppress(Exception, KeyboardInterrupt), operation_limit(RECEIPT_FINAL_TIMEOUT):
                    if handle.tell() != committed:
                        handle.seek(committed)
                        handle.truncate()
                        os.fsync(handle.fileno())
                raise
        print(
            json.dumps(
                {
                    **base,
                    "collectionId": collection_id,
                    "state": result["state"],
                    "sampleCount": result["sampleCount"],
                    "runtimeAttributionState": result["runtimeAttribution"]["state"],
                    "reasons": result["reasons"],
                    **({"failure": result["failure"]} if "failure" in result else {}),
                }
            )
        )
        return 0 if result["state"] == "observed" else 1
    except (Exception, KeyboardInterrupt) as error:
        print(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "state": "incomplete",
                    "installedAcceptance": False,
                    "failure": failure.evidence(error, phase, "invalid-configuration"),
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
