"""Minimal-demo Argo render contracts at the provider manifest boundary."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from test_delegated_argo import fixture_assets

from scripts import minimal_argo_fence
from scripts.consumer_profile import MINIMAL_PROFILE, Target
from scripts.delegated_argo_config import Delegation
from scripts.delegated_argo_render import render
from scripts.local_kind import CommandFailure

IMAGES = {
    "argocd-dex-server": ("dex", "ghcr.io/dexidp/dex:v2.45.1", "copyutil"),
    "argocd-redis": ("redis", "public.ecr.aws/docker/library/redis:8.2.3-alpine", "secret-init"),
    "argocd-repo-server": ("argocd-repo-server", "quay.io/argoproj/argocd:v3.5.2", "copyutil"),
    "argo-server": ("argo-server", "quay.io/argoproj/argocli:v4.1.5", None),
    "workflow-controller": (
        "workflow-controller",
        "quay.io/argoproj/workflow-controller:v4.1.5",
        None,
    ),
}


def reviewed_fixture() -> dict:
    assets = fixture_assets()
    for group in ("argocd", "workflows"):
        for obj in assets[group]:
            if obj["kind"] not in {"Deployment", "StatefulSet"}:
                continue
            name = obj["metadata"]["name"]
            container, image, init = IMAGES.get(
                name, (name, "quay.io/argoproj/argocd:v3.5.2", None)
            )
            spec = obj["spec"]["template"]["spec"]
            spec["containers"] = [{"name": container, "image": image}]
            if init:
                spec["initContainers"] = [{"name": init, "image": "quay.io/argoproj/argocd:v3.5.2"}]
    return assets


class MinimalArgoAssetTests(unittest.TestCase):
    def test_minimal_render_pins_and_bounds_the_exact_controllers_and_leaves_legacy_alone(self):
        from scripts.minimal_argo_assets import controller_documents

        delegation = Delegation("qual-argo", ("qual-argo-apps",), ("release",))
        assets = reviewed_fixture()
        shared, legacy = render(delegation, assets, networking=True)
        minimal_shared, rendered = render(delegation, assets, networking=True, minimal=True)
        self.assertEqual(minimal_shared, shared)
        self.assertTrue(any(obj["metadata"]["name"] == "delegated-argocd" for obj in legacy))
        self.assertFalse(any(obj["metadata"]["name"] == "delegated-argocd" for obj in rendered))
        controllers = controller_documents(delegation, rendered)
        self.assertEqual(len(controllers), 9)
        self.assertEqual(controllers[0]["metadata"]["name"], "argocd-redis")
        self.assertEqual(controllers[-1]["metadata"]["name"], "argocd-application-controller")
        for controller in controllers:
            spec = controller["spec"]
            pod = spec["template"]["spec"]
            self.assertEqual(spec["replicas"], 1)
            self.assertEqual(pod["nodeSelector"]["k8s-infrastructure.io/role"], "worker")
            if controller["kind"] == "Deployment":
                self.assertEqual(spec["strategy"], {"type": "Recreate"})
            for item in [*pod["containers"], *pod.get("initContainers", [])]:
                self.assertRegex(item["image"], r"@sha256:[0-9a-f]{64}$")
                self.assertEqual(item["imagePullPolicy"], "Never")
            for item in pod.get("initContainers", []):
                self.assertEqual(item["resources"]["requests"]["memory"], "384Mi")
                self.assertEqual(item["resources"]["limits"]["memory"], "384Mi")
        old_redis = next(
            obj
            for obj in legacy
            if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "argocd-redis"
        )
        self.assertEqual(
            old_redis["spec"]["template"]["spec"]["initContainers"][0]["resources"]["requests"][
                "memory"
            ],
            "64Mi",
        )

    def test_minimal_render_refuses_unreviewed_initializer_and_extra_workloads(self):
        delegation = Delegation("qual-argo", ("qual-argo-apps",), ("release",))
        for change in ("initializer", "workload"):
            assets = reviewed_fixture()
            redis = next(
                obj for obj in assets["argocd"] if obj["metadata"]["name"] == "argocd-redis"
            )
            if change == "initializer":
                redis["spec"]["template"]["spec"]["initContainers"][0]["name"] = "unreviewed"
            else:
                assets["argocd"].append({**redis, "metadata": {"name": "foreign"}})
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "Argo"):
                render(delegation, assets, minimal=True)

    def test_workflow_fence_is_closed_at_install_and_matches_only_its_namespace(self):
        delegation = Delegation("qual-argo", ("qual-argo-apps",), ("release",))
        _, docs = render(delegation, reviewed_fixture(), minimal=True)
        gate = next(item for item in docs if item["metadata"]["name"] == "argo-lifecycle-fence")
        self.assertEqual(gate["metadata"]["namespace"], delegation.controllers)
        self.assertEqual(gate["data"], {"mode": "closed"})
        policy = next(
            item for item in docs if item["metadata"]["name"] == "qual-argo-argo-lifecycle-fence"
        )
        scope = policy["spec"]["matchConstraints"]
        self.assertEqual(
            scope["namespaceSelector"],
            {"matchLabels": {"kubernetes.io/metadata.name": delegation.managed}},
        )
        self.assertEqual(scope["resourceRules"][0]["operations"], ["CREATE"])

    def test_gitops_fence_limits_writes_to_top_level_argo_intent_in_its_namespace(self):
        delegation = Delegation("qual-argo", ("qual-argo-apps",), ("release",))
        documents = minimal_argo_fence.documents_for(delegation)
        policy = next(
            item
            for item in documents
            if item["kind"] == "ValidatingAdmissionPolicy"
            and item["metadata"]["name"] == "qual-argo-argo-restore-gitops"
        )
        scope = policy["spec"]["matchConstraints"]
        self.assertEqual(
            {"kubernetes.io/metadata.name": delegation.gitops},
            scope["namespaceSelector"]["matchLabels"],
        )
        self.assertEqual(
            [
                {
                    "apiGroups": ["argoproj.io"],
                    "apiVersions": ["v1alpha1"],
                    "operations": ["CREATE", "UPDATE", "DELETE"],
                    "resources": ["applications", "applicationsets"],
                }
            ],
            scope["resourceRules"],
        )
        denied = CommandFailure(
            "private details withheld", denial=("admission", "qual-argo-argo-restore-gitops")
        )
        with TemporaryDirectory() as directory:
            target = Target("qual-argo", "default", Path(directory), MINIMAL_PROFILE, 18443)
            with patch.object(minimal_argo_fence, "kube", side_effect=[denied, denied]) as kube:
                minimal_argo_fence.confirm_gitops_frozen(target, delegation)
        probes = [call.args[2][0] for call in kube.call_args_list]
        self.assertEqual(["Application", "ApplicationSet"], [item["kind"] for item in probes])
        for call in kube.call_args_list:
            self.assertIn("--dry-run=server", call.args[1])
            self.assertEqual(delegation.gitops, call.args[2][0]["metadata"]["namespace"])

    def test_fence_patch_checks_exact_identity_and_observes_the_native_decision(self):
        delegation = Delegation("qual-argo", ("qual-argo-apps",), ("release",))
        with TemporaryDirectory() as directory:
            target = Target("qual-argo", "default", Path(directory), MINIMAL_PROFILE, 18443)
            controller = {"uid": "fence-id", "resourceVersion": "57"}
            before = {"metadata": controller, "data": {"mode": "open"}}
            after = {
                "metadata": {**controller, "resourceVersion": "58"},
                "data": {"mode": "closed"},
            }
            denied = CommandFailure(
                "private details withheld", denial=("admission", "qual-argo-argo-lifecycle-fence")
            )
            with (
                patch.object(minimal_argo_fence, "read_fence", side_effect=[before, after]),
                patch.object(minimal_argo_fence, "kube", side_effect=["", denied]) as kube,
            ):
                minimal_argo_fence.set_closed(target, delegation, True, expected_uid="fence-id")
            commands = [call.args[1] for call in kube.call_args_list]
            self.assertEqual(
                json.loads(commands[0][-1])[:3],
                [
                    {"op": "test", "path": "/metadata/uid", "value": "fence-id"},
                    {"op": "test", "path": "/metadata/resourceVersion", "value": "57"},
                    {"op": "test", "path": "/data/mode", "value": "open"},
                ],
            )
            self.assertIn("--dry-run=server", commands[1])
            self.assertIn(
                "--as=system:serviceaccount:qual-argo-releases:release-releaser", commands[1]
            )
            with (
                patch.object(minimal_argo_fence, "read_fence", return_value=before),
                patch.object(minimal_argo_fence, "kube") as refused,
                self.assertRaisesRegex(ValueError, "UID differs"),
            ):
                minimal_argo_fence.set_closed(target, delegation, True, expected_uid="stale-id")
            refused.assert_not_called()
            with (
                patch.object(
                    minimal_argo_fence,
                    "kube",
                    side_effect=CommandFailure(
                        "other control", denial=("authorization", "forbidden")
                    ),
                ),
                self.assertRaises(CommandFailure),
            ):
                minimal_argo_fence.confirm_admission(target, delegation, True)
