import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
OVERLAY = REPO / "clusters/local-kind/kind-baseline/addons/argocd"
UPSTREAM = (
    "https://raw.githubusercontent.com/argoproj/argo-cd/"
    "e258ee23c3e52266d407572f4bcdfe7d9ed36cb5/manifests/install.yaml"
)
UPSTREAM_SHA256 = "9a87f2b3e14c278f12501eb0ef5c3955b27cf05370ca425381c6a908cf85a5c5"
OWNER = {"infra.k8s-infrastructure.io/owner": "registered-kind-argocd"}
CONTROLLERS = {
    ("Deployment", "argocd-applicationset-controller"),
    ("Deployment", "argocd-dex-server"),
    ("Deployment", "argocd-notifications-controller"),
    ("Deployment", "argocd-redis"),
    ("Deployment", "argocd-repo-server"),
    ("Deployment", "argocd-server"),
    ("StatefulSet", "argocd-application-controller"),
}


@unittest.skipUnless(
    os.environ.get("KIND_BASELINE_KUBECTL") and os.environ.get("KIND_BASELINE_ARGO_INSTALL"),
    "set KIND_BASELINE_KUBECTL and KIND_BASELINE_ARGO_INSTALL to staged paths",
)
class KindBaselineArgoCDTests(unittest.TestCase):
    def test_render_preserves_upstream_selectors_and_marks_all_objects_owned(self):
        kubectl = Path(os.environ["KIND_BASELINE_KUBECTL"]).resolve(strict=True)
        upstream = Path(os.environ["KIND_BASELINE_ARGO_INSTALL"]).read_bytes()
        self.assertEqual(hashlib.sha256(upstream).hexdigest(), UPSTREAM_SHA256)
        configuration = (OVERLAY / "kustomization.yaml").read_text(encoding="utf-8")
        self.assertEqual(yaml.safe_load(configuration)["resources"], ["namespace.yaml", UPSTREAM])

        with tempfile.TemporaryDirectory(prefix="kind-baseline-argocd-") as stage:
            staged = Path(stage)
            for name in ("namespace.yaml", "redis-pull-policy.yaml"):
                shutil.copyfile(OVERLAY / name, staged / name)
            (staged / "install.yaml").write_bytes(upstream)
            # Substitute only the upstream URL with the exact verified bytes for local rendering.
            (staged / "kustomization.yaml").write_text(
                configuration.replace(UPSTREAM, "install.yaml"), encoding="utf-8"
            )
            result = subprocess.run(
                [kubectl, "kustomize", staged],
                capture_output=True,
                timeout=30,
                env={"HOME": stage, "PATH": os.defpath},
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode())

        expected = {
            (doc["kind"], doc["metadata"]["name"]): doc for doc in yaml.safe_load_all(upstream)
        }
        documents = list(yaml.safe_load_all(result.stdout))
        rendered = {(doc["kind"], doc["metadata"]["name"]): doc for doc in documents}
        namespace = ("Namespace", "argocd")
        self.assertEqual(len(documents), len(rendered))
        self.assertEqual(set(rendered), set(expected) | {namespace})
        self.assertEqual(
            rendered[namespace]["metadata"]["labels"],
            {
                "app.kubernetes.io/name": "argocd",
                "app.kubernetes.io/managed-by": "k8s-infrastructure",
                "app.kubernetes.io/part-of": "kubernetes-substrate",
            }
            | OWNER,
        )
        self.assertEqual(
            {key for key in rendered if key[0] in {"Deployment", "StatefulSet"}}, CONTROLLERS
        )
        for key, doc in expected.items():
            with self.subTest(object=key):
                self.assertEqual(
                    rendered[key]["metadata"]["labels"], doc["metadata"].get("labels", {}) | OWNER
                )
                if key in CONTROLLERS:
                    self.assertEqual(rendered[key]["metadata"]["namespace"], "argocd")
                    self.assertEqual(rendered[key]["spec"]["selector"], doc["spec"]["selector"])
                    self.assertEqual(
                        rendered[key]["spec"]["template"]["metadata"]["labels"],
                        doc["spec"]["template"]["metadata"]["labels"],
                    )

        redis = rendered[("Deployment", "argocd-redis")]["spec"]["template"]["spec"]["containers"]
        self.assertEqual(len(redis), 1)
        self.assertEqual(redis[0]["name"], "redis")
        self.assertEqual(
            redis[0]["image"],
            "docker.io/library/redis@sha256:08ad0b1d280850169a790dba1393ff7a90aef951fc19632cf4d3ce4f78e679ba",
        )
        self.assertEqual(redis[0]["imagePullPolicy"], "IfNotPresent")
