"""Offline rejection and orchestration coverage; real traffic is a separate live gate."""

from __future__ import annotations

import copy
import io
import json
import socket
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import consumer_runtime, network_assets, network_qualification, network_runtime
from scripts.consumer_profile import Target, configuration, identity
from scripts.kubernetes import contains, reference
from scripts.network import main
from scripts.network_fixtures import namespaces, policies, routes, workloads


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.target = Target("neutral", "local-engine", self.root, "isolated-network-local", 18443)

    def test_existing_profile_identity_and_networking_remain_unchanged(self):
        base = Target("neutral", "local-engine", self.root)
        argo = Target("neutral", "local-engine", self.root, "isolated-argo-local")
        for target in (base, argo):
            self.assertEqual(configuration(target)["networking"], {"apiServerAddress": "127.0.0.1"})
            self.assertFalse(
                any("extraPortMappings" in node for node in configuration(target)["nodes"])
            )
            self.assertNotIn("httpsPort", identity(target))

    def test_new_profile_binds_cni_proxy_and_loopback_port_into_ownership(self):
        value = configuration(self.target)
        self.assertEqual(
            value["networking"],
            {"apiServerAddress": "127.0.0.1", "disableDefaultCNI": True, "kubeProxyMode": "none"},
        )
        self.assertEqual(
            value["nodes"][1]["extraPortMappings"],
            [
                {
                    "containerPort": 8443,
                    "hostPort": 18443,
                    "listenAddress": "127.0.0.1",
                    "protocol": "TCP",
                }
            ],
        )
        other = Target("neutral", "local-engine", self.root, "isolated-network-local", 19443)
        self.assertNotEqual(identity(self.target), identity(other))
        for port in (None, True, 443, 65536):
            with self.subTest(port=port), self.assertRaises(ValueError):
                Target("neutral", "local-engine", self.root, "isolated-network-local", port)
        with self.assertRaises(ValueError):
            Target("neutral", "local-engine", self.root, "isolated-local", 18443)

    def test_preview_has_no_process_download_private_reads_or_state_writes(self):
        with (
            patch(
                "scripts.network.download", side_effect=AssertionError("preview download")
            ) as download,
            patch("scripts.consumer_runtime.command") as command,
            redirect_stdout(io.StringIO()) as out,
        ):
            self.assertEqual(
                main(
                    [
                        "qualify",
                        "--profile",
                        self.target.profile,
                        "--consumer",
                        self.target.consumer,
                        "--docker-context",
                        self.target.docker_context,
                        "--state-root",
                        str(self.root),
                        "--https-port",
                        "18443",
                    ]
                ),
                0,
            )
        self.assertEqual(json.loads(out.getvalue())["execution"], "not-performed")
        self.assertFalse(self.target.directory.exists())
        command.assert_not_called()
        download.assert_not_called()

    def test_occupied_listener_is_refused_without_substitution(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            target = Target(
                "neutral",
                "local-engine",
                self.root,
                "isolated-network-local",
                occupied.getsockname()[1],
            )
            with self.assertRaisesRegex(ValueError, "occupied"):
                network_runtime.listener_available(target)

    def test_actual_docker_listener_must_match_reviewed_loopback_mapping(self):
        node: dict = {
            "role": "worker",
            "ports": {"8443/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18443"}]},
        }
        consumer_runtime.verify_listener(self.target, node)
        for address, port in (("0.0.0.0", "18443"), ("127.0.0.1", "19443")):
            changed = copy.deepcopy(node)
            changed["ports"]["8443/tcp"][0] = {"HostIp": address, "HostPort": port}
            with self.assertRaises(ValueError):
                consumer_runtime.verify_listener(self.target, changed)
        with self.assertRaises(ValueError):
            consumer_runtime.verify_listener(
                self.target,
                {
                    "role": "control-plane",
                    "ports": {"6443/tcp": [{"HostIp": "0.0.0.0", "HostPort": "54321"}]},
                },
            )

    def test_install_refuses_existing_profiles_before_any_cluster_access(self):
        with patch.object(network_runtime.consumer_lifecycle, "owned") as owned:
            with self.assertRaisesRegex(ValueError, "no CNI replacement"):
                network_runtime.install(Target("neutral", "local-engine", self.root))
        owned.assert_not_called()

    def test_existing_cni_is_refused_before_asset_download_or_writes(self):
        with (
            patch.object(network_runtime.consumer_lifecycle, "owned"),
            patch.object(network_runtime, "kube", return_value="kindnet kube-proxy"),
            patch.object(network_runtime, "download") as download,
            patch.object(network_runtime, "apply_objects") as write,
            self.assertRaisesRegex(ValueError, "no in-place network replacement"),
        ):
            network_runtime.install(self.target)
        download.assert_not_called()
        write.assert_not_called()

    def test_bad_values_cannot_inject_hooks_or_expose_admin_interfaces(self):
        original = network_assets.values_configuration()
        for key, value in (("extraArgs", ["--unsafe"]), ("hubble", {"enabled": True})):
            changed = copy.deepcopy(original)
            changed[key] = value
            with (
                patch.object(network_assets.yaml, "safe_load", return_value=changed),
                self.assertRaises(ValueError),
            ):
                network_assets.values_configuration()

    def test_gateway_policy_grants_are_only_added_for_explicit_network_profile(self):
        from scripts.delegated_argo_config import Delegation
        from scripts.delegated_argo_policy import delegated_roles

        delegation = Delegation("neutral", ("neutral-apps",), ("reviewed",))

        def groups(networking):
            roles = delegated_roles(delegation, networking=networking)
            return {
                group
                for item in roles
                if item["kind"] == "Role" and item["metadata"]["name"] == "delegated-argocd"
                for rule in item["rules"]
                for group in rule["apiGroups"]
            }

        self.assertNotIn("gateway.networking.k8s.io", groups(False))
        self.assertNotIn("cilium.io", groups(False))
        self.assertTrue({"gateway.networking.k8s.io", "cilium.io"} <= groups(True))

    def test_asset_integrity_and_official_release_sources_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            network_assets.verified(b"altered", network_assets.selection()["cilium"])
        changed = copy.deepcopy(network_assets.selection())
        changed["ciliumCRDs"][0]["url"] = "https://unreviewed.invalid/manifest.yaml"
        with (
            patch.object(network_assets.json, "loads", return_value=changed),
            self.assertRaises(ValueError),
        ):
            network_assets.selection()

    def test_inventory_refuses_foreign_and_incompatible_shared_schemas(self):
        expected: dict = {
            "apiVersion": "apiextensions.k8s.io/v1",
            "kind": "CustomResourceDefinition",
            "metadata": {
                "name": "reviewed.example.test",
                "labels": network_assets.labels(self.target),
            },
            "spec": {"group": "example.test"},
        }
        for field in ("labels", "schema"):
            actual = copy.deepcopy(expected)
            if field == "labels":
                actual["metadata"]["labels"]["app.kubernetes.io/managed-by"] = "foreign"
            else:
                actual["spec"]["group"] = "different.test"
            with (
                patch.object(network_runtime, "kube", return_value=json.dumps({"items": [actual]})),
                self.assertRaises(ValueError),
            ):
                network_runtime.inventory(self.target, [expected])
        with patch.object(network_runtime, "kube") as kube, self.assertRaises(ValueError):
            network_runtime.inventory(self.target, [{"kind": "Secret"}])
        kube.assert_not_called()

    def test_network_probe_failures_cannot_be_classified_as_policy_denials(self):
        for output in ("", "000", "28 invalid", "failure"):
            with (
                patch.object(network_qualification, "kube", return_value=output),
                self.assertRaises(ValueError),
            ):
                network_qualification.probe(self.target, "neutral-net-a", "http://10.0.0.1:8080")
        with (
            patch.object(network_qualification, "kube", side_effect=ValueError("API unavailable")),
            self.assertRaises(ValueError),
        ):
            network_qualification.expect_probe(
                self.target, "neutral-net-a", "http://10.0.0.1:8080", (28, 0)
            )
        with patch.object(network_qualification, "kube", return_value="28 000"):
            self.assertEqual(
                network_qualification.probe(self.target, "neutral-net-a", "http://10.0.0.1:8080"),
                (28, 0),
            )

    def test_qualification_never_adopts_existing_neutral_namespaces(self):
        with (
            patch.object(network_qualification, "kube", return_value="existing-uid") as kube,
            self.assertRaisesRegex(ValueError, "namespace exists"),
        ):
            network_qualification.create_fixtures(self.target)
        self.assertEqual(kube.call_count, 1)
        self.assertEqual(kube.call_args.args[1][0], "get")

    def test_neutral_fixtures_cover_two_consumers_and_do_not_mount_credentials(self):
        docs = workloads(self.target)
        self.assertEqual(len({reference(item) for item in docs}), len(docs))
        identities = [
            item["metadata"]["labels"]["k8s-infrastructure.io/consumer"]
            for item in docs
            if item["kind"] == "Namespace"
        ]
        self.assertEqual(len(set(identities)), 2)
        for item in docs:
            if item["kind"] == "Pod":
                self.assertFalse(item["spec"]["automountServiceAccountToken"])
                self.assertNotIn("hostNetwork", item["spec"])
                self.assertIn("@sha256:", item["spec"]["containers"][0]["image"])
        self.assertTrue(any(item["kind"] == "NetworkPolicy" for item in policies(self.target)))
        gateway = routes(self.target, "neutral.localhost")[0]
        listener = gateway["spec"]["listeners"][0]
        self.assertEqual(listener["protocol"], "HTTPS")
        self.assertEqual(listener["allowedRoutes"]["namespaces"]["from"], "Same")
        self.assertEqual(gateway["metadata"]["namespace"], namespaces(self.target)[0])

    def test_server_defaults_cannot_inject_controller_credentials(self):
        expected: dict = {"spec": {"template": {"spec": {"containers": [{"name": "controller"}]}}}}
        actual = copy.deepcopy(expected)
        actual["spec"]["template"]["spec"]["containers"][0]["envFrom"] = [
            {"secretRef": {"name": "unexpected"}}
        ]
        with self.assertRaises(ValueError):
            network_runtime.verify_pod_template(expected, actual)

    def test_native_controller_serialization_preserves_drift_detection(self):
        expected: dict = {
            "kind": "DaemonSet",
            "spec": {
                "template": {
                    "metadata": {"annotations": None},
                    "spec": {
                        "containers": [
                            {
                                "resources": {"limits": {"cpu": "1000m"}},
                                "volumeMounts": [{"name": "config", "readOnly": False}],
                                "readinessProbe": {"initialDelaySeconds": 0},
                            }
                        ],
                        "initContainers": [{"resources": {"limits": {"cpu": 1}}}],
                    },
                }
            },
        }
        actual = copy.deepcopy(expected)
        del actual["spec"]["template"]["metadata"]["annotations"]
        pod = actual["spec"]["template"]["spec"]
        pod["containers"][0]["resources"]["limits"]["cpu"] = "1"
        pod["initContainers"][0]["resources"]["limits"]["cpu"] = "1"
        del pod["containers"][0]["volumeMounts"][0]["readOnly"]
        del pod["containers"][0]["readinessProbe"]["initialDelaySeconds"]
        wanted = network_runtime.comparable_spec(expected)
        self.assertTrue(contains(wanted, network_runtime.comparable_spec(actual)))
        for field in ("cpu", "mount", "probe", "annotation"):
            changed = copy.deepcopy(actual)
            container = changed["spec"]["template"]["spec"]["containers"][0]
            if field == "cpu":
                container["resources"]["limits"]["cpu"] = "2"
            elif field == "mount":
                container["volumeMounts"][0]["readOnly"] = True
            elif field == "probe":
                container["readinessProbe"]["initialDelaySeconds"] = 30
            else:
                changed["spec"]["template"]["metadata"]["annotations"] = {"unexpected": "true"}
            with self.subTest(field=field):
                self.assertFalse(contains(wanted, network_runtime.comparable_spec(changed)))
        self.assertEqual(
            expected["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["cpu"],
            "1000m",
        )
