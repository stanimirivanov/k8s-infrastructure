"""Bounded, read-only Linux physical-host observation; no Docker/VM assumptions."""

from __future__ import annotations

import os
import platform
import signal
import subprocess
import time
from pathlib import Path

from . import host_admission as model


def host_id() -> str:
    """Boot identity changes on restart, invalidating all earlier host samples."""
    if platform.system() != "Linux":
        raise model.Refusal("Only native Linux hosts supported", code="unsupported-host")
    return model.digest(Path("/proc/sys/kernel/random/boot_id").read_text().strip())


CGROUP_ROOT = Path("/sys/fs/cgroup")


def collector_cgroup() -> Path:
    for line in Path("/proc/self/cgroup").read_text().splitlines():
        if line.startswith("0::/"):
            return CGROUP_ROOT / line[4:]
    raise model.Refusal("Unified cgroup v2 required", code="missing-prerequisite")


def mem_available() -> int:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return model.integer(int(line.split()[1])) * 1024
    raise model.Refusal("MemAvailable unavailable", code="host-evidence-unavailable")


def mount_records() -> list[tuple[Path, str]]:
    records = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        before, after = line.split(" - ", 1)
        raw = before.split()[4]
        for encoded, decoded in ((r"\040", " "), (r"\011", "\t"), (r"\012", "\n"), (r"\134", "\\")):
            raw = raw.replace(encoded, decoded)
        records.append((Path(raw), after.split()[0]))
    return records


def mount_points() -> list[Path]:
    return [path for path, _ in mount_records()]


def physical_filesystem(path: Path, *, mounts: list[tuple[Path, str]] | None = None) -> None:
    records = mount_records() if mounts is None else mounts
    candidates = [(mount, kind) for mount, kind in records if path.is_relative_to(mount)]
    if not candidates or max(candidates, key=lambda item: len(item[0].parts))[1] not in {
        "ext4",
    }:
        raise model.Refusal(
            "Only native local ext4 physical stores are supported", code="unsupported-storage"
        )


def ordinary_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.resolve(strict=True) != path or not path.is_dir():
        raise model.Refusal("Ordinary absolute directory required", code="invalid-request")
    return path


def memory_observation(request: dict) -> dict:
    result = {}
    paths: list[Path] = []
    collector = collector_cgroup().resolve(strict=True)
    collector_accounted = False
    for item in request["memory"]:
        if "cgroup" not in item:
            if item["kind"] == "tooling":
                raise model.Refusal("Tooling requires an enforced cgroup", code="invalid-request")
            result[item["id"]] = {"residentBytes": 0}
            continue
        path = ordinary_path(item["cgroup"])
        if not path.is_relative_to(CGROUP_ROOT) or any(
            path == previous or path.is_relative_to(previous) or previous.is_relative_to(path)
            for previous in paths
        ):
            raise model.Refusal(
                "Overlapping or unsupported cgroup accounts", code="invalid-request"
            )
        paths.append(path)
        bound = model.integer(int((path / "memory.max").read_text()))
        current = model.integer(int((path / "memory.current").read_text()))
        if bound > item["limitBytes"] or current > item["limitBytes"]:
            raise model.Refusal("Cgroup memory exceeds requested bound", code="invalid-request")
        if item["kind"] == "tooling":
            quota, period = (path / "cpu.max").read_text().split()
            if (
                int(period) <= 0
                or int(quota) <= 0
                or int(quota) * 1000 > item["cpuMillicores"] * int(period)
            ):
                raise model.Refusal("Tooling CPU exceeds requested bound", code="invalid-request")
            collector_accounted |= collector == path or collector.is_relative_to(path)
        stats = dict(line.split() for line in (path / "memory.stat").read_text().splitlines())
        # File cache can also be included in MemAvailable. Credit only anon,
        # conservatively excluding reclaimable cache and all unknown metrics.
        if "anon" not in stats:
            raise model.Refusal("Missing anonymous memory metric", code="invalid-observation")
        resident = model.integer(int(stats["anon"]))
        if resident > current:
            raise model.Refusal(
                "Inconsistent cgroup observation; retry", code="invalid-observation"
            )
        result[item["id"]] = {
            "residentBytes": resident,
            "cgroupId": model.digest([path.stat().st_dev, path.stat().st_ino]),
        }
    if not collector_accounted:
        raise model.Refusal(
            "Collector is not inside the declared bounded tooling cgroup", code="invalid-request"
        )
    return result


