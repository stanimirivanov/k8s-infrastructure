"""Offline capability refusals and boundaries; native persistence is a separate gate."""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import database_assets as assets
from scripts import database_policy as policy
from scripts import database_qualification as qualification
from scripts import database_runtime as runtime
from scripts.consumer_profile import Target
from scripts.database import main
from scripts.delegated_argo_config import Delegation, load_delegation
from scripts.delegated_argo_policy import delegated_roles


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.target = Target("neutral", "local-engine", self.root, "isolated-network-local", 18444)

    def sources(self):
        result: dict = {"cert-manager": [], "cnpg": [], "barman": []}
        for index in range(18):
            group = "cert-manager" if index < 6 else "cnpg" if index < 17 else "barman"
            result[group].append(
                {"kind": "CustomResourceDefinition", "metadata": {"name": f"schema-{index}"}}
            )
        images = assets.selection()["images"]
        for name, (namespace, _, _, _, _) in assets.CONTROLLERS.items():
            group = (
                "cert-manager"
                if namespace == "cert-manager"
                else "cnpg"
                if name.startswith("cnpg")
                else "barman"
            )
            key = "cnpg" if group == "cnpg" else "barman" if group == "barman" else name
            result[group].append(
                {
                    "kind": "Deployment",
                    "metadata": {"name": name, "namespace": namespace},
                    "spec": {
                        "replicas": 1,
                        "template": {
                            "spec": {
                                "securityContext": {"runAsNonRoot": True},
                                "containers": [
                                    {
                                        "image": images[key].split("@")[0],
                                        "securityContext": {
                                            "allowPrivilegeEscalation": False,
                                            "readOnlyRootFilesystem": True,
                                            "capabilities": {"drop": ["ALL"]},
                                        },
                                    }
                                ],
                            }
                        },
                    },
                }
            )
        result["barman"].append(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "public-image", "namespace": "cnpg-system"},
                "data": {
                    "SIDECAR_IMAGE": base64.b64encode(
                        images["sidecar"].split("@")[0].encode()
                    ).decode()
                    + "\n"
                },
            }
        )
        return result

    def test_preview_never_reads_private_state_or_invokes_processes(self):
        with (
            patch("scripts.database.download") as fetch,
            patch("scripts.consumer_runtime.command") as command,
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(
                main(
                    [
                        "qualify",
                        "--profile",
                        self.target.profile,
                        "--consumer",
                        "neutral",
                        "--docker-context",
                        "local-engine",
                        "--state-root",
                        str(self.root),
                        "--https-port",
                        "18444",
                    ]
                ),
                0,
            )
        self.assertEqual(json.loads(output.getvalue())["execution"], "not-performed")
        self.assertFalse(self.target.directory.exists())
        fetch.assert_not_called()
        command.assert_not_called()

    def test_old_profile_is_refused_before_cluster_access(self):
        target = Target("neutral", "local-engine", self.root)
        with patch.object(runtime, "network_health") as health, self.assertRaises(ValueError):
            runtime.install(target)
        health.assert_not_called()

    def test_release_sources_and_bytes_are_verified(self):
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            assets.parse(b"changed", assets.selection()["assets"]["cnpg"])
        altered = copy.deepcopy(assets.selection())
        altered["assets"]["barman"]["url"] = "https://unreviewed.invalid/manifest.yaml"
        with (
            patch.object(assets.json, "loads", return_value=altered),
            self.assertRaises(ValueError),
        ):
            assets.selection()

    def test_render_pins_images_bounds_resources_and_avoids_configuration_secrets(self):
        original = self.sources()
        result = assets.render(self.target, original)
        self.assertTrue(any(d["kind"] == "Secret" for d in original["barman"]))
        self.assertFalse(any(d["kind"] == "Secret" for docs in result.values() for d in docs))
        config = next(d for d in result["barman"] if d["kind"] == "ConfigMap")
        self.assertEqual(config["data"]["SIDECAR_IMAGE"], assets.selection()["images"]["sidecar"])
        for docs in result.values():
            for item in docs:
                if item["kind"] == "Deployment":
                    pod = item["spec"]["template"]["spec"]
                    self.assertIn("@sha256:", pod["containers"][0]["image"])
                    self.assertEqual(pod["containers"][0]["resources"]["limits"]["cpu"], "500m")

    def test_source_cannot_add_host_access_or_credential_payloads(self):
        for mutation in ("hostPath", "privileged", "secret"):
            source = self.sources()
            if mutation == "secret":
                source["barman"][-1]["data"]["password"] = "unapproved"
            else:
                pod = source["cnpg"][-1]["spec"]["template"]["spec"]
                if mutation == "hostPath":
                    pod["volumes"] = [{"name": "escape", "hostPath": {"path": "/"}}]
                else:
                    pod["containers"][0]["securityContext"]["privileged"] = True
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                assets.render(self.target, source)

    def test_secret_inventory_is_refused_before_any_request(self):
        with patch.object(runtime, "kube") as kube, self.assertRaises(ValueError):
            runtime.inventory(self.target, [{"kind": "Secret"}])
        kube.assert_not_called()
        with patch.object(runtime, "kube") as kube:
            self.assertEqual(runtime.inventory(self.target, []), {})
        kube.assert_not_called()

    def test_foreign_or_changed_storage_ownership_is_not_adopted(self):
        desired = assets.storage_class(self.target)
        actual = copy.deepcopy(desired)
        actual["metadata"]["labels"]["app.kubernetes.io/managed-by"] = "foreign"
        with (
            patch.object(runtime, "kube", return_value=json.dumps({"items": [actual]})),
            self.assertRaises(ValueError),
        ):
            runtime.inventory(self.target, [desired])

    def test_retained_class_does_not_become_default_or_claim_expansion(self):
        value = assets.storage_class(self.target)
        self.assertEqual(value["reclaimPolicy"], "Retain")
        self.assertEqual(value["volumeBindingMode"], "WaitForFirstConsumer")
        self.assertFalse(value["allowVolumeExpansion"])
        self.assertNotIn("annotations", value["metadata"])

    def test_database_authority_requires_explicit_namespace_opt_in(self):
        old = Delegation("neutral", ("neutral-app", "neutral-db"), ("release-approved",))
        original = [old.consumer, old.workload_namespaces, old.approved_templates]
        self.assertEqual(
            old.digest,
            hashlib.sha256(json.dumps(original, separators=(",", ":")).encode()).hexdigest(),
        )
        selected = Delegation(
            "neutral", old.workload_namespaces, old.approved_templates, ("neutral-db",)
        )
        self.assertNotEqual(old.digest, selected.digest)
        for delegation in (old, selected):
            roles = [
                d
                for d in delegated_roles(delegation)
                if d["kind"] == "Role" and d["metadata"]["name"] == "delegated-argocd"
            ]
            for role in roles:
                groups = {g for rule in role["rules"] for g in rule["apiGroups"]}
                self.assertEqual(
                    "postgresql.cnpg.io" in groups,
                    delegation is selected and role["metadata"]["namespace"] == "neutral-db",
                )
                self.assertNotIn("cert-manager.io", groups)
        with self.assertRaises(ValueError):
            Delegation("neutral", old.workload_namespaces, old.approved_templates, ("foreign-db",))

    def test_json_delegation_preserves_old_format_and_validates_new_list(self):
        file = self.root / "delegation.json"
        value = {
            "consumer": "neutral",
            "workloadNamespaces": ["neutral-db"],
            "approvedTemplates": ["release-approved"],
        }
        for extra in ({}, {"databaseNamespaces": ["neutral-db"]}):
            file.write_text(json.dumps({**value, **extra}), encoding="utf-8")
            self.assertEqual(
                bool(load_delegation(file, self.target).database_namespaces), bool(extra)
            )
        file.write_text(json.dumps({**value, "databaseNamespaces": "neutral-db"}), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_delegation(file, self.target)

    def test_storage_scope_is_bounded_and_uses_native_fail_closed_enforcement(self):
        docs = policy.objects(self.target, ("neutral-db",))
        quota = next(d for d in docs if d["kind"] == "ResourceQuota")
        self.assertEqual(
            quota["spec"]["hard"], {"requests.storage": "8Gi", "persistentvolumeclaims": "4"}
        )
        admission = next(d for d in docs if d["kind"] == "ValidatingAdmissionPolicy")
        self.assertEqual(admission["spec"]["failurePolicy"], "Fail")
        self.assertIn('"neutral-db"', admission["spec"]["matchConditions"][0]["expression"])
        for names in ((), ("foreign-db",), ("neutral-../escape",), ("neutral-db", "neutral-db")):
            with self.subTest(names=names), self.assertRaises(ValueError):
                policy.objects(self.target, names)

    def test_native_denial_requires_a_healthy_positive_control(self):
        with (
            patch.object(
                qualification,
                "kube",
                side_effect=[*["ok"] * 4, ValueError("denied"), ValueError("unavailable")],
            ),
            self.assertRaises(ValueError),
        ):
            qualification.admission(self.target, "neutral-db")
        with (
            patch.object(qualification, "kube", return_value="accepted"),
            self.assertRaisesRegex(ValueError, "admitted"),
        ):
            qualification.admission(self.target, "neutral-db")

    def test_native_admission_probes_all_sizes_and_rejects_fractional_requests(self):
        accepted, rejected = [], []

        def native(_target, _args, docs):
            item = docs[0]
            if item["kind"] == "ObjectStore":
                return "accepted"
            spec = item["spec"]
            size = spec["resources"]["requests"]["storage"]
            if (
                spec["storageClassName"] != "local-retained"
                or spec["accessModes"] != ["ReadWriteOnce"]
                or size not in {"1Gi", "2Gi", "3Gi", "4Gi"}
            ):
                rejected.append(size)
                raise ValueError("denied")
            accepted.append(size)
            return "accepted"

        with patch.object(qualification, "kube", side_effect=native):
            qualification.admission(self.target, "neutral-db")
        self.assertEqual(set(accepted), {"1Gi", "2Gi", "3Gi", "4Gi"})
        self.assertEqual(len(rejected), 5)
        self.assertIn("1536Mi", rejected)
        self.assertIn("512Mi", rejected)

    def test_admission_waits_for_current_generation_and_refuses_current_warnings(self):
        found = {}
        for item in policy.objects(self.target, ("neutral-db",)):
            item["metadata"]["generation"] = 2
            if item["kind"] == "ValidatingAdmissionPolicy":
                item["status"] = {
                    "observedGeneration": 1,
                    "typeChecking": {"expressionWarnings": [{"warning": "old expression"}]},
                }
            found[runtime.reference(item)] = item
        with (
            patch.object(policy, "inspect_namespaces"),
            patch.object(policy, "inventory", return_value=found),
        ):
            with self.assertRaisesRegex(ValueError, "incomplete"):
                policy.health(self.target, ("neutral-db",))
            for item in found.values():
                if item["kind"] == "ValidatingAdmissionPolicy":
                    item["status"]["observedGeneration"] = 2
            with self.assertRaisesRegex(ValueError, "failed"):
                policy.health(self.target, ("neutral-db",))

    def test_admission_health_refuses_selectors_that_bypass_enforcement(self):
        desired = policy.objects(self.target, ("neutral-db",))
        found = {}
        for item in copy.deepcopy(desired):
            item["metadata"]["generation"] = 1
            if item["kind"] == "ValidatingAdmissionPolicy":
                item["status"] = {"observedGeneration": 1}
            found[(item["kind"], item["metadata"]["name"], item["metadata"].get("namespace"))] = (
                item
            )
        guards = [d for d in found.values() if d["kind"] == "ValidatingAdmissionPolicy"]
        self.assertEqual(len(guards), 2)
        self.assertTrue(
            all(len(d["spec"]["matchConstraints"]["resourceRules"]) == 1 for d in guards)
        )
        for key in ("namespaceSelector", "objectSelector", "excludeResourceRules"):
            altered = copy.deepcopy(found)
            guard = next(d for d in altered.values() if d["kind"] == "ValidatingAdmissionPolicy")
            guard["spec"]["matchConstraints"][key] = {"unapproved": "scope"}
            with (
                self.subTest(key=key),
                patch.object(policy, "inspect_namespaces"),
                patch.object(policy, "inventory", return_value=altered),
                self.assertRaisesRegex(ValueError, "narrowed"),
            ):
                policy.health(self.target, ("neutral-db",))

    def test_existing_neutral_fixture_namespace_is_not_adopted(self):
        with (
            patch.object(qualification, "health", return_value={}),
            patch.object(qualification, "get", return_value={"metadata": {"uid": "existing"}}),
            patch.object(qualification, "kube") as kube,
            self.assertRaisesRegex(ValueError, "namespace exists"),
        ):
            qualification.qualify(self.target)
        kube.assert_not_called()

    def test_destructive_fixture_step_uses_server_uid_preconditions(self):
        item = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "neutral", "namespace": "neutral-db", "uid": "exact-old-uid"},
        }
        with patch.object(qualification, "command") as command:
            qualification.uid_delete(self.target, item)
        body = json.loads(command.call_args.kwargs["input_data"])
        self.assertEqual(body["preconditions"], {"uid": "exact-old-uid"})
        self.assertIn("/api/v1/namespaces/neutral-db/pods/neutral", command.call_args.args[1])
