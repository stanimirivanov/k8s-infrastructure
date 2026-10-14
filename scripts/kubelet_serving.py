"""Explicit, identity-fenced serving-certificate rollout on the reviewed kind nodes."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any

import yaml

from . import local_kind, metrics_certificates

CONFIG = "/var/lib/kubelet/config.yaml"
BACKUP = CONFIG + ".before-resource-metrics"


class KubeletLoader(yaml.SafeLoader):
    """Safe YAML with duplicate/non-string mapping keys refused before node edits."""


def unique_mapping(loader: yaml.SafeLoader, node: yaml.Node) -> dict[str, Any]:
    """Construct an unambiguous kubelet mapping without changing key meaning."""
    if not isinstance(node, yaml.MappingNode):
        raise ValueError("Kubelet configuration must use mappings")
    result: dict[str, Any] = {}
    for key, value in loader.construct_pairs(node, deep=True):
        if not isinstance(key, str) or key in result:
            raise ValueError("Duplicate or non-string kubelet configuration field")
        result[key] = value
    return result


KubeletLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def container_id(name: str) -> str:
    """Require the reviewed node image and kind owner before using Docker exec."""
    template = '{"image":{{json .Config.Image}},"id":{{json .Id}},"cluster":{{json (index .Config.Labels "io.x-k8s.kind.cluster")}}}'
    value = json.loads(
        local_kind.run(
            ["docker", "inspect", "--type", "container", "--format", template, name], timeout=15
        )
    )
    aliases = {local_kind.NODE_IMAGE, "kindest/node@" + local_kind.NODE_IMAGE.split("@", 1)[1]}
    if (
        value.get("image") not in aliases
        or value.get("cluster") != local_kind.NAME
        or not isinstance(value.get("id"), str)
        or re.fullmatch(r"[a-f0-9]{64}", value["id"]) is None
    ):
        raise ValueError("Kubelet container image or kind ownership differs")
    return value["id"]


def bootstrap_configuration(content: str, flags: str) -> bytes:
    """Preserve kubelet configuration except the three explicit serving-TLS fields.

    Command-line serving-certificate overrides would defeat this rollout and are
    rejected rather than silently editing systemd or another provider's flags.
    """
    if len(content) > 65536 or len(flags) > 65536:
        raise ValueError("Kubelet configuration exceeds its review bound")
    if "--tls-cert-file" in flags or "--tls-private-key-file" in flags:
        raise ValueError("Kubelet command-line TLS overrides require separate operator review")
    configuration = yaml.load(content, Loader=KubeletLoader)
    if (
        not isinstance(configuration, dict)
        or configuration.get("kind") != "KubeletConfiguration"
        or configuration.get("apiVersion") != "kubelet.config.k8s.io/v1beta1"
    ):
        raise ValueError("Unsupported kubelet configuration")
    configuration["serverTLSBootstrap"] = True
    configuration.pop("tlsCertFile", None)
    configuration.pop("tlsPrivateKeyFile", None)
    return yaml.safe_dump(configuration, sort_keys=False).encode()


def enable(kube: list[str], nodes: list[dict[str, Any]], guard: Callable[[], None]) -> None:
    """Roll out serving bootstrap one node at a time after prevalidating all inputs.

    The original node-local file is preserved. No certificates or CSRs are
    approved here. All writes require the caller's current cluster/node guard.
    """
    plans = []
    for node in nodes:
        name = node["metadata"]["name"]
        identity = container_id(name)
        content = local_kind.run(["docker", "exec", identity, "cat", CONFIG], timeout=30)
        flags = local_kind.run(
            [
                "docker",
                "exec",
                identity,
                "sh",
                "-ec",
                "cat /var/lib/kubelet/kubeadm-flags.env /etc/default/kubelet",
            ],
            timeout=30,
        )
        prepared = bootstrap_configuration(content, flags)
        plans.append((name, identity, content, prepared))
    for name, identity, content, prepared in plans:
        guard()
        if container_id(name) != identity:
            raise ValueError("Kubelet container was replaced during rollout")
        current = yaml.safe_load(content)
        active = json.loads(
            local_kind.run(
                [*kube, "get", "--raw", f"/api/v1/nodes/{name}/proxy/configz"], timeout=15
            )
        )["kubeletconfig"]
        configured = (
            current.get("serverTLSBootstrap") is True
            and not current.get("tlsCertFile")
            and not current.get("tlsPrivateKeyFile")
        )
        if not configured:
            digest = hashlib.sha256(content.encode()).hexdigest()
            # Fixed node-local paths, exclusive temporary creation and two content
            # checks prevent overwriting an intervening operator edit.
            script = (
                "set -euC; umask 077; "
                "test ! -L /var/lib/kubelet/config.yaml; "
                'test "$(sha256sum /var/lib/kubelet/config.yaml | cut -d\' \' -f1)" = "$1"; '
                "test ! -e /var/lib/kubelet/config.yaml.before-resource-metrics; "
                "test ! -L /var/lib/kubelet/config.yaml.before-resource-metrics; "
                "cp -p /var/lib/kubelet/config.yaml /var/lib/kubelet/config.yaml.before-resource-metrics; "
                "cat > /var/lib/kubelet/config.yaml.resource-metrics-new; "
                'test "$(sha256sum /var/lib/kubelet/config.yaml | cut -d\' \' -f1)" = "$1"; '
                "mv /var/lib/kubelet/config.yaml.resource-metrics-new /var/lib/kubelet/config.yaml"
            )
            local_kind.run(
                ["docker", "exec", "-i", identity, "sh", "-c", script, "resource-metrics", digest],
                input_data=prepared,
                timeout=30,
            )
        if (
            not configured
            or active.get("serverTLSBootstrap") is not True
            or active.get("tlsCertFile")
            or active.get("tlsPrivateKeyFile")
        ):
            guard()
            if container_id(name) != identity:
                raise ValueError("Kubelet container was replaced before restart")
            actual_hash = local_kind.run(
                ["docker", "exec", identity, "sha256sum", CONFIG], timeout=15
            ).split()[0]
            expected_hash = hashlib.sha256(content.encode() if configured else prepared).hexdigest()
            if actual_hash != expected_hash:
                raise ValueError("Kubelet configuration changed before restart")
            print(
                f"Restarting kubelet serving TLS on {name}; original file retained at {BACKUP}.",
                flush=True,
            )
            local_kind.run(
                ["docker", "exec", identity, "systemctl", "restart", "kubelet"], timeout=60
            )
            local_kind.run(
                [*kube, "wait", f"node/{name}", "--for=condition=Ready", "--timeout=120s"],
                timeout=130,
            )
        print(
            f"Serving bootstrap configured on {name}; inspect and explicitly approve its serving CSR.",
            flush=True,
        )


def approve(
    kube: list[str],
    request: dict[str, Any],
    node: dict[str, Any],
    now: datetime,
    guard: Callable[[], None],
) -> None:
    """Approve one reviewed CSR with API resource-version/UID concurrency fencing."""
    metrics_certificates.review_request(request, node, now)
    metadata = request["metadata"]
    if not metadata.get("uid") or not metadata.get("resourceVersion"):
        raise ValueError("CSR lacks API concurrency identity")
    if any(c.get("type") == "Approved" for c in request.get("status", {}).get("conditions", [])):
        print("Selected serving CSR is already approved; no write performed.")
        return
    update = copy.deepcopy(request)
    update.setdefault("status", {}).setdefault("conditions", []).append(
        {
            "type": "Approved",
            "status": "True",
            "reason": "ReviewedNodeServingIdentity",
            "message": "Explicit operator approval after checking node identity and address SANs",
            "lastUpdateTime": now.isoformat().replace("+00:00", "Z"),
        }
    )
    guard()
    local_kind.run(
        [
            *kube,
            "replace",
            "--raw",
            f"/apis/certificates.k8s.io/v1/certificatesigningrequests/{metadata['name']}/approval",
            "-f",
            "-",
        ],
        input_data=json.dumps(update).encode(),
        timeout=20,
    )
