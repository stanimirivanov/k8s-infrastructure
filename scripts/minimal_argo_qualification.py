"""Compare a disposable controller cycle with an identity-bound native peak window."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import UUID

from . import resource_peaks
from .consumer_profile import Target, load_profile
from .network_resources import quantity
from .peak_observation import ATTEMPT_LIMIT


def peak_records(
    target: Target, path: Path, provenance: dict
) -> tuple[dict, list[dict], dict, str]:
    """Read the collector's bounded record stream with its exact byte identity."""
    checksum = hashlib.sha256()
    records = []
    size = 0
    with path.open("rb") as handle:
        while line := handle.readline(resource_peaks.MAX_RECEIPT_BYTES - size + 1):
            size += len(line)
            if size > resource_peaks.MAX_RECEIPT_BYTES or not line.endswith(b"\n"):
                raise ValueError("Argo peak receipt is incomplete or exceeds its byte bound")
            if len(records) == 3 + resource_peaks.MAX_SAMPLES:
                raise ValueError("Argo peak receipt exceeds its record bound")
            checksum.update(line)
            records.append(json.loads(line))
    if len(records) < 5 or any(not isinstance(item, dict) for item in records):
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
    check_sampling(start, window, samples, result)
    return window, samples, result, "sha256:" + checksum.hexdigest()


def check_sampling(start: dict, window: dict, samples: list[dict], result: dict) -> None:
    duration, interval = window["requestedDurationSeconds"], window["intervalSeconds"]
    if type(duration) is not int or type(interval) is not int:
        raise ValueError("Invalid Argo peak receipt cadence")
    resource_peaks.check_window(duration, interval)
    start_ns = window["windowStartUnixNs"]
    step = interval * resource_peaks.SECOND
    if (
        "failure" in result
        or type(start_ns) is not int
        or type(start.get("createdAtUnixNs")) is not int
        or not 0 < start["createdAtUnixNs"] <= start_ns
        or len(samples) != 1 + duration // interval
        or result.get("sampleCount") != len(samples)
        or result.get("windowEndElapsedNs") != samples[-1]["finishedElapsedNs"]
    ):
        raise ValueError("Argo peak receipt sampling sequence is incomplete")
    stores = {value["filesystemId"] for value in window["stores"].values()}
    tooling = samples[0]["tooling"].keys()
    previous_end = previous_start = 0
    for index, frame in enumerate(samples):
        begin, end = frame["startedElapsedNs"], frame["finishedElapsedNs"]
        if (
            frame["record"] != "sample"
            or type(frame["index"]) is not int
            or frame["index"] != index
            or type(begin) is not int
            or type(end) is not int
            or not index * step <= begin <= (index + 1) * step
            or not begin <= end <= begin + step
            or previous_end > begin
            or index
            and not 0 < begin - previous_start <= 2 * step
        ):
            raise ValueError("Argo peak receipt sampling sequence is stale or incomplete")
        if (
            frame["nodes"].keys() != window["nodes"].keys()
            or frame["filesystems"].keys() != stores
            or frame["tooling"].keys() != tooling
            or not stores
            or not tooling
            or len(frame["containers"]) > ATTEMPT_LIMIT
            or not begin <= frame["diskElapsedNs"] <= end
            or frame["initOverlap"]
            != resource_peaks.init_overlap(frame["containers"], frame["nodes"])
        ):
            raise ValueError("Argo peak receipt raw sample scope or attribution differs")
        check_sample_counters(frame, begin, end)
        previous_start, previous_end = begin, end
    expected = resource_peaks.summarize(samples, interval, window_start_ns=start_ns)
    charge = result["collectorToolingCharge"]
    if charge.keys() != tooling or any(item["state"] != "observed" for item in charge.values()):
        expected["state"] = "incomplete"
        expected["reasons"] = sorted([*expected["reasons"], "collector-tooling-charge-incomplete"])
    if any(result.get(key) != value for key, value in expected.items()):
        raise ValueError("Argo peak receipt native summary differs from its raw samples")


def check_sample_counters(frame: dict, begin: int, end: int) -> None:
    values = [
        *frame["nodes"].values(),
        *frame["tooling"].values(),
        *(item["counters"] for item in frame["containers"]),
        *(item["counters"] for item in frame.get("unattributedRuntimeScopes", [])),
    ]
    if (
        not valid_counter(frame["host"]["memAvailableBytes"])
        or not begin <= frame["host"]["elapsedNs"] <= end
        or any(
            not valid_counter(row[key])
            for row in frame["filesystems"].values()
            for key in ("availableBytes", "usedBytes")
        )
        or any(
            item is not None
            and (
                not begin <= item["elapsedNs"] <= end
                or not valid_counter(item["memoryCurrentBytes"])
                or not valid_counter(item["lifetimeMemoryPeakBytes"])
                or item["memoryCurrentBytes"] > item["lifetimeMemoryPeakBytes"]
                or set(item["cpu"]) != set(resource_peaks.CPU_FIELDS.values())
                or not all(valid_counter(value) for value in item["cpu"].values())
            )
            for item in values
        )
    ):
        raise ValueError("Argo peak receipt has invalid native measurement counters")


def valid_counter(value: object) -> bool:
    return type(value) is int and 0 <= value < 2**64


