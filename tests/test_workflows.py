from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import workflows


class WorkflowsSubstrateTests(unittest.TestCase):
    @staticmethod
    def owned_namespace() -> str:
        value = workflows.validate_namespace()
        return json.dumps(value)

    def test_offline_selection_and_preview(self) -> None:
        selection = workflows.load_selection()
        self.assertEqual(selection["version"], "v4.1.5")
        self.assertEqual(len(selection["sha256"]), 64)
        workflows.validate_namespace()
        with tempfile.TemporaryDirectory() as temporary:
            plan = workflows.commands("bootstrap", state_root=Path(temporary))
        self.assertEqual(
            plan[0][-6:], ["get", "namespace", "argo-workflows", "--ignore-not-found", "-o", "json"]
        )
        self.assertEqual(plan[4][-4:], ["apply", "--server-side", "-f", "-"])
        self.assertEqual(plan[-2][-2:], ["deployment/workflow-controller", "--timeout=300s"])
        self.assertEqual(plan[-1][-2:], ["deployment/argo-server", "--timeout=300s"])
        self.assertTrue(all("kind-perfeng-local" in command for command in plan))

    def test_floating_or_foreign_release_is_rejected(self) -> None:
        for version, url in (
            (
                "main",
                "https://github.com/argoproj/argo-workflows/releases/download/main/namespace-install.yaml",
            ),
            ("v4.1.5", "https://example.test/namespace-install.yaml"),
        ):
            with self.subTest(version=version, url=url), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                folder = root / workflows.ADDON
                folder.mkdir(parents=True)
                selection = copy.deepcopy(workflows.load_selection())
                selection.update(version=version, url=url)
                (folder / "release.json").write_text(json.dumps(selection), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "official versioned manifest"):
                    workflows.load_selection(root)

    def test_namespace_version_and_owner_are_exact(self) -> None:
        expected = workflows.validate_namespace()
        workflows.validate_live_namespace(self.owned_namespace(), expected, allow_missing=False)
        for field, value in (
            ("app.kubernetes.io/managed-by", "foreign"),
            ("app.kubernetes.io/version", "v4.1.4"),
        ):
            with self.subTest(field=field):
                foreign = copy.deepcopy(expected)
                foreign["metadata"]["labels"][field] = value
                with self.assertRaisesRegex(ValueError, "unexpected owner or version"):
                    workflows.validate_live_namespace(
                        json.dumps(foreign), expected, allow_missing=False
                    )
        with self.assertRaisesRegex(ValueError, "missing"):
            workflows.validate_live_namespace("", expected, allow_missing=False)

    def test_wrong_asset_hash_stops_before_parsing_or_apply(self) -> None:
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            workflows.validate_manifest(b"untrusted", workflows.load_selection())

    def test_first_install_refuses_preexisting_workflow_crds(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch(
                "scripts.workflows.local_kind.normalize_state_root", return_value=Path(temporary)
            ),
            patch("scripts.workflows.local_kind.execute"),
            patch(
                "scripts.workflows.local_kind.run",
                side_effect=[
                    "",
                    "customresourcedefinition.apiextensions.k8s.io/workflows.argoproj.io",
                ],
            ) as run,
            patch("scripts.workflows.download_manifest", return_value=b"verified"),
        ):
            with self.assertRaisesRegex(ValueError, "already exist"):
                workflows.execute("bootstrap")
            self.assertEqual(run.call_count, 2)
            self.assertEqual(
                run.call_args_list[0].args[0][-6:],
                ["get", "namespace", "argo-workflows", "--ignore-not-found", "-o", "json"],
            )

    def test_foreign_namespace_refuses_before_download_or_apply(self) -> None:
        foreign = json.loads(self.owned_namespace())
        foreign["metadata"]["labels"]["app.kubernetes.io/managed-by"] = "foreign"
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch(
                "scripts.workflows.local_kind.normalize_state_root", return_value=Path(temporary)
            ),
            patch("scripts.workflows.local_kind.execute"),
            patch("scripts.workflows.local_kind.run", return_value=json.dumps(foreign)) as run,
            patch("scripts.workflows.download_manifest") as download,
        ):
            with self.assertRaisesRegex(ValueError, "unexpected owner or version"):
                workflows.execute("bootstrap")
            run.assert_called_once()
            download.assert_not_called()

    def test_bootstrap_applies_only_verified_bytes_and_waits_for_controllers(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch(
                "scripts.workflows.local_kind.normalize_state_root", return_value=Path(temporary)
            ),
            patch("scripts.workflows.local_kind.execute"),
            patch(
                "scripts.workflows.local_kind.run",
                side_effect=[
                    "",
                    "",
                    "",
                    "",
                    self.owned_namespace(),
                    "",
                    self.owned_namespace(),
                    "",
                    "",
                ],
            ) as run,
            patch("scripts.workflows.download_manifest", return_value=b"verified") as download,
        ):
            workflows.execute("bootstrap")
            download.assert_called_once_with(workflows.load_selection())
            self.assertEqual(run.call_count, 9)
            self.assertEqual(run.call_args_list[5].kwargs["input_data"], b"verified")
            self.assertEqual(
                run.call_args_list[-2].args[0][-2:],
                ["deployment/workflow-controller", "--timeout=300s"],
            )
            self.assertEqual(
                run.call_args_list[-1].args[0][-2:], ["deployment/argo-server", "--timeout=300s"]
            )
            self.assertTrue(all("delete" not in call.args[0] for call in run.call_args_list))

    def test_health_is_read_only_and_requires_owned_namespace(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch(
                "scripts.workflows.local_kind.normalize_state_root", return_value=Path(temporary)
            ),
            patch("scripts.workflows.local_kind.execute"),
            patch(
                "scripts.workflows.local_kind.run", side_effect=[self.owned_namespace(), "", ""]
            ) as run,
            patch("scripts.workflows.download_manifest") as download,
        ):
            workflows.execute("health")
            self.assertEqual(run.call_count, 3)
            self.assertTrue(all("apply" not in call.args[0] for call in run.call_args_list))
            download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