def allocated_disk_bytes(paths: list[Path]) -> dict[Path, int]:
    """Measure native allocated bytes with the existing five-second disk-scan limit."""
    # One GNU du invocation deduplicates hardlinks across roots. Separate scans
    # would credit the same allocated blocks twice and could over-admit growth.
    measured = subprocess.run(
        [
            "du",
            "--summarize",
            "--block-size=1",
            "--one-file-system",
            "--null",
            "--",
            *(str(path) for path in paths),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=5,
        check=True,
        text=True,
    )
    resident_by_path = {}
    for record in measured.stdout.split("\0"):
        if record:
            size, name = record.split("\t", 1)
            resident_by_path[Path(name)] = model.integer(int(size))
    if set(resident_by_path) != set(paths):
        raise model.Refusal(
            "Incomplete or overlapping physical disk observations", code="invalid-observation"
        )
    return resident_by_path


def collect(request: dict, expected_host_id: str) -> dict:
    model.validate(request)
    start = int(time.time())
    if host_id() != expected_host_id:
        raise model.Refusal("Host identity changed", code="identity-mismatch")
    memory = memory_observation(request)
    mounts = mount_points()
    paths: list[Path] = []
    disks = {}
    roots: dict[tuple[int, int], Path] = {}
    accounts = {}
    stores = {}
    for item in request["disk"]:
        path = ordinary_path(item["path"])
        physical_filesystem(path)
        if any(
            path != previous and (path.is_relative_to(previous) or previous.is_relative_to(path))
            for previous in paths
        ):
            raise model.Refusal("Overlapping disk accounts", code="invalid-request")
        paths.append(path)
        if any(mount != path and mount.is_relative_to(path) for mount in mounts):
            raise model.Refusal(
                "Nested mount requires a separate physical account", code="invalid-request"
            )
        before = path.stat()
        key = (before.st_dev, before.st_ino)
        roots.setdefault(key, path)
        accounts[item["id"]] = (path, key)
    resident_by_path = allocated_disk_bytes(list(roots.values()))
    for name, (path, key) in accounts.items():
        after = path.stat()
        if (after.st_dev, after.st_ino) != key:
            raise model.Refusal(
                "Physical account changed during observation", code="identity-mismatch"
            )
        statistics = os.statvfs(path)
        available = stores.setdefault(key[0], statistics.f_bavail * statistics.f_frsize)
        disks[name] = {
            "physicalId": model.digest([expected_host_id, *key]),
            "filesystemId": model.digest([expected_host_id, key[0]]),
            "availableBytes": available,
            "residentBytes": resident_by_path[roots[key]],
        }
    if host_id() != expected_host_id:
        raise model.Refusal("Host identity changed during observation", code="identity-mismatch")
    return {
        "version": 1,
        "requestDigest": model.digest(request),
        "hostId": expected_host_id,
        "target": request["target"],
        "observedAt": start,
        "memAvailableBytes": mem_available(),
        "memory": memory,
        "disk": disks,
    }


def observe(request: dict, *, expected_host_id: str) -> dict:
    """Maximum 30s total, 5s for the disk scan; no cleanup or provider access."""

    def expired(signum: int, frame: object) -> None:
        raise TimeoutError("Host observation exceeded 30s")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.alarm(30)
    try:
        return collect(request, expected_host_id)
    except model.Refusal:
        raise
    except ValueError:
        # Linux numeric metrics and GNU du records may be unavailable/malformed.
        raise model.Refusal("Malformed host metric", code="invalid-observation") from None
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