def assess(
    target: Target,
    history: dict,
    workloads: dict,
    window: dict,
    samples: list[dict],
    peak: dict,
    live_tooling: dict,
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
        or charge.keys() != live_tooling.keys()
        or peak.get("collectorToolingChargeElapsedFrom") != "first-tooling-observation"
        or any(
            not complete_tooling_charge(
                metric, tooling[name], live_tooling[name], name, samples, window
            )
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
    initializers, init_reasons = initializer_peaks(history, peak["containers"])
    reasons.extend(init_reasons)
    initializer_coverage = (
        "not-required"
        if not initializers
        else "observed"
        if all(item["state"] == "observed" for item in initializers)
        else "unavailable"
        if all(item["state"] == "unavailable" for item in initializers)
        else "partial"
    )
    return {
        "reasons": sorted(set(reasons)),
        "nativeNodes": nodes,
        "toolingCharge": charge,
        "pressure": pressure,
        "runtimeAttribution": peak["runtimeAttribution"],
        "collectorCoverage": peak["state"],
        "coveredRestoredContainers": covered,
        "initializerNativePeakCoverage": initializer_coverage,
        "initializerNativePeaks": initializers,
        "sampledInitOverlap": peak["initOverlap"],
    }


def complete_tooling_charge(
    charge: dict, interval: dict, live: dict, name: str, samples: list[dict], window: dict
) -> bool:
    maximum_duration = (
        2 * resource_peaks.IDENTITY_TIMEOUT
        + window["requestedDurationSeconds"]
        + resource_peaks.SAMPLING_GRACE
        + resource_peaks.RECEIPT_SETUP_TIMEOUT
    ) * resource_peaks.SECOND
    elapsed = charge.get("cpuIntervalElapsedNs")
    delta = charge.get("intervalCpuDelta")
    limits = ("memoryLimitBytes", "cpuLimitMillicores")
    return (
        charge.get("state") == interval.get("state") == "observed"
        and charge.get("reasons") == []
        and charge.get("cgroupIdentity") == interval.get("cgroupIdentity") == live["cgroupIdentity"]
        and all(
            all(sample["tooling"][name].get(key) == charge.get(key) == live[key] for key in limits)
            for sample in samples
        )
        and isinstance(elapsed, list)
        and len(elapsed) == 2
        and all(valid_counter(value) for value in elapsed)
        and 0 <= elapsed[0] < elapsed[1] <= maximum_duration
        and elapsed[1] - elapsed[0] >= samples[-1]["finishedElapsedNs"]
        and isinstance(delta, dict)
        and delta.keys() == interval["intervalCpuDelta"].keys()
        and all(
            valid_counter(value) and value >= interval["intervalCpuDelta"][key]
            for key, value in delta.items()
        )
        and 0
        < interval["lifetimeMemoryPeakBytes"]
        <= charge["lifetimeMemoryPeakBytes"]
        <= min(live["lifetimeMemoryPeakBytes"], charge["memoryLimitBytes"])
        and 0 <= charge["sampledMemoryMaxBytes"] <= charge["lifetimeMemoryPeakBytes"]
    )


def initializer_peaks(history: dict, attempts: dict) -> tuple[list[dict], list[str]]:
    peaks = []
    reasons = []
    for installed in history["restoredInitializers"]:
        record = {key: installed[key] for key in ("controller", "containerName", "podUid")}
        if not (
            history["resumeStartedAtUnixNs"] - resource_peaks.SECOND
            <= installed["startedAtNs"]
            <= installed["finishedAtNs"]
            <= history["resumeCompletedAtUnixNs"] + resource_peaks.SECOND
        ):
            reasons.append("restored-init-lifecycle-outside-startup")
        row = attempts.get(installed["containerId"])
        if row is None:
            peaks.append(
                {**record, "state": "unavailable", "reason": "native-attempt-not-observed"}
            )
            continue
        if (
            row.get("kind") != "init"
            or any(
                row.get(key) != installed[key]
                for key in ("node", "namespace", "podUid", "containerName", "startedAtNs")
            )
            or row.get("finishedAtNs") not in {None, installed["finishedAtNs"]}
        ):
            reasons.append("restored-init-native-identity-mismatch")
            peaks.append(
                {**record, "state": "unavailable", "reason": "native-attempt-identity-mismatch"}
            )
            continue
        cap = quantity(installed["resources"]["limits"]["memory"], cpu=False)
        native = row.get("lifetimeMemoryPeakBytes") or row.get(
            "lastObservedLifetimeMemoryPeakBytes"
        )
        if native is not None and native > cap:
            reasons.append("restored-init-native-limit-exceeded")
        if (
            row.get("state") == "observed"
            and row.get("finishedAtNs") == installed["finishedAtNs"]
            and native
        ):
            peaks.append(
                {
                    **record,
                    "state": "observed",
                    "lifetimeMemoryPeakBytes": native,
                    "intervalCpuDelta": row["intervalCpuDelta"],
                }
            )
        else:
            peaks.append(
                {**record, "state": "unavailable", "reason": "complete-native-attempt-unavailable"}
            )
    return peaks, reasons


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
        for container in template["containers"]:
            rows = [
                row
                for row in attempts.values()
                if row["podUid"] == new[name][0]
                and row["kind"] == "app"
                and row["containerName"] == container["name"]
            ]
            if len(rows) != 1:
                reasons.append("restored-app-coverage-incomplete")
                continue
            row = rows[0]
            cap = quantity(container["resources"]["limits"]["memory"], cpu=False)
            if (
                row["namespace"] != namespace
                or row["node"] != worker
                or row.get("state") != "observed"
                or row["finishedAtNs"] is not None
                or type(row.get("lifetimeMemoryPeakBytes")) is not int
                or not 0 < row["lifetimeMemoryPeakBytes"] <= cap
                or row.get("startedAtNs") is None
                or not history["resumeStartedAtUnixNs"] - resource_peaks.SECOND
                <= row["startedAtNs"]
                <= history["resumeCompletedAtUnixNs"] + resource_peaks.SECOND
            ):
                reasons.append("restored-app-coverage-incomplete")
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
