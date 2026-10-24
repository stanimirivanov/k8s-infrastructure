"""Offline boundary tests; real admission/execution is a separate native qualification."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import yaml

from scripts import delegated_argo_config as config
from scripts import delegated_argo_qualification as qualification
from scripts import delegated_argo_runtime as runtime
from scripts.consumer_profile import Target
from scripts.delegated_argo import main
from scripts.delegated_argo_policy import admission, delegated_roles, resource
from scripts.delegated_argo_render import reference, render
from scripts.workflows import CRDS


def fixture_workflow_crd() -> dict:
    """Representative closed fields; the asset gate uses the complete pinned schema."""

    def obj(**properties):
        return {"type": "object", "properties": properties}

    schema = obj(
        spec=obj(
            workflowTemplateRef=obj(name={}, clusterScope={}),
            arguments=obj(
                parameters={"type": "array", "items": obj(name={}, value={}, valueFrom={})},
                artifacts={},
            ),
            suspend={},
            entrypoint={},
            podSpecPatch={},
        ),
        status=obj(phase={}),
    )
    return resource(
        "CustomResourceDefinition",
        "workflows.argoproj.io",
        None,
        spec={
            "group": "argoproj.io",
            "versions": [{"name": "v1alpha1", "schema": {"openAPIV3Schema": schema}}],
        },
    )


def fixture_assets() -> dict[str, list[dict]]:
    """Small synthetic upstream topology; never substitutes for SHA-pinned asset checks."""
    crds = [
        fixture_workflow_crd()
        if name == "workflows.argoproj.io"
        else resource("CustomResourceDefinition", name, None, spec={"group": "argoproj.io"})
        for name in sorted(CRDS)
    ]
    cd_crds = [
        resource("CustomResourceDefinition", name, None, spec={"group": "argoproj.io"})
        for name in (
            "applications.argoproj.io",
            "applicationsets.argoproj.io",
            "appprojects.argoproj.io",
        )
    ]

    def deployment(name: str, kind: str = "Deployment") -> dict:
        return resource(
            kind,
            name,
            "upstream",
            spec={
                "template": {"spec": {"containers": [{"name": name, "image": "official:reviewed"}]}}
            },
        )

    cd = [
        deployment(name)
        for name in (
            "argocd-applicationset-controller",
            "argocd-dex-server",
            "argocd-notifications-controller",
            "argocd-redis",
            "argocd-repo-server",
            "argocd-server",
        )
    ]
    cd += [deployment("argocd-application-controller", "StatefulSet")]
    cd += [resource("ConfigMap", name, "upstream") for name in ("argocd-cm", "argocd-rbac-cm")]
    wf = [
        *crds,
        resource("PriorityClass", "workflow-controller", None, value=1000),
        deployment("workflow-controller"),
        deployment("argo-server"),
        resource("ConfigMap", "workflow-controller-configmap", "upstream"),
    ]
    wf += [
        resource(
            "Role",
            name,
            "upstream",
            rules=[
                {
                    "apiGroups": ["argoproj.io"],
                    "resources": ["workflows"],
                    "verbs": ["get", "list", "watch"],
                }
            ],
        )
        for name in ("argo-role", "argo-server-role")
    ]
    return {
        "argocd": cd,
        "argocd-apis": [*cd_crds, resource("ClusterRoleBinding", "must-never-apply", None)],
        "workflows": wf,
    }


class DelegatedArgoTests(unittest.TestCase):
    def test_api_omission_of_empty_arrays_does_not_allow_injected_entries(self):
        self.assertTrue(runtime.contains({"env": []}, {}))
        self.assertTrue(runtime.contains({"env": []}, {"env": []}))
        self.assertFalse(runtime.contains({"env": []}, {"env": [{"name": "INJECTED"}]}))
        self.assertFalse(runtime.contains({"env": [{"name": "REQUIRED"}]}, {}))

    def test_server_defaults_cannot_hide_extra_admission_or_service_authority(self):
        policy = admission(
            config.Delegation("checked", ("checked-apps",), ("reviewed",)), fixture_workflow_crd()
        )[0]
        changed = copy.deepcopy(policy)
        changed["spec"]["matchConditions"] = [{"name": "bypass", "expression": "false"}]
        with self.assertRaises(ValueError):
            runtime.verify_security(policy, changed)
        service = resource("Service", "internal", "checked-system", spec={"ports": []})
        changed = copy.deepcopy(service)
        changed["spec"]["type"] = "NodePort"
        with self.assertRaises(ValueError):
            runtime.verify_security(service, changed)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.target = Target("consumer-a", "local-engine", self.root, "isolated-argo-local")
        self.delegation = config.Delegation(
            "consumer-a", ("consumer-a-apps",), (qualification.FIXTURE,)
        )
        self.path = self.root / "delegation.json"
        self.path.write_text(
            json.dumps(
                {
                    "consumer": "consumer-a",
                    "workloadNamespaces": ["consumer-a-apps"],
                    "approvedTemplates": [qualification.FIXTURE],
                }
            ),
            encoding="utf-8",
        )

    def test_explicit_delegation_rejects_foreign_reserved_and_oversized_inputs(self):
        self.assertEqual(config.load_delegation(self.path, self.target), self.delegation)
        for namespaces, templates in [
            (("perfeng-apps",), ("reviewed",)),
            (("consumer-a-releases",), ("reviewed",)),
            (("consumer-a-apps",) * 2, ("reviewed",)),
            (("consumer-a-apps",), ("bad/name",)),
            (("consumer-a-apps",), ()),
        ]:
            with (
                self.subTest(namespaces=namespaces, templates=templates),
                self.assertRaises(ValueError),
            ):
                config.Delegation("consumer-a", namespaces, templates)
        value = json.loads(self.path.read_text())
        value["consumer"] = "foreign"
        self.path.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            config.load_delegation(self.path, self.target)

    def test_preview_is_offline_and_never_creates_private_state(self):
        with (
            patch.object(runtime, "kube") as kube,
            patch.object(config, "download_assets") as download,
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(
                main(
                    [
                        "apply",
                        "--profile",
                        self.target.profile,
                        "--consumer",
                        self.target.consumer,
                        "--docker-context",
                        self.target.docker_context,
                        "--state-root",
                        str(self.root),
                        "--delegation",
                        str(self.path),
                    ]
                ),
                0,
            )
        self.assertEqual(json.loads(output.getvalue())["execution"], "not-performed")
        kube.assert_not_called()
        download.assert_not_called()
        self.assertFalse(self.target.directory.exists())

    def test_asset_bytes_are_verified_before_parsing(self):
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            config.parse_asset(b"unreviewed", config.selection()["argocd"])
        self.assertEqual(
            config.selection()["argocd"]["version"], config.selection()["argocd-apis"]["version"]
        )

    def test_verified_yaml_is_loaded_as_data_and_cannot_construct_python_objects(self):
        valid = b"kind: ConfigMap\nmetadata:\n  name: public-fixture\n"
        self.assertEqual(
            config.parse_asset(valid, {"sha256": hashlib.sha256(valid).hexdigest()}),
            [{"kind": "ConfigMap", "metadata": {"name": "public-fixture"}}],
        )
        unsafe = b"!!python/object:builtins.object {}"
        with self.assertRaises(yaml.YAMLError):
            config.parse_asset(unsafe, {"sha256": hashlib.sha256(unsafe).hexdigest()})

    def test_shared_apis_have_one_consumer_independent_owner(self):
        shared, documents = render(self.delegation, fixture_assets())
        other = config.Delegation("consumer-b", ("consumer-b-apps",), ("another-template",))
        second, _ = render(other, fixture_assets())
        self.assertEqual(shared, second)
        self.assertEqual(len(shared), 12)
        self.assertFalse(
            any(
                item["kind"]
                in {
                    "ClusterRole",
                    "ClusterRoleBinding",
                    "CustomResourceDefinition",
                    "PriorityClass",
                }
                for item in documents
            )
        )
        for item in [*shared, *documents]:
            self.assertTrue(all(len(value) <= 63 for value in item["metadata"]["labels"].values()))
        self.assertEqual(
            {item["metadata"]["name"] for item in documents if item["kind"] == "Namespace"},
            {self.delegation.gitops, self.delegation.controllers},
        )

    def test_controllers_are_disjoint_bounded_and_servers_internal(self):
        _, documents = render(self.delegation, fixture_assets())
        deployments = [item for item in documents if item["kind"] in {"Deployment", "StatefulSet"}]
        self.assertEqual(len(deployments), 9)
        for item in deployments:
            pod = item["spec"]["template"]["spec"]
            self.assertEqual(pod["nodeSelector"][config.LABEL], self.target.consumer)
            self.assertEqual(pod["containers"][0]["resources"]["limits"]["memory"], "384Mi")
            if item["metadata"]["namespace"] == self.delegation.controllers:
                self.assertIn("--namespaced", pod["containers"][0]["args"])
                self.assertIn(self.delegation.managed, pod["containers"][0]["args"])
                self.assertNotIn("--instanceid", pod["containers"][0]["args"])
        upstream = fixture_assets()
        upstream["argocd"].append(resource("ClusterRole", "unexpected", None))
        with self.assertRaisesRegex(ValueError, "cluster authority"):
            render(self.delegation, upstream)

    def test_secure_mode_has_native_fail_closed_submission_and_template_rules(self):
        documents = admission(self.delegation, fixture_workflow_crd())
        policies = [item for item in documents if item["kind"] == "ValidatingAdmissionPolicy"]
        for policy in policies:
            self.assertEqual(policy["spec"]["failurePolicy"], "Fail")
            self.assertEqual(
                policy["spec"]["matchConstraints"]["namespaceSelector"]["matchLabels"][
                    "kubernetes.io/metadata.name"
                ],
                self.delegation.managed,
            )
        self.assertIn(
            "!has(object.spec.entrypoint)", policies[0]["spec"]["validations"][0]["expression"]
        )
        self.assertIn(
            "object.status == oldObject.status", policies[0]["spec"]["validations"][0]["expression"]
        )
        self.assertTrue(
            all(
                item["spec"]["validationActions"] == ["Deny", "Audit"]
                for item in documents
                if item["kind"].endswith("Binding")
            )
        )

    def test_new_fields_are_denied_and_open_workflow_schemas_are_refused(self):
        crd = fixture_workflow_crd()
        spec = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]
        spec["properties"]["futureOverride"] = {"type": "string"}
        policy = admission(self.delegation, crd)[0]
        self.assertIn(
            "!has(object.spec.futureOverride)", policy["spec"]["validations"][0]["expression"]
        )
        for key, value in (
            ("x-kubernetes-preserve-unknown-fields", True),
            ("additionalProperties", {}),
        ):
            changed = copy.deepcopy(crd)
            changed["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"][
                key
            ] = value
            with self.assertRaisesRegex(ValueError, "closed structural fields"):
                admission(self.delegation, changed)

    def test_release_roles_have_no_pod_secret_template_authoring_or_rbac_rights(self):
        roles = {
            item["metadata"]["name"]: item
            for item in delegated_roles(self.delegation)
            if item["kind"] == "Role" and item["metadata"]["name"].startswith("release-")
        }
        for item in roles.values():
            self.assertFalse(
                any(
                    "*" in rule["resources"]
                    or "*" in rule["verbs"]
                    or "secrets" in rule["resources"]
                    or "pods" in rule["resources"]
                    for rule in item["rules"]
                )
            )
        self.assertEqual(
            roles["release-executor"]["rules"][0]["resources"], ["workflowtaskresults"]
        )
        self.assertEqual(roles["release-approver"]["rules"][0]["verbs"], ["get", "patch"])

    def test_argocd_overrides_are_gated_and_revision_source_is_not_release_owned(self):
        _, documents = render(self.delegation, fixture_assets())
        maps = {
            item["metadata"]["name"]: item["data"]
            for item in documents
            if item["kind"] == "ConfigMap"
        }
        self.assertEqual(
            maps["argocd-cm"]["application.sync.requireOverridePrivilegeForRevisionSync"], "true"
        )
        self.assertEqual(maps["argocd-cm"]["admin.enabled"], "false")
        self.assertIn("applications, override, */*, deny", maps["argocd-rbac-cm"]["policy.csv"])
        self.assertEqual(
            maps["workflow-controller-configmap"]["workflowRestrictions"],
            "templateReferencing: Secure\n",
        )

    def test_inventory_refuses_foreign_and_incompatible_shared_apis(self):
        shared, _ = render(self.delegation, fixture_assets())
        expected = shared[0]
        for change in ("labels", "spec"):
            actual = copy.deepcopy(expected)
            if change == "labels":
                actual["metadata"]["labels"]["app.kubernetes.io/managed-by"] = "foreign"
            else:
                actual["spec"]["group"] = "foreign"
            with (
                patch.object(runtime, "read_resources", return_value={reference(expected): actual}),
                self.assertRaises(ValueError),
            ):
                runtime.inventory(self.target, [expected], [], complete=True)

    def test_secret_observation_requests_metadata_only(self):
        secret = resource("Secret", "private", self.delegation.gitops)
        with patch.object(runtime, "kube", return_value=json.dumps(secret["metadata"])) as kube:
            result = runtime.read_resources(self.target, [secret])
        self.assertIn(reference(secret), result)
        self.assertEqual(kube.call_args.args[1][-1], "jsonpath={.metadata}")
        self.assertNotIn("data", result[reference(secret)])

    def test_absent_inventory_accepts_empty_success_but_not_invalid_responses(self):
        item = resource("ConfigMap", "absent", self.delegation.controllers)
        for output in ("", "\n", '{"kind":"List","items":[]}'):
            with patch.object(runtime, "kube", return_value=output):
                self.assertEqual(runtime.read_resources(self.target, [item]), {})
        with (
            patch.object(runtime, "kube", return_value="invalid-json"),
            self.assertRaises(json.JSONDecodeError),
        ):
            runtime.read_resources(self.target, [item])
        with (
            patch.object(runtime, "kube", side_effect=ValueError("unavailable")),
            self.assertRaises(ValueError),
        ):
            runtime.read_resources(self.target, [item])

    def test_large_api_creation_has_a_bounded_schema_compilation_window(self):
        crd = resource("CustomResourceDefinition", "reviewed.example.test", None)
        config_map = resource("ConfigMap", "ordinary", self.delegation.controllers)
        with patch.object(runtime, "kube") as kube:
            runtime.write(self.target, [crd])
            self.assertEqual(kube.call_args.kwargs["request_timeout"], 120)
            runtime.write(self.target, [config_map])
            self.assertEqual(kube.call_args.kwargs["request_timeout"], 30)

    def test_apply_refuses_before_first_write_and_installs_admission_before_roles(self):
        shared, documents = render(self.delegation, fixture_assets())
        with (
            patch.object(runtime.consumer_lifecycle, "health"),
            patch.object(runtime, "namespaces_delegated"),
            patch.object(runtime, "download_assets", return_value=fixture_assets()),
            patch.object(runtime, "inventory", side_effect=ValueError("foreign")),
            patch.object(runtime, "write") as write,
        ):
            with self.assertRaises(ValueError):
                runtime.apply(self.target, self.delegation)
            write.assert_not_called()
        with (
            patch.object(runtime.consumer_lifecycle, "health"),
            patch.object(runtime, "namespaces_delegated"),
            patch.object(runtime, "download_assets", return_value=fixture_assets()),
            patch.object(
                runtime, "inventory", return_value={reference(item): item for item in shared}
            ),
            patch.object(runtime, "kube"),
            patch.object(runtime, "verify_admission"),
            patch.object(runtime, "observe", return_value={}),
            patch.object(runtime, "write") as write,
        ):
            runtime.apply(self.target, self.delegation)
        self.assertEqual(
            [item["kind"] for item in write.call_args_list[0].args[1]], ["Namespace", "Namespace"]
        )
        self.assertEqual(
            {item["kind"] for item in write.call_args_list[1].args[1]},
            {"ValidatingAdmissionPolicy"},
        )
        self.assertEqual(
            {item["kind"] for item in write.call_args_list[2].args[1]},
            {"ValidatingAdmissionPolicyBinding"},
        )
        self.assertTrue(
            any(item["kind"] == "RoleBinding" for item in write.call_args_list[3].args[1])
        )

    def test_full_profile_uses_networked_argo_render_and_delegation_preflight(self):
        target = Target(
            self.target.consumer,
            self.target.docker_context,
            self.root,
            "isolated-network-full-local",
            18443,
        )
        with (
            patch.object(runtime.consumer_lifecycle, "health"),
            patch.object(runtime, "namespaces_delegated"),
            patch.object(runtime, "download_assets", return_value={}),
            patch.object(runtime, "render", return_value=([], [])) as render,
            patch.object(runtime, "observe", return_value={}),
        ):
            runtime.health(target, self.delegation)
            self.assertTrue(render.call_args.kwargs["networking"])
        with (
            patch.object(runtime.consumer_lifecycle, "health") as health,
            patch.object(
                runtime, "namespaces_delegated", side_effect=ValueError("delegation preflight")
            ),
            patch.object(runtime, "download_assets") as download,
            self.assertRaisesRegex(ValueError, "delegation preflight"),
        ):
            runtime.apply(target, self.delegation)
        health.assert_called_once_with(target)
        download.assert_not_called()

    def test_apply_refuses_the_smaller_profile_before_cluster_access(self):
        smaller = Target(self.target.consumer, self.target.docker_context, self.root)
        with (
            patch.object(runtime.consumer_lifecycle, "health") as health,
            patch.object(runtime, "download_assets") as download,
            self.assertRaisesRegex(ValueError, "explicit isolated-argo-local"),
        ):
            runtime.apply(smaller, self.delegation)
        health.assert_not_called()
        download.assert_not_called()

    def test_incomplete_native_authorization_and_unavailable_admission_never_pass(self):
        with (
            patch.object(qualification, "kube", return_value='{"status":{}}'),
            self.assertRaises(ValueError),
        ):
            qualification.access(
                self.target, "actor", self.delegation.managed, "", "secrets", "get"
            )
        with (
            patch.object(qualification, "kube", side_effect=ValueError("unavailable")),
            self.assertRaises(ValueError),
        ):
            qualification.admission_checks(self.target, self.delegation)

    def test_native_workflow_terminal_failure_is_immediate_and_never_a_pass(self):
        for phase in ("Failed", "Error"):
            with (
                patch.object(qualification, "kube", return_value=phase),
                patch.object(qualification.time, "sleep") as sleep,
                self.assertRaisesRegex(ValueError, "retained fixture"),
            ):
                qualification.wait_workflow(self.target, self.delegation)
            sleep.assert_not_called()
        with patch.object(qualification, "kube", return_value="Succeeded"):
            qualification.wait_workflow(self.target, self.delegation)

    def test_removal_uses_server_uid_preconditions_and_preserves_shared_and_consumer_state(self):
        item = resource("ConfigMap", "owned", self.delegation.controllers, data={})
        shared = [resource("CustomResourceDefinition", "shared", None)]
        documents = [
            item,
            resource("Namespace", self.delegation.controllers, None),
            resource("Secret", "private", self.delegation.controllers),
        ]
        found = {reference(item): {**item, "metadata": {**item["metadata"], "uid": "original-uid"}}}
        with (
            patch.object(runtime.consumer_lifecycle, "owned"),
            patch.object(runtime, "namespaces_delegated"),
            patch.object(runtime, "download_assets"),
            patch.object(runtime, "render", return_value=(shared, documents)),
            patch.object(runtime, "inventory", return_value=found),
            patch.object(runtime, "kube", return_value='{"items":[]}'),
            patch.object(runtime.consumer_runtime, "command", return_value="") as delete,
        ):
            runtime.remove(self.target, self.delegation, f"{self.target.name}/argo")
        self.assertEqual(delete.call_count, 1)
        self.assertEqual(
            json.loads(delete.call_args.kwargs["input_data"])["preconditions"],
            {"uid": "original-uid"},
        )
        self.assertIn("/configmaps/owned", delete.call_args.args[1][-3])
        with (
            patch.object(runtime.consumer_lifecycle, "owned") as owned,
            self.assertRaises(ValueError),
        ):
            runtime.remove(self.target, self.delegation, "foreign/argo")
        owned.assert_not_called()
