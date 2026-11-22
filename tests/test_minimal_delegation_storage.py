"""Filesystem contract and binding checks without a live cluster."""

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


class FilesystemProbeTests(unittest.TestCase):
    def test_competing_writer_fails_then_replacement_reads_durable_state(self):
        from scripts.minimal_delegation_storage_worker import exercise

        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            writer = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    (
                        "from pathlib import Path; from scripts.minimal_delegation_storage_worker import "
                        "exercise; import sys; exercise(Path(sys.argv[1]), 'writer', 'neutral')"
                    ),
                    name,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            try:
                deadline = time.monotonic() + 10
                while not (root / "qualification" / "ready").is_file():
                    if writer.poll() is not None or time.monotonic() >= deadline:
                        self.fail("Neutral writer did not acquire both locks")
                    time.sleep(0.02)
                exercise(root, "contender", "neutral")
                exercise(root, "release", "neutral")
                self.assertEqual(writer.wait(timeout=10), 0)
                exercise(root, "replacement", "neutral")
                with self.assertRaisesRegex(ValueError, "persisted"):
                    exercise(root, "replacement", "wrong")
            finally:
                if writer.poll() is None:
                    writer.kill()
                writer.communicate(timeout=10)

    def test_foreign_worker_affinity_cannot_qualify_retained_binding(self):
        from scripts.minimal_delegation_storage import validate_binding

        pod = {"metadata": {"uid": "writer"}, "spec": {"nodeName": "qual-demo-local-worker"}}
        claim = {
            "metadata": {"name": "probe", "namespace": "qual-demo-apps", "uid": "claim"},
            "spec": {
                "volumeName": "new-pv",
                "storageClassName": "local-retained",
                "accessModes": ["ReadWriteOnce"],
            },
            "status": {"phase": "Bound"},
        }
        volume = {
            "metadata": {"name": "new-pv", "uid": "volume"},
            "spec": {
                "storageClassName": "local-retained",
                "persistentVolumeReclaimPolicy": "Retain",
                "accessModes": ["ReadWriteOnce"],
                "hostPath": {"path": "/var/local-path-provisioner/fixture"},
                "capacity": {"storage": "1Gi"},
                "claimRef": {"namespace": "qual-demo-apps", "name": "probe", "uid": "claim"},
                "nodeAffinity": {
                    "required": {
                        "nodeSelectorTerms": [
                            {
                                "matchExpressions": [
                                    {
                                        "key": "kubernetes.io/hostname",
                                        "operator": "In",
                                        "values": ["foreign-worker"],
                                    }
                                ]
                            }
                        ]
                    }
                },
            },
            "status": {"phase": "Bound"},
        }
        with self.assertRaisesRegex(ValueError, "Retain identity or worker placement"):
            validate_binding(claim, volume, pod, "claim", "qual-demo-local-worker")
        expression = volume["spec"]["nodeAffinity"]["required"]["nodeSelectorTerms"][0][
            "matchExpressions"
        ][0]
        expression["values"] = ["qual-demo-local-worker"]
        self.assertEqual(
            validate_binding(claim, volume, pod, "claim", "qual-demo-local-worker")["volumeUID"],
            "volume",
        )
