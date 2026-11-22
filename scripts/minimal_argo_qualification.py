"""Compare a disposable controller cycle with an identity-bound native peak window."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import UUID

from . import resource_peaks
from .consumer_profile import Target, load_profile
from .network_resources import quantity


def peak_records(
    target: Target, path: Path, provenance: dict
) -> tuple[dict, list[dict], dict, str]:
    """Read the collector's bounded record stream with its exact byte identity."""
    checksum = hashlib.sha256()
    records = []
    size = 0
    with path.open("rb") as handle:
        for line in handle:
            size += len(line)
            if size > resource_peaks.MAX_RECEIPT_BYTES or not line.endswith(b"\n"):
                raise ValueError("Argo peak receipt is incomplete or exceeds its byte bound")
            if len(records) == 3 + resource_peaks.MAX_SAMPLES:
                raise ValueError("Argo peak receipt exceeds its record bound")
            checksum.update(line)
            records.append(json.loads(line))
    if len(records) < 5:
        raise ValueError("Argo peak receipt has no complete native sampling window")
    start, window, *samples, result = records
    collection = start["collectionId"]
    common = {
        "schema": resource_peaks.SCHEMA,
        "collectionId": collection,
        "target": target.name,
        "profile": target.profile,
        "installedAcceptance": False,
    }
    if (
        str(UUID(collection)) != collection
        or any(any(record.get(key) != value for key, value in common.items()) for record in records)
        or start["record"] != "start"
        or window["record"] != "window"
        or result["record"] != "result"
        or any(window.get(key) != value for key, value in provenance.items())
    ):
        raise ValueError("Argo peak receipt provenance differs from the current disposable target")
    if (
        result.get("sampleCount") != len(samples)
        or result.get("windowEndElapsedNs") != samples[-1]["finishedElapsedNs"]
        or any(
            item["record"] != "sample" or item["index"] != index
            for index, item in enumerate(samples)
        )
        or any(
            not left["finishedElapsedNs"] <= right["startedElapsedNs"]
            for left, right in zip(samples, samples[1:], strict=False)
        )
    ):
        raise ValueError("Argo peak receipt sampling sequence is incomplete")
    return window, samples, result, "sha256:" + checksum.hexdigest()


def assess(
    target: Target, history: dict, workloads: dict, window: dict, samples: list[dict], peak: dict
) -> dict:
    start = window["windowStartUnixNs"]
    stopped = history["suspendCompletedAtUnixNs"]
    restarting = history["resumeStartedAtUnixNs"]
    finished = history["resumeCompletedAtUnixNs"]
    if not (
        start + samples[0]["finishedElapsedNs"]
        <= history["suspendStartedAtUnixNs"]
        <= stopped
        <= restarting
        <= finished
        <= start + samples[-1]["startedElapsedNs"]
    ):
        raise ValueError("Peak window must bracket the complete observed suspend and resume cycle")
    reasons: list[str] = []
    nodes = peak["nodes"]
    limits = {
        f"{target.name}-{role}": values["memoryBytes"]
        for role, values in load_profile(name=target.profile)["nodes"].items()
    }
    if set(nodes) != set(limits) or any(
        metric.get("state") != "observed"
        or metric.get("cgroupIdentity") != window["nodes"][name]["cgroupIdentity"]
        or type(metric.get("lifetimeMemoryPeakBytes")) is not int
        or not 0 < metric["lifetimeMemoryPeakBytes"] <= limits[name]
        for name, metric in nodes.items()
    ):
        reasons.append("native-node-coverage-incomplete")
    charge = peak["collectorToolingCharge"]
    tooling = peak["tooling"]
    if (
        not charge
        or charge.keys() != tooling.keys()
        or any(
            metric.get("state") != "observed"
            or tooling[name].get("state") != "observed"
            or not 0 < metric.get("lifetimeMemoryPeakBytes", 0) <= metric["memoryLimitBytes"]
            for name, metric in charge.items()
        )
    ):
        reasons.append("native-tooling-coverage-incomplete")
    pressure = peak["sampledPressure"]
    if (
        pressure.get("memoryFloorBreached") is not False
        or pressure.get("diskFloorBreachedFilesystemIds") != []
    ):
        reasons.append("host-or-store-pressure")
    pod_reasons, covered = controller_coverage(
        target, history, workloads, start, samples, peak["containers"]
    )
    reasons.extend(pod_reasons)
    return {
        "reasons": sorted(set(reasons)),
        "nativeNodes": nodes,
        "toolingCharge": charge,
        "pressure": pressure,
        "runtimeAttribution": peak["runtimeAttribution"],
        "collectorCoverage": peak["state"],
        "coveredRestoredContainers": covered,
    }


def controller_coverage(
    target: Target, history: dict, workloads: dict, start: int, samples: list[dict], attempts: dict
) -> tuple[list[str], int]:
    reasons = []
    covered = 0
    old = history["previousPods"]
    new = history["restoredPods"]
    if (
        old.keys() != workloads.keys()
        or new.keys() != workloads.keys()
        or any(
            len(old[name]) != 1 or len(new[name]) != 1 or old[name] == new[name]
            for name in workloads
        )
    ):
        raise ValueError(
            "Qualification requires all nine previously running and newly restored Argo Pods"
        )
    before = live_rows(
        samples, "finishedElapsedNs", history["suspendStartedAtUnixNs"] - start, opening=True
    )
    after = live_rows(
        samples, "startedElapsedNs", history["resumeCompletedAtUnixNs"] - start, opening=False
    )
    worker = f"{target.name}-worker"
    for name, workload in workloads.items():
        if old[name][0] not in before or new[name][0] not in after:
            reasons.append("controller-window-sample-missing")
        template = workload["spec"]["template"]["spec"]
        namespace = workload["metadata"]["namespace"]
        for kind, field in (("app", "containers"), ("init", "initContainers")):
            for container in template.get(field, []):
                rows = [
                    row
                    for row in attempts.values()
                    if row["podUid"] == new[name][0]
                    and row["kind"] == kind
                    and row["containerName"] == container["name"]
                ]
                if len(rows) != 1:
                    reasons.append(f"restored-{kind}-coverage-incomplete")
                    continue
                row = rows[0]
                cap = quantity(container["resources"]["limits"]["memory"], cpu=False)
                if (
                    row["namespace"] != namespace
                    or row["node"] != worker
                    or row.get("state") != "observed"
                    or type(row.get("lifetimeMemoryPeakBytes")) is not int
                    or not 0 < row["lifetimeMemoryPeakBytes"] <= cap
                    or row.get("startedAtNs") is None
                    or not history["resumeStartedAtUnixNs"] - resource_peaks.SECOND
                    <= row["startedAtNs"]
                    <= history["resumeCompletedAtUnixNs"] + resource_peaks.SECOND
                ):
                    reasons.append(f"restored-{kind}-coverage-incomplete")
                    continue
                covered += 1
    return reasons, covered


def live_rows(samples: list[dict], offset: str, boundary: int, *, opening: bool) -> set[str]:
    return {
        row["podUid"]
        for frame in samples
        if (frame[offset] <= boundary if opening else frame[offset] >= boundary)
        for row in frame["containers"]
        if row["kind"] == "app"
        and row["state"] == "running"
        and row["counterState"] == "observed"
        and row["restartCount"] == 0
    }
