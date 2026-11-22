"""Proof that the replacement CA injector can populate a new owned target."""

import base64
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.consumer_profile import Target


class MinimalDatabaseCAProbeTests(unittest.TestCase):
    def test_observes_new_injection_and_refuses_a_different_bundle(self):
        from scripts import minimal_database_ca as probe

        with tempfile.TemporaryDirectory() as directory:
            target = Target("qual-db", "default", Path(directory), "minimal-demo", 18443)
            public_ca = "public API server certificate data\n"
            presented = public_ca
            created: dict = {}

            def kube(selected, args, documents=None, **kwargs):
                self.assertEqual(selected, target)
                if args[:4] == ["-n", "cert-manager", "get", "configmap"]:
                    self.assertEqual(args[4], "kube-root-ca.crt")
                    return json.dumps({"data": {"ca.crt": public_ca}})
                if args[0] == "create":
                    assert documents is not None
                    self.assertEqual(documents[0]["kind"], "ValidatingWebhookConfiguration")
                    webhook = documents[0]["webhooks"][0]
                    self.assertEqual(webhook["rules"], [])
                    self.assertEqual(webhook["failurePolicy"], "Ignore")
                    self.assertNotIn("caBundle", webhook["clientConfig"])
                    created.clear()
                    created.update(copy.deepcopy(documents[0]))
                    created["metadata"]["uid"] = "owned-ca-probe-uid"
                    return json.dumps(created)
                self.assertEqual(args[:2], ["get", "validatingwebhookconfiguration"])
                self.assertEqual(args[2], created["metadata"]["name"])
                injected = copy.deepcopy(created)
                injected["webhooks"][0]["clientConfig"]["caBundle"] = base64.b64encode(
                    presented.encode()
                ).decode()
                return json.dumps(injected)

            with patch.object(probe, "kube", side_effect=kube):
                evidence = probe.probe_ca_injection(target)
                self.assertEqual(evidence["uid"], "owned-ca-probe-uid")
                self.assertEqual(
                    evidence["bundleSha256"], hashlib.sha256(public_ca.encode()).hexdigest()
                )
                presented = "different CA"
                with self.assertRaisesRegex(ValueError, "differs from the public API server CA"):
                    probe.probe_ca_injection(target)
