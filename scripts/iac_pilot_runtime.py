"""Bounded OpenTofu, kind and Kubernetes calls for the #74 laptop pilot."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from . import argocd, local_kind
from .iac_pilot_state import (
    DOCKER_ENDPOINT,
    PACKAGE_DIGEST,
    Target,
    check_path,
    digest,
    encoded,
    fingerprint,
    mapping,
    private_directory,
    read_file,
    store,
    uid,
)

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "clusters/local-kind/iac-pilot"
PROVIDER = "registry.terraform.io/elioseverojunior/kind"
PACKAGE = "terraform-provider-kind_0.0.4_linux_amd64.zip"
ADDRESS = "kind_cluster.pilot"
CLUSTER_LABEL = "io.x-k8s.kind.cluster"


def validate_plan(data: dict[str, Any], action: str, name: str) -> list[dict[str, Any]]:
    version = data.get("format_version")
    if (
        not isinstance(version, str)
        or version.split(".")[0] != "1"
        or data.get("errored") is not False
        or data.get("complete", True) is not True
        or data.get("deferred_changes")
        or data.get("action_invocations")
        or data.get("output_changes")
        or data.get("variables", {}).get("cluster_name", {}).get("value") != name
    ):
        raise ValueError("Unexpected or incomplete #74 saved plan")
    expected = {"create": ["create"], "destroy": ["delete"], "repeat": ["no-op"]}[action]
    items = data.get("resource_changes", [])
    if not isinstance(items, list) or len(items) != 1:
        raise ValueError("The saved plan must contain only the registered pilot resource")
    for item in items:
        change = item.get("change", {})
        value = change.get("before" if action == "destroy" else "after")
        if (
            item.get("address") != ADDRESS
            or item.get("mode") != "managed"
            or item.get("provider_name") != PROVIDER
            or item.get("previous_address")
            or item.get("deposed")
            or change.get("actions") != expected
            or not isinstance(value, dict)
            or value.get("name") != name
            or change.get("importing")
        ):
            raise ValueError("The saved plan proposes an unapproved action or target")
    drift = data.get("resource_drift", [])
    if not isinstance(drift, list):
        raise ValueError("Invalid drift summary in the saved plan")
    return [{"address": entry["address"], "actions": entry["change"]["actions"]} for entry in drift]


class Runtime:
    def __init__(self, target: Target, lock: int):
        self.target = target
        self.lock = lock
        self.work = target.directory / "opentofu"
        self.tofu_home = target.directory / "provider-home"
        self.cli_config = target.directory / "opentofu.tfrc"

    def run(
        self,
        command: list[str],
        *,
        write: bool = False,
        data: bytes | None = None,
        tofu: bool = False,
        timeout: int = 120,
    ) -> str:
        environment = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(self.tofu_home if tofu else Path.home()),
            "LANG": "C.UTF-8",
            "DOCKER_HOST": DOCKER_ENDPOINT,
            # Prevent provider/CLI side effects against the operator's kubeconfig.
            "KUBECONFIG": str(self.target.directory / "provider.kubeconfig"),
        }
        if tofu:
            environment.update(
                {
                    "TF_CLI_CONFIG_FILE": str(self.cli_config),
                    "TF_IN_AUTOMATION": "1",
                    "TF_INPUT": "0",
                    "CHECKPOINT_DISABLE": "1",
                }
            )
        return local_kind.run(
            command,
            input_data=data,
            isolated_environment=environment,
            hold_lock=self.lock if write else None,
            timeout=timeout,
        )

    def docker_info(self) -> dict[str, Any]:
        import platform

        if platform.system() != "Linux" or platform.machine() != "x86_64":
            raise ValueError("The #74 rehearsal requires Linux AMD64")
        endpoint = json.loads(
            self.run(
                [
                    "docker",
                    "context",
                    "inspect",
                    "default",
                    "--format",
                    "{{json .Endpoints.docker.Host}}",
                ]
            )
        )
        info = mapping(self.run(["docker", "info", "--format", "{{json .}}"]))
        if endpoint != DOCKER_ENDPOINT or not isinstance(info.get("ID"), str) or not info["ID"]:
            raise ValueError("Expected the laptop's default local Docker daemon")
        return info

    def preflight(self, info: dict[str, Any]) -> None:
        if str(info["CgroupVersion"]) != "2":
            raise ValueError("The #74 rehearsal requires cgroup v2")
        storage = os.statvfs(info["DockerRootDir"])
        instances = Path("/proc/sys/fs/inotify/max_user_instances").read_text().strip()
        watches = Path("/proc/sys/fs/inotify/max_user_watches").read_text().strip()
        available = next(
            line.split()[1]
            for line in Path("/proc/meminfo").read_text().splitlines()
            if line.startswith("MemAvailable:")
        )
        print(
            f"Laptop Docker: {info['NCPU']} CPUs, {int(available) // 1024} MiB RAM available, "
            f"{storage.f_bavail * storage.f_frsize / 1024**3:.1f} GiB Docker disk free, "
            f"{storage.f_favail} free inodes. Inotify per UID: {instances} instances, {watches} watches."
        )
        self.report_inotify_usage()

    @staticmethod
    def report_inotify_usage() -> None:
        for owner in sorted({0, os.geteuid()}):
            visible = hidden = 0
            for process in Path("/proc").glob("[0-9]*"):
                try:
                    if process.stat().st_uid != owner:
                        continue
                    for fd in (process / "fd").iterdir():
                        try:
                            visible += str(fd.readlink()) == "anon_inode:inotify"
                        except FileNotFoundError:
                            continue
                except PermissionError:
                    hidden += 1
                except (FileNotFoundError, ProcessLookupError):
                    continue
            print(f"UID {owner}: {visible} visible inotify FDs, {hidden} processes inaccessible.")

    def nodes(self) -> dict[str, str]:
        output = self.run(
            [
                "docker",
                "ps",
                "--all",
                "--no-trunc",
                "--format",
                '{{.ID}} {{.Names}} {{.Label "' + CLUSTER_LABEL + '"}}',
            ]
        )
        nodes = {}
        for line in output.splitlines():
            fields = line.split()
            if len(fields) not in {2, 3}:
                raise ValueError("Unexpected Docker inventory response")
            identifier, name = fields[:2]
            label = fields[2] if len(fields) == 3 else None
            has_name = name.startswith(f"{self.target.name}-")
            if has_name or label == self.target.name:
                if (
                    not has_name
                    or label != self.target.name
                    or not re.fullmatch(r"[a-f0-9]{64}", identifier)
                ):
                    raise ValueError("Unowned or unexpected Docker node for the #74 target")
                if name in nodes or identifier in nodes.values():
                    raise ValueError("Ambiguous Docker node identity for the #74 target")
                nodes[name] = identifier
        return nodes

    def kube_prefix(self, config: Path | str | None = None) -> list[str]:
        return [
            "kubectl",
            "--kubeconfig",
            str(config or self.target.kubeconfig),
            "--context",
            self.target.context,
            "--request-timeout=30s",
        ]

    def kind_config(self) -> bytes:
        output = self.run(["kind", "get", "kubeconfig", "--name", self.target.name]).encode()
        value = yaml.safe_load(output)
        if not isinstance(value, dict) or value.get("current-context") != self.target.context:
            raise ValueError("kind returned an unexpected Kubernetes context")
        contexts = value.get("contexts", [])
        clusters = value.get("clusters", [])
        users = value.get("users", [])
        if not all(
            isinstance(items, list) and len(items) == 1 for items in (contexts, clusters, users)
        ):
            raise ValueError("Expected exactly one dedicated kubeconfig context, cluster and user")
        context = contexts[0]
        cluster = clusters[0]
        user = users[0]
        access = user.get("user", {})
        connection = cluster.get("cluster", {})
        server = urlsplit(connection.get("server", ""))
        if (
            context.get("name") != self.target.context
            or context.get("context", {}).get("cluster") != self.target.context
            or context.get("context", {}).get("user") != self.target.context
            or cluster.get("name") != self.target.context
            or user.get("name") != self.target.context
            or server.scheme != "https"
            or server.hostname != "127.0.0.1"
            or not server.port
            or server.username
            or server.password
            or server.path
            or server.query
            or server.fragment
            or set(connection) != {"server", "certificate-authority-data"}
            or not connection["certificate-authority-data"]
            or set(access) != {"client-certificate-data", "client-key-data"}
            or not all(isinstance(value, str) and value for value in access.values())
        ):
            raise ValueError("kind kubeconfig does not select the approved loopback TLS target")
        return output

    def live_uid(self, *, unpublished: bool = False) -> str:
        fresh = self.kind_config()
        if unpublished:
            config = "/dev/stdin"
        else:
            check_path(
                self.target.kubeconfig, directory=False, owner=os.geteuid(), prohibited=0o077
            )
            if yaml.safe_load(read_file(self.target.kubeconfig, 131072)) != yaml.safe_load(fresh):
                raise ValueError(
                    "Dedicated kubeconfig has drifted from the registered kind cluster"
                )
            config = str(self.target.kubeconfig)
        return uid(
            self.run(
                [
                    *self.kube_prefix(config),
                    "get",
                    "namespace",
                    "kube-system",
                    "-o",
                    "jsonpath={.metadata.uid}",
                ],
                data=fresh if unpublished else None,
            ).strip()
        )

    def publish_config(self) -> str:
        candidate = self.kind_config()
        cluster_uid = self.live_uid(unpublished=True)
        # Only publish the bytes verified against the same live cluster.
        if self.kind_config() != candidate:
            raise ValueError("kind kubeconfig changed during identity verification")
        if os.path.lexists(self.target.kubeconfig):
            check_path(
                self.target.kubeconfig, directory=False, owner=os.geteuid(), prohibited=0o077
            )
            if yaml.safe_load(read_file(self.target.kubeconfig, 131072)) != yaml.safe_load(
                candidate
            ):
                raise ValueError("Refusing to overwrite a different dedicated kubeconfig")
        else:
            store(self.target.kubeconfig, candidate)
        if self.live_uid() != cluster_uid:
            raise ValueError("Cluster UID changed while publishing the dedicated kubeconfig")
        return cluster_uid

    def verify_ready(self, nodes: dict[str, str]) -> dict[str, str]:
        data = mapping(self.run([*self.kube_prefix(), "get", "nodes", "-o", "json"]))
        local_kind.check_nodes(data)
        result = {item["metadata"]["name"]: uid(item["metadata"]["uid"]) for item in data["items"]}
        if result.keys() != nodes.keys() or len(set(result.values())) != 3:
            raise ValueError("Kubernetes nodes differ from the three distinct Docker nodes")
        for item in data["items"]:
            labels = item["metadata"]["labels"]
            expected = {"performance-generator": "generator", "sut": "sut"}.get(labels["workload"])
            if expected is not None and labels.get("perfeng.io/node-type") != expected:
                raise ValueError("Missing reviewed worker placement label")
        self.run(
            [
                *self.kube_prefix(),
                "-n",
                "kube-system",
                "rollout",
                "status",
                "deployment/coredns",
                "--timeout=60s",
            ]
        )
        storage = mapping(
            self.run(
                [
                    *self.kube_prefix(),
                    "get",
                    "storageclass",
                    "standard",
                    "-o",
                    "json",
                ]
            )
        )
        if storage.get("provisioner") != "rancher.io/local-path":
            raise ValueError("Expected the standard kind local-path storage capability")
        self.run(
            [
                *self.kube_prefix(),
                "-n",
                "local-path-storage",
                "rollout",
                "status",
                "deployment/local-path-provisioner",
                "--timeout=60s",
            ]
        )
        return result

    def snapshot(
        self, *, cluster_uid: bool = False, tolerate_unreachable: bool = False
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"state": fingerprint(self.target.state), "nodes": self.nodes()}
        if cluster_uid and result["nodes"]:
            try:
                result["uid"] = self.live_uid(unpublished=tolerate_unreachable)
            except (OSError, ValueError, TypeError, yaml.YAMLError):
                if not tolerate_unreachable:
                    raise
                result["uid"] = "unreachable"
        else:
            result["uid"] = None
        return result

    def exists_in_state(self) -> bool:
        path = self.target.state
        if not os.path.lexists(path):
            return False
        check_path(path, directory=False, owner=os.geteuid(), prohibited=0o077)
        resources = mapping(read_file(path)).get("resources")
        if resources == []:
            return False
        if not isinstance(resources, list) or len(resources) != 1:
            raise ValueError("Canonical state has unexpected resources")
        item = resources[0]
        instances = item.get("instances")
        if (
            item.get("type") != "kind_cluster"
            or item.get("name") != "pilot"
            or item.get("mode") != "managed"
            or item.get("module")
            or item.get("provider") != f'provider["{PROVIDER}"]'
            or not isinstance(instances, list)
            or len(instances) != 1
            or instances[0].get("index_key") is not None
            or instances[0].get("attributes", {}).get("name") != self.target.name
        ):
            raise ValueError("Canonical state does not bind the registered #74 target")
        return True

    def verify_state_kubeconfig(self) -> None:
        """Bind a completed create to the live kind cluster before recording its first UID."""
        state = mapping(read_file(self.target.state))
        attributes = state["resources"][0]["instances"][0]["attributes"]
        live = yaml.safe_load(self.kind_config())
        if (
            yaml.safe_load(attributes["kubeconfig"]) != live
            or attributes["endpoint"] != live["clusters"][0]["cluster"]["server"]
        ):
            raise ValueError("OpenTofu's saved kubeconfig differs from the live kind cluster")

    def tofu(self, *args: str, write: bool = False, timeout: int = 180) -> str:
        return self.run(
            ["tofu", f"-chdir={self.work}", *args], tofu=True, write=write, timeout=timeout
        )

    def initialize(self) -> str:
        private_directory(self.work)
        private_directory(self.tofu_home)
        private_directory(self.tofu_home / ".kube")
        mirror = self.target.directory / "mirror"
        package = mirror / PROVIDER / PACKAGE
        check_path(package, directory=False, owner=os.geteuid(), prohibited=0o222)
        if fingerprint(package) != PACKAGE_DIGEST:
            raise ValueError("Local mirror archive differs from the pinned package")
        configuration = {
            name: read_file(MODULE / name) for name in ("main.tf", ".terraform.lock.hcl")
        }
        for name, value in configuration.items():
            store(self.work / name, value)
        # OpenTofu may load any .tf/.tofu input in this directory. Reject extras.
        if any(
            path.name not in configuration
            and path.name.endswith(
                (".tf", ".tf.json", ".tofu", ".tofu.json", ".tfvars", ".tfvars.json")
            )
            for path in self.work.iterdir()
        ):
            raise ValueError("Unexpected OpenTofu configuration in the approved private directory")
        store(
            self.cli_config,
            (
                "provider_installation {\n"
                "  filesystem_mirror {\n"
                f"    path = {json.dumps(str(mirror))}\n"
                f"    include = [{json.dumps(PROVIDER)}]\n"
                "  }\n}\n"
            ).encode(),
        )
        if mapping(self.tofu("version", "-json")).get("terraform_version") != "1.13.10":
            raise ValueError("The rehearsal requires OpenTofu 1.13.10")
        self.tofu(
            "init",
            "-no-color",
            "-input=false",
            "-reconfigure",
            "-lockfile=readonly",
            f"-backend-config=path={self.target.state}",
            write=True,
        )
        return digest(encoded({name: digest(value) for name, value in configuration.items()}))

    @contextmanager
    def staged_plan(self, source: Path | None = None) -> Iterator[Path]:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="plan-", dir=self.target.directory) as directory:
            path = Path(directory) / "saved.tfplan"
            if source is not None:
                check_path(source, directory=False, owner=os.geteuid(), prohibited=0o077)
                store(path, read_file(source))
            yield path

    def inspect(self, plan: Path, action: str) -> list[dict[str, Any]]:
        return validate_plan(
            mapping(self.tofu("show", "-json", str(plan))), action, self.target.name
        )

    def render_argo(self) -> bytes:
        argocd.validate_configuration(ROOT)
        output = self.run(["kubectl", "kustomize", str(ROOT / argocd.BOOTSTRAP)], timeout=180)
        if not output.strip():
            raise ValueError("Argo CD rendered an empty manifest")
        return output.encode()

    def argo_inventory(self) -> dict[str, str]:
        kube = [*self.kube_prefix(), "-n", argocd.NAMESPACE]
        namespace = self.run(argocd.namespace_command(kube))
        argocd.validate_live_namespace(namespace)
        deployments = [*argocd.argocd_status.DEPLOYMENTS]
        statefulsets = [*argocd.argocd_status.STATEFULSETS]
        objects = mapping(
            self.run(
                [
                    *kube,
                    "get",
                    *(f"deployment/{name}" for name in deployments),
                    *(f"statefulset/{name}" for name in statefulsets),
                    "-o",
                    "json",
                ]
            )
        )
        result = {
            item["metadata"][
                "name"
            ]: f"{uid(item['metadata']['uid'])}:{item['metadata']['generation']}"
            for item in objects["items"]
        }
        if result.keys() != set(deployments + statefulsets):
            raise ValueError("Argo CD controller inventory is incomplete")
        result[argocd.NAMESPACE] = uid(mapping(namespace)["metadata"]["uid"])
        for command in argocd.argocd_status.rollout_commands(kube):
            self.run(command, timeout=330)
        return result
