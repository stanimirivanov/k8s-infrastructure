"""Exercise minimal-demo lifecycle operations through the explicit command boundary."""

import contextlib
import io
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from scripts import minimal_argo


class MinimalArgoTests(unittest.TestCase):
    def test_stale_installation_identity_blocks_any_cluster_resource_access(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            root = Path(directory)
            (root / ".local").mkdir(mode=0o700)
            delegation = root / "delegation.json"
            delegation.write_text(
                json.dumps(
                    {
                        "consumer": "qual-argo",
                        "workloadNamespaces": ["qual-argo-apps"],
                        "approvedTemplates": ["qual-template"],
                    }
                )
            )
            receipt = root / ".local/qual-argo-local.argo-lifecycle.json"
            receipt.write_text(
                json.dumps({"schema": "minimal-argo-lifecycle.v1", "incarnation": "stale"})
            )
            receipt.chmod(0o600)
            stack.enter_context(
                patch("scripts.minimal_argo.bootstrap.network_operation", contextlib.nullcontext)
            )
            stack.enter_context(
                patch(
                    "scripts.minimal_argo.consumer_lifecycle.owned",
                    return_value={
                        "phase": "provisioned",
                        "incarnation": "current",
                        "nonce": "owner",
                    },
                )
            )
            command = stack.enter_context(patch("scripts.minimal_argo.kube"))
            output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            self.assertEqual(
                1,
                minimal_argo.main(
                    [
                        "suspend",
                        "--consumer",
                        "qual-argo",
                        "--docker-context",
                        "default",
                        "--state-root",
                        directory,
                        "--delegation",
                        str(delegation),
                        "--confirm-target",
                        "qual-argo-local",
                        "--execute",
                    ]
                ),
            )
            self.assertIn("identity", output.getvalue())
            self.assertIn("stale", receipt.read_text())
            command.assert_not_called()

    def test_resumes_suspension_after_partial_scale_down_and_observes_pod_disappearance(self):
        with LifecycleFixture() as fixture:
            fixture.started()
            fixture.fail_scale = "argocd-repo-server"
            with self.assertRaisesRegex(ValueError, "simulated command failure"):
                fixture.invoke("suspend")
            self.assertEqual("suspending", fixture.read()["phase"])
            self.assertEqual(0, fixture.workloads["argocd-server"]["spec"]["replicas"])
            self.assertEqual(1, fixture.workloads["argocd-redis"]["spec"]["replicas"])
            original = fixture.read()["restoreReplicas"]
            fixture.fail_scale = None
            result = fixture.invoke("suspend")
            self.assertEqual("suspended", result["phase"])
            self.assertEqual(original, fixture.read()["restoreReplicas"])
            self.assertTrue(all(state["pods"] == [] for state in result["controllers"].values()))
            self.assertEqual({name: 1 for name in CONTROLLERS}, original)

    def test_blocked_workflows_and_nonterminal_pods_never_claim_suspension(self):
        with LifecycleFixture() as fixture:
            fixture.started()
            fixture.workflows = [{"metadata": {"name": "queued"}}]
            with self.assertRaisesRegex(ValueError, "Nonterminal Workflows"):
                fixture.invoke("suspend")
            self.assertEqual([], fixture.patches)
            fixture.workflows = [{"status": {"phase": "Succeeded"}}]
            fixture.keep_terminating = True
            fixture.stop_on_wait()
            with self.assertRaisesRegex(ValueError, "simulated deadline"):
                fixture.invoke("suspend")
            self.assertEqual("suspending", fixture.read()["phase"])
            self.assertTrue(all(replicas == 0 for _, replicas in fixture.patches))

    def test_resume_recovers_existing_start_without_a_second_patch_or_unsafe_desired_state(self):
        with LifecycleFixture() as fixture:
            fixture.started()
            fixture.invoke("suspend")
            fixture.applications = [{"spec": {"syncPolicy": {"automated": {}}}}]
            before = list(fixture.patches)
            with self.assertRaisesRegex(ValueError, "automated sync"):
                fixture.invoke("resume")
            self.assertEqual(before, fixture.patches)
            fixture.applications = []
            fixture.keep_pending = True
            wait = fixture.stop_on_wait()
            with self.assertRaisesRegex(ValueError, "simulated deadline"):
                fixture.invoke("resume")
            self.assertEqual("resuming", fixture.read()["phase"])
            first_start = [name for name, replica in fixture.patches if replica == 1]
            self.assertEqual(1, len(first_start))
            wait.side_effect = None
            fixture.keep_pending = False
            result = fixture.invoke("resume")
            self.assertEqual("running", result["phase"])
            self.assertCountEqual(CONTROLLERS, [name for name, n in fixture.patches if n == 1])
            previous = fixture.read()["history"]["previousPods"]
            for name, result_entry in result["controllers"].items():
                self.assertTrue(result_entry["observed"])
                self.assertEqual(1, len(result_entry["pods"]))
                self.assertNotEqual(previous[name], result_entry["pods"])

    def test_resume_blocks_unaccounted_or_over_budget_worker_before_scaling(self):
        with LifecycleFixture() as fixture:
            fixture.started()
            fixture.invoke("suspend")
            pod = fixture.pod(fixture.workloads["argocd-server"])
            pod["metadata"] = {
                "name": "resident-extra",
                "namespace": "unrelated",
                "uid": "resident",
            }
            pod["spec"]["containers"] = [
                {
                    "name": "existing",
                    "resources": {
                        "requests": {"cpu": "1730m", "memory": "6000Mi"},
                        "limits": {"cpu": "1750m", "memory": "7000Mi"},
                    },
                }
            ]
            fixture.worker_pods = [pod]
            before = list(fixture.patches)
            with self.assertRaisesRegex(ValueError, "worker envelope"):
                fixture.invoke("resume")
            self.assertEqual(before, fixture.patches)

    def test_admitted_sidecar_blocks_restoration_before_any_scale_up(self):
        with LifecycleFixture() as fixture:
            fixture.started()
            fixture.invoke("suspend")
            fixture.mutate_admitted_pod = True
            before = list(fixture.patches)
            with self.assertRaisesRegex(ValueError, "finite worker startup"):
                fixture.invoke("resume")
            self.assertEqual(before, fixture.patches)

    def test_qualification_refuses_another_incarnation_and_binds_complete_cycle_evidence(self):
        with LifecycleFixture() as fixture:
            fixture.started()
            fixture.invoke("suspend")
            fixture.invoke("resume")
            history = fixture.read()["history"]
            provenance = {
                "incarnation": fixture.owner["incarnation"],
                "artifactDigest": fixture.read()["sourceArtifactDigest"],
            }
            with (
                patch(
                    "scripts.minimal_argo.resource_peaks.bind",
                    return_value=SimpleNamespace(provenance=provenance),
                ),
                patch("scripts.minimal_argo.check_failure_events"),
            ):
                peak = fixture.peak_receipt(history, {**provenance, "incarnation": "foreign"})
                with self.assertRaisesRegex(ValueError, "peak receipt provenance"):
                    fixture.invoke("qualify", request={}, host_id="host", peak_receipt=peak)
                peak = fixture.peak_receipt(history, provenance)
                result = fixture.invoke("qualify", request={}, host_id="host", peak_receipt=peak)
            self.assertEqual("qualified", result["phase"])
            self.assertTrue((fixture.target.directory / result["qualificationEvidence"]).is_file())
            self.assertFalse(result["installedAcceptance"])

    def test_dormant_install_records_prior_intent_before_restoration(self):
        with LifecycleFixture() as fixture:
            fixture.started()
            documents = list(fixture.workloads.values())
            fixture.workloads = {}
            fixture.receipt.unlink()
            fixture.fail_create = "argocd-applicationset-controller"
            with fixture.install_assets(documents):
                with self.assertRaisesRegex(ValueError, "simulated install interruption"):
                    fixture.invoke("install")
                self.assertEqual("installing", fixture.read()["phase"])
                self.assertTrue(
                    all(item["spec"]["replicas"] == 0 for item in fixture.workloads.values())
                )
                fixture.fail_create = None
                result = fixture.invoke("install")
            self.assertEqual("suspended", result["phase"])
            self.assertEqual({name: 1 for name in CONTROLLERS}, fixture.read()["restoreReplicas"])
            self.assertEqual([], fixture.patches)
            self.assertTrue(fixture.fences_closed)
            self.assertCountEqual(CONTROLLERS, fixture.workloads)


CONTROLLERS = {
    "argocd-server": "Deployment",
    "argocd-repo-server": "Deployment",
    "argocd-redis": "Deployment",
    "argocd-notifications-controller": "Deployment",
    "argocd-dex-server": "Deployment",
    "argocd-applicationset-controller": "Deployment",
    "workflow-controller": "Deployment",
    "argo-server": "Deployment",
    "argocd-application-controller": "StatefulSet",
}


class LifecycleFixture(contextlib.ExitStack):
    """A narrow Kubernetes JSON/patch adapter with independent controller state."""

    def __init__(self):
        super().__init__()
        from scripts.consumer_profile import Target
        from scripts.delegated_argo_config import Delegation

        root = Path(self.enter_context(tempfile.TemporaryDirectory()))
        (root / ".local").mkdir(mode=0o700)
        self.target = Target("qual-argo", "default", root, "minimal-demo")
        self.delegation = Delegation("qual-argo", ("qual-argo-apps",), ("qual-template",))
        self.workloads = {}
        self.owner = {"phase": "provisioned", "incarnation": "cluster-id", "nonce": "owner"}
        self.fail_scale = None
        self.fail_create = None
        self.mutate_admitted_pod = False
        self.keep_terminating = False
        self.keep_pending = False
        self.patches = []
        self.workflows = []
        self.applications = []
        self.application_sets = []
        self.fences_closed = False
        self.enter_context(
            patch("scripts.minimal_argo.minimal_argo_fence.set_closed", side_effect=self.fence)
        )
        self.worker_pods = []
        self.receipt = root / ".local/qual-argo-local.argo-lifecycle.json"
        self.enter_context(
            patch("scripts.minimal_argo.bootstrap.network_operation", contextlib.nullcontext)
        )
        self.enter_context(
            patch("scripts.minimal_argo.consumer_lifecycle.owned", return_value=self.owner)
        )
        self.enter_context(patch("scripts.minimal_argo.kube", side_effect=self.command))
        self.sleep = self.enter_context(patch("scripts.minimal_argo.time.sleep"))
        self.enter_context(
            patch(
                "scripts.minimal_argo.runtime.read_readiness",
                return_value=[
                    {
                        "role": "worker",
                        "schedulerEnvelope": {"cpuMillicores": 1750, "memoryBytes": 7580 * 1024**2},
                    },
                ],
            )
        )
        self.enter_context(
            patch(
                "scripts.minimal_argo.runtime.node_identity",
                return_value={
                    f"{self.target.name}-worker": {
                        "name": f"{self.target.name}-worker",
                        "role": "worker",
                        "id": "node",
                    },
                },
            )
        )
        self.enter_context(patch("scripts.minimal_argo.runtime.verify_nodes"))
        self.enter_context(patch("scripts.minimal_argo.staged_images"))

    def install_assets(self, documents):
        from scripts.kubernetes import reference

        stack = contextlib.ExitStack()
        mocks: tuple[tuple[str, dict[str, Any]], ...] = (
            ("delegated_argo_config.download_assets", {"return_value": {}}),
            ("render", {"return_value": ([], documents)}),
            ("delegated_argo_runtime.namespaces_delegated", {}),
            ("delegated_argo_runtime.verify_admission", {}),
            (
                "delegated_argo_runtime.inventory",
                {
                    "side_effect": lambda target, shared, docs, **kw: {
                        reference(item): item for item in self.workloads.values()
                    }
                },
            ),
            ("minimal_argo_fence.read_fence", {"return_value": {"metadata": {"uid": "fence-uid"}}}),
        )
        for name, kwargs in mocks:
            stack.enter_context(patch("scripts.minimal_argo." + name, **kwargs))
        return stack

    def fence(self, target, delegation, closed, *, expected_uid):
        if expected_uid != "fence-uid":
            raise AssertionError("Wrong fence identity")
        self.fences_closed = closed

    def stop_on_wait(self):
        self.sleep.side_effect = ValueError("simulated deadline")
        return self.sleep

    def read(self):
        return json.loads(self.receipt.read_text())

    def started(self):
        from scripts.delegated_argo_config import labels
        from scripts.host_admission import digest

        records = {}
        for name, kind in CONTROLLERS.items():
            namespace = (
                self.delegation.controllers
                if name in {"workflow-controller", "argo-server"}
                else self.delegation.gitops
            )
            template = {
                "spec": {
                    "containers": [
                        {
                            "name": name,
                            "resources": {
                                "requests": {"cpu": "50m", "memory": "64Mi"},
                                "limits": {"cpu": "500m", "memory": "384Mi"},
                            },
                        }
                    ],
                    "nodeSelector": {
                        "k8s-infrastructure.io/consumer": self.target.consumer,
                        "k8s-infrastructure.io/role": "worker",
                    },
                }
            }
            self.workloads[name] = {
                "apiVersion": "apps/v1",
                "kind": kind,
                "metadata": {
                    "name": name,
                    "namespace": namespace,
                    "uid": f"{name}-uid",
                    "resourceVersion": "1",
                    "generation": 1,
                    "annotations": {minimal_argo.INSTALLATION: "installation"},
                    "labels": labels(self.delegation),
                },
                "spec": {"replicas": 1, "template": template},
                "status": {"observedGeneration": 1, "replicas": 1, "readyReplicas": 1},
            }
            records[name] = {
                "kind": kind,
                "namespace": namespace,
                "uid": f"{name}-uid",
                "specDigest": digest({"template": template}),
            }
        value = {
            **minimal_argo.binding(self.target, self.delegation, self.owner),
            "phase": "running",
            "controllers": records,
            "restoreReplicas": {name: 1 for name in CONTROLLERS},
            "history": {},
            "installation": "installation",
            "fenceUID": "fence-uid",
        }
        self.receipt.write_text(json.dumps(value))
        self.receipt.chmod(0o600)

    def invoke(self, action, **kwargs):
        return minimal_argo.operate(self.target, self.delegation, action, 20, **kwargs)

    def peak_receipt(self, history, provenance):
        record_id = str(uuid.uuid4())
        start = history["suspendStartedAtUnixNs"] - 10_000
        end = history["resumeCompletedAtUnixNs"] - start + 10_000
        node_ids = {
            self.target.name + "-worker": {"cgroupIdentity": "worker-metric"},
            self.target.name + "-control-plane": {"cgroupIdentity": "control-metric"},
        }
        before = []
        after = []
        attempts = {}
        for name in CONTROLLERS:
            namespace = self.workloads[name]["metadata"]["namespace"]
            base = {
                "namespace": namespace,
                "node": self.target.name + "-worker",
                "containerName": name,
                "kind": "app",
                "state": "running",
                "counterState": "observed",
                "restartCount": 0,
            }
            old = history["previousPods"][name][0]
            new = history["restoredPods"][name][0]
            before.append({**base, "podUid": old, "containerId": name + "-old"})
            after.append({**base, "podUid": new, "containerId": name + "-new"})
            attempts[name + "-new"] = {
                **base,
                "podUid": new,
                "state": "observed",
                "lifetimeMemoryPeakBytes": 1000,
                "startedAtNs": history["resumeStartedAtUnixNs"],
            }
        common = {
            "schema": "resource-peaks.v1",
            "collectionId": record_id,
            "target": self.target.name,
            "profile": self.target.profile,
            "installedAcceptance": False,
        }
        samples = [
            {
                "record": "sample",
                "index": 0,
                "startedElapsedNs": 0,
                "finishedElapsedNs": 1,
                "containers": before,
            },
            {
                "record": "sample",
                "index": 1,
                "startedElapsedNs": end - 1,
                "finishedElapsedNs": end,
                "containers": after,
            },
        ]
        peak = {
            "record": "result",
            "sampleCount": 2,
            "windowEndElapsedNs": end,
            "state": "incomplete",
            "nodes": {
                node: {
                    "state": "observed",
                    "cgroupIdentity": metric["cgroupIdentity"],
                    "lifetimeMemoryPeakBytes": 1000,
                }
                for node, metric in node_ids.items()
            },
            "tooling": {"tooling": {"state": "observed"}},
            "collectorToolingCharge": {
                "tooling": {
                    "state": "observed",
                    "lifetimeMemoryPeakBytes": 1000,
                    "memoryLimitBytes": 10000,
                }
            },
            "containers": attempts,
            "sampledPressure": {"memoryFloorBreached": False, "diskFloorBreachedFilesystemIds": []},
            "runtimeAttribution": {"state": "incomplete"},
        }
        records = [
            {"record": "start"},
            {"record": "window", **provenance, "windowStartUnixNs": start, "nodes": node_ids},
            *samples,
            peak,
        ]
        path = self.target.directory / ("peak-" + record_id + ".jsonl")
        path.write_text("".join(json.dumps({**common, **item}) + "\n" for item in records))
        path.chmod(0o600)
        return path

    def pod(self, item):
        name = item["metadata"]["name"]
        kind = item["kind"]
        return {
            "metadata": {
                "name": name + "-pod",
                "namespace": item["metadata"]["namespace"],
                "uid": f"{name}-pod-{item['metadata']['generation']}",
                "ownerReferences": [
                    {
                        "kind": kind if kind == "StatefulSet" else "ReplicaSet",
                        "controller": True,
                        "uid": name + ("-uid" if kind == "StatefulSet" else "-rs-uid"),
                        "name": name,
                    }
                ],
                **({"deletionTimestamp": "now"} if not item["spec"]["replicas"] else {}),
            },
            "spec": {**item["spec"]["template"]["spec"], "nodeName": self.target.name + "-worker"},
            "status": {
                "phase": "Pending" if self.keep_pending else "Running",
                "conditions": [] if self.keep_pending else [{"type": "Ready", "status": "True"}],
                "containerStatuses": [
                    {"name": name, "ready": True, "restartCount": 0, "state": {"running": {}}}
                ],
            },
        }

    def command(self, target, args, *unused, **kwargs):
        if "--dry-run=server" in args:
            import copy

            pod = copy.deepcopy(unused[0][0])
            if self.mutate_admitted_pod:
                extra = copy.deepcopy(pod["spec"]["containers"][0])
                extra["name"] = "unreviewed-sidecar"
                pod["spec"]["containers"].append(extra)
            return json.dumps(pod)
        if args[0] == "create":
            for desired in unused[0]:
                import copy

                item = copy.deepcopy(desired)
                name = item["metadata"]["name"]
                if name == self.fail_create:
                    raise ValueError("simulated install interruption")
                item["status"] = {"observedGeneration": item["metadata"]["generation"]}
                self.workloads[name] = item
            return ""
        if args[0] == "get" and args[1] == "pods":
            return json.dumps(
                {
                    "kind": "List",
                    "items": [
                        *self.worker_pods,
                        *[
                            self.pod(item)
                            for item in self.workloads.values()
                            if item["spec"]["replicas"]
                        ],
                    ],
                }
            )
        namespace, operation, resource = args[1:4]
        selected = [
            item for item in self.workloads.values() if item["metadata"]["namespace"] == namespace
        ]
        if operation == "patch":
            name = resource.split("/")[1]
            if name == self.fail_scale:
                raise ValueError("simulated command failure")
            item = self.workloads[name]
            data = json.loads(args[-1])
            for op in data[:-1]:
                _, key, field = op["path"].split("/")
                if item[key][field] != op["value"]:
                    raise AssertionError("Invalid CAS")
            replicas = data[-1]["value"]
            self.patches.append((name, replicas))
            item["spec"]["replicas"] = replicas
            item["metadata"]["generation"] += 1
            item["metadata"]["resourceVersion"] = str(item["metadata"]["generation"])
            item["status"] = {
                "observedGeneration": item["metadata"]["generation"],
                "replicas": replicas,
                "readyReplicas": replicas,
            }
            return json.dumps(item)
        if resource.startswith(("deployments", "statefulsets")):
            items = selected
        elif resource == "replicasets":
            items = [
                {
                    "metadata": {
                        "uid": item["metadata"]["name"] + "-rs-uid",
                        "ownerReferences": [
                            {
                                "controller": True,
                                "kind": "Deployment",
                                "uid": item["metadata"]["uid"],
                                "name": item["metadata"]["name"],
                            }
                        ],
                    }
                }
                for item in selected
                if item["kind"] == "Deployment"
            ]
        elif resource == "pods":
            items = [
                self.pod(item)
                for item in selected
                if item["spec"]["replicas"] or self.keep_terminating
            ]
        elif resource == "workflows.argoproj.io":
            items = self.workflows
        elif resource == "applications.argoproj.io":
            items = self.applications
        elif resource == "applicationsets.argoproj.io":
            items = self.application_sets
        else:
            raise AssertionError(args)
        return json.dumps({"kind": "List", "items": items})
