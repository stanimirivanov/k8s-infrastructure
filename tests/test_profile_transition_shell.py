"""Native Linux guard and interrupted-write controls; no Docker or cluster needed."""

import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts import profile_transition_private as private

OPERATION = "a" * 32


@unittest.skipUnless(sys.platform == "linux", "Kind node scripts are Linux; exercised by Linux CI")
class TransitionShellTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.current = self.root / "config.yaml"
        self.old = b'{"old":"private predecessor"}\n'
        self.new = b'{"new":"private successor"}\n'
        self.old_hash = hashlib.sha256(self.old).hexdigest()
        self.new_hash = hashlib.sha256(self.new).hexdigest()
        self.current.write_bytes(self.old)
        self.backup = self.root / f"config.profile-transition-{OPERATION}.before.yaml"
        self.pending = self.root / f"config.profile-transition-{OPERATION}.next"
        self.repair = self.root / "config.before-allocatable.yaml"
        self.repair.write_bytes(b"original repair backup")

    def command(self, script, *args, data=None, operation=OPERATION):
        return subprocess.run(
            [
                "sh",
                "-ceu",
                script.replace("/var/lib/kubelet", str(self.root)),
                "transition",
                operation,
                *args,
            ],
            input=data,
            capture_output=True,
            timeout=5,
        )

    def backup_original(self):
        self.assertEqual(self.command(private.BACKUP, self.old_hash, "apply").returncode, 0)

    def write_next(self, mode="apply", old_hash=None):
        return self.command(
            private.WRITE, old_hash or self.old_hash, self.new_hash, mode, data=self.new
        )

    def test_versioned_backup_atomic_write_and_original_repair_preservation(self):
        self.assertEqual(self.command(private.PREFLIGHT, self.old_hash).returncode, 0)
        self.backup_original()
        self.assertEqual(self.write_next().returncode, 0)
        self.assertEqual(self.backup.read_bytes(), self.old)
        self.assertEqual(self.current.read_bytes(), self.new)
        self.assertEqual(self.repair.read_bytes(), b"original repair backup")
        self.assertEqual(self.current.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.backup.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.pending.exists())
        self.assertEqual(self.write_next("recover").returncode, 0)

    def test_existing_or_corrupt_backup_is_never_overwritten(self):
        self.backup_original()
        self.assertNotEqual(self.command(private.BACKUP, self.old_hash, "apply").returncode, 0)
        self.assertEqual(self.command(private.BACKUP, self.old_hash, "recover").returncode, 0)
        self.backup.write_bytes(b"interrupted backup")
        self.assertNotEqual(self.command(private.BACKUP, self.old_hash, "recover").returncode, 0)
        self.assertNotEqual(self.write_next("recover").returncode, 0)
        self.assertEqual(self.backup.read_bytes(), b"interrupted backup")
        self.assertEqual(self.current.read_bytes(), self.old)

    def test_complete_staged_write_can_recover_but_partial_or_foreign_bytes_cannot(self):
        self.backup_original()
        for data in (b"partial", b"foreign"):
            self.pending.write_bytes(data)
            self.assertNotEqual(self.write_next("recover").returncode, 0)
            self.assertEqual(self.pending.read_bytes(), data)
            self.assertEqual(self.current.read_bytes(), self.old)
        self.pending.write_bytes(self.new)
        self.assertNotEqual(self.write_next().returncode, 0)
        self.assertEqual(self.write_next("recover").returncode, 0)
        self.assertEqual(self.current.read_bytes(), self.new)
        self.assertEqual(self.current.stat().st_mode & 0o777, 0o600)

    def test_wrong_hash_and_unsafe_operation_refuse_before_writes(self):
        self.assertNotEqual(
            self.command(private.PREFLIGHT, self.old_hash, operation="../unsafe").returncode, 0
        )
        self.backup_original()
        self.assertNotEqual(self.write_next(old_hash="f" * 64).returncode, 0)
        self.assertFalse(self.pending.exists())
        self.assertEqual(self.current.read_bytes(), self.old)

    def test_symlink_guards_include_dangling_backup_temporary_and_config(self):
        self.backup_original()
        outside = self.root / "outside"
        outside.write_bytes(b"untouched")
        for path in (self.backup, self.pending, self.current):
            original = path.read_bytes() if path.exists() else None
            original_mode = path.stat().st_mode & 0o777 if path.exists() else None
            if path.exists():
                path.unlink()
            for destination in (outside, self.root / "missing"):
                with self.subTest(path=path.name, destination=destination.name):
                    path.symlink_to(destination)
                    self.assertNotEqual(self.write_next("recover").returncode, 0)
                    self.assertTrue(path.is_symlink())
                    self.assertEqual(outside.read_bytes(), b"untouched")
                    path.unlink()
            if original is not None:
                path.write_bytes(original)
                assert original_mode is not None
                path.chmod(original_mode)
