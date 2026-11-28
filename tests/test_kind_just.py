"""Exercise the optional kind planning interface with disposable fixtures."""

import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
JUST = os.environ.get("KIND_TEST_JUST")
MODULE = Path("clusters/local-kind/iac-pilot")
RUNBOOK = Path("docs/local-kind-opentofu.md")
PROFILE_FILES = (
    "main.tf",
    ".terraform.lock.hcl",
    "profiles/pilot-74/target.auto.tfvars",
    "profiles/retained-434/target.auto.tfvars",
    "profiles/retained-434/retained-lifecycle.tf",
)

DOCKER = """#!/bin/bash
set -eu
[[ $1 == --host && $2 == unix:///var/run/docker.sock ]]
case "$3 $4" in
  'info --format') printf '%s\\n' "$FIXTURE_DAEMON" ;;
  'ps -a')
    case "$6" in
      "label=io.x-k8s.kind.cluster=$FIXTURE_CLUSTER") printf '%s' "$FIXTURE_LABEL" ;;
      "name=^/$FIXTURE_CLUSTER-") printf '%s' "$FIXTURE_NAME" ;;
      *) exit 99 ;;
    esac ;;
  *) exit 99 ;;
esac
"""

TOFU = """#!/bin/bash
set -eu
[[ ! -v TF_LOG && ! -v TF_LOG_PATH && ! -v TF_LOG_CORE && ! -v TF_LOG_PROVIDER ]]
plan_dir=${1#-chdir=}
shift
case "$1" in
  init)
    [[ $* == *'-lockfile=readonly'* && $* == *"-backend-config=path=$FIXTURE_STATE/cluster.tfstate"* ]]
    [[ $TF_CLI_CONFIG_FILE == "$FIXTURE_STATE/opentofu.tfrc" ]]
    ;;
  state)
    [[ $2 == list ]]
    if [[ $# == 2 && ${TF_CLI_ARGS_state_list:-} == -state=* ]]; then
      exit 0
    fi
    [[ $3 == "-state=$FIXTURE_STATE/cluster.tfstate" && ! -v TF_CLI_ARGS_state_list ]]
    printf '%s\\n' "$FIXTURE_RESOURCES"
    ;;
  console)
    read -r expression
    [[ "$expression" == *"var.cluster_name == \\"$FIXTURE_CLUSTER\\"" ]]
    printf '%s\\n' "$FIXTURE_NAME_MATCH"
    ;;
  plan)
    [[ $KUBECONFIG == "$plan_dir/provider.kubeconfig" && $* == *'-lock-timeout=30s'* ]]
    if flock -n "$FIXTURE_STATE/operation.lock" true; then exit 99; fi
    touch "$FIXTURE_PLAN_ATTEMPTED"
    printf 'fixture saved plan\\n' > "$plan_dir/saved.tfplan"
    if [[ $FIXTURE_PLAN_ERROR == yes ]]; then
      printf 'fixture plan failure\\n' >&2
      exit 1
    fi
    if [[ $FIXTURE_PLAN_ERROR == timeout ]]; then
      [[ -e $FIXTURE_TOFU_DEADLINES ]] || exit 99
      printf 'fixture plan waiting\\n' >&2
      trap '' TERM
      exec sleep 10
    fi
    ;;
  show)
    [[ $3 == "$plan_dir/saved.tfplan" ]]
    printf 'fixture readable plan\\n'
    ;;
  *) exit 99 ;;
esac
"""

TIMEOUT = """#!/bin/bash
set -eu
if [[ $1 == --verbose && $2 == --kill-after=5s && $4 == tofu ]]; then
  printf '%s %s\\n' "$3" "$6" >> "$FIXTURE_TOFU_DEADLINES"
  if [[ $6 == plan && $FIXTURE_PLAN_ERROR == timeout ]]; then
    shift 3
    exec "$FIXTURE_REAL_TIMEOUT" --verbose --kill-after=0.2s 0.2s "$@"
  fi
fi
exec "$FIXTURE_REAL_TIMEOUT" "$@"
"""

KUBECTL = """#!/bin/bash
set -eu
[[ $1 == --kubeconfig && $2 == "$FIXTURE_STATE/kubeconfig" ]]
[[ $3 == --context && $4 == "kind-$FIXTURE_CLUSTER" ]]
[[ $5 == --request-timeout=* && $6 == wait && $9 == namespace/kube-system ]]
[[ $7 == "--for=jsonpath={.metadata.uid}=$FIXTURE_LIVE_UID" ]]
"""


@unittest.skipUnless(JUST, "set KIND_TEST_JUST to the pinned Linux AMD64 just binary")
class KindJustTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix="kind-just-fixture-")
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        self.home = self.root / "operator home"
        self.home.mkdir(mode=0o700)
        self.env = {"HOME": str(self.home), "PATH": os.defpath, "GIT_CONFIG_NOSYSTEM": "1"}

    def test_bare_just_lists_supported_tasks(self):
        result = self.command(JUST or "just", cwd=REPO)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"(?m)^\s+plan PROFILE INTENT\b")

    def test_plan_refuses_unregistered_profile_and_intent(self):
        result = self.command(JUST or "just", "plan", "pilot-74", cwd=REPO)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.home / ".local").exists())
        marker = self.root / "substitution-ran"
        for profile, intent, message in (
            (f"$(touch {marker})", "create", "Unknown kind profile"),
            ("pilot-74", "apply", "Plan intent must be create or managed"),
        ):
            with self.subTest(profile=profile, intent=intent):
                result = self.command(JUST or "just", "plan", profile, intent, cwd=REPO)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertFalse(marker.exists())
                self.assertFalse((self.home / ".local").exists())

    def command(self, *args: str, cwd: Path, input_text: str | None = None):
        return subprocess.run(
            args,
            cwd=cwd,
            env=self.env,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    def git(self, *args: str):
        result = self.command("git", *args, cwd=self.checkout)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def prepare_checkout(self, profile: str):
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        for path in (Path("justfile"), RUNBOOK, *(MODULE / name for name in PROFILE_FILES)):
            destination = self.checkout / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / path, destination)
        self.git("init", "--quiet")
        self.git("add", ".")
        self.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "Reviewed inputs",
        )
        self.revision = self.git("rev-parse", "HEAD")

        self.state = (
            self.home
            / ".local/state/perfeng"
            / ("kind-74" if profile == "pilot-74" else "kind-434")
        )
        self.state.mkdir(mode=0o700, parents=True)
        tools = self.home / ".local/state/perfeng/kind-74/bin"
        tools.mkdir(mode=0o700, parents=True)
        for name, contents in (
            ("docker", DOCKER),
            ("tofu", TOFU),
            ("kubectl", KUBECTL),
            ("timeout", TIMEOUT),
        ):
            tool = tools / name
            tool.write_text(contents, encoding="utf-8")
            tool.chmod(0o700)
        (self.state / "opentofu.tfrc").touch(mode=0o600)
        self.plan_attempted = self.root / "plan-attempted"
        self.tofu_deadlines = self.root / "tofu-deadlines"
        real_timeout = shutil.which("timeout", path=os.defpath)
        if real_timeout is None:
            self.fail("timeout is required for the local kind planning fixtures")
        self.env.update(
            EXPECTED_DAEMON="fixture-daemon",
            FIXTURE_DAEMON="fixture-daemon",
            FIXTURE_CLUSTER="perfeng-iac-pilot-74"
            if profile == "pilot-74"
            else "perfeng-retained-434",
            FIXTURE_LABEL="",
            FIXTURE_NAME="",
            FIXTURE_STATE=str(self.state),
            FIXTURE_RESOURCES="",
            FIXTURE_NAME_MATCH="true",
            FIXTURE_LIVE_UID="fixture-uid",
            FIXTURE_PLAN_ATTEMPTED=str(self.plan_attempted),
            FIXTURE_PLAN_ERROR="no",
            FIXTURE_TOFU_DEADLINES=str(self.tofu_deadlines),
            FIXTURE_REAL_TIMEOUT=real_timeout,
        )

    def plan(self, interface: str, profile: str, intent: str):
        if interface == "just":
            return self.command(JUST or "just", "plan", profile, intent, cwd=self.checkout)
        self.env.update(PROFILE=profile, INTENT=intent)
        runbook = (self.checkout / RUNBOOK).read_text(encoding="utf-8")
        native_section = runbook.split("### Plan with native commands\n", 1)[1]
        source = native_section.split("~~~bash\n", 1)[1].split("~~~", 1)[0]
        return self.command("bash", "--noprofile", "--norc", cwd=self.checkout, input_text=source)

    def test_directs_each_interface_to_reviewed_inputs_and_compatible_artifacts(self):
        profile = "retained-434"
        self.prepare_checkout(profile)
        inputs = {
            "main.tf": MODULE / "main.tf",
            ".terraform.lock.hcl": MODULE / ".terraform.lock.hcl",
            "target.auto.tfvars": MODULE / "profiles" / profile / "target.auto.tfvars",
            "retained_override.tf": MODULE / "profiles" / profile / "retained-lifecycle.tf",
        }
        expected_blobs = {
            name: self.git("rev-parse", f"{self.revision}:{path}") for name, path in inputs.items()
        }
        # Change only file metadata so a normal Git status would refresh the index.
        runbook = self.checkout / RUNBOOK
        metadata = runbook.stat()
        os.utime(runbook, ns=(metadata.st_atime_ns, metadata.st_mtime_ns - 1_000_000_000))
        self.assertEqual(self.git("--no-optional-locks", "status", "--porcelain"), "")
        index_before = (self.checkout / ".git/index").read_bytes()
        self.env.update(
            TF_LOG="TRACE",
            TF_LOG_PATH=str(self.root / "outside-plan.log"),
            TF_LOG_CORE="TRACE",
            TF_LOG_PROVIDER="TRACE",
        )

        for interface in ("just", "native"):
            with self.subTest(interface=interface):
                result = self.plan(interface, profile, "create")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((self.checkout / ".git/index").read_bytes(), index_before)
                reviews = set((self.state / "plans").glob("plan.*/REVIEW.md"))
                self.assertEqual(len(reviews), 1)
                review = reviews.pop()
                directory = review.parent
                self.assertEqual(
                    {
                        name: self.git("hash-object", "--no-filters", str(directory / name))
                        for name in inputs
                    },
                    expected_blobs,
                )
                self.assertEqual(
                    (directory / "source-revision.txt").read_text().strip(), self.revision
                )
                self.assertEqual((directory / "plan.txt").read_text(), "fixture readable plan\n")
                saved = directory / "saved.tfplan"
                digest = hashlib.sha256(b"fixture saved plan\n").hexdigest()
                self.assertEqual((directory / "SHA256SUMS").read_text(), f"{digest}  {saved}\n")
                evidence = review.read_text(encoding="utf-8")
                for field in (profile, self.revision, "create", str(saved), digest):
                    self.assertIn(field, evidence)
                self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
                self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
                self.assert_lock_released()
                shutil.rmtree(directory)

    def assert_lock_released(self):
        lock = self.command(
            "flock", "-n", str(self.state / "operation.lock"), "true", cwd=self.checkout
        )
        self.assertEqual(lock.returncode, 0, lock.stderr)

    def assert_refused(self, interface: str, intent: str, message: str):
        result = self.plan(interface, "pilot-74", intent)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(message, result.stderr)
        self.assertFalse(self.plan_attempted.exists())
        self.assertFalse(list((self.state / "plans").glob("plan.*/REVIEW.md")))

    def test_create_refuses_a_wrong_daemon_or_either_docker_collision(self):
        self.prepare_checkout("pilot-74")
        for interface in ("just", "native"):
            for variable, value, message in (
                ("EXPECTED_DAEMON", "", "Set the registered Docker daemon ID"),
                ("FIXTURE_DAEMON", "other-daemon", "Docker daemon ID differs"),
                ("FIXTURE_LABEL", "occupied-label", "kind name is already in use"),
                ("FIXTURE_NAME", "occupied-name", "kind name is already in use"),
                ("FIXTURE_NAME_MATCH", "false", "staged cluster name differs"),
            ):
                with self.subTest(interface=interface, variable=variable):
                    original = self.env[variable]
                    self.env[variable] = value
                    try:
                        self.assert_refused(interface, "create", message)
                    finally:
                        self.env[variable] = original

    def test_create_refuses_existing_state_and_registration(self):
        self.prepare_checkout("pilot-74")
        (self.state / "cluster.tfstate").touch(mode=0o600)
        self.env["FIXTURE_RESOURCES"] = "kind_cluster.pilot"
        for interface in ("just", "native"):
            with self.subTest(interface=interface):
                self.assert_refused(interface, "create", "state already contains resources")
        (self.state / "identity.json").touch(mode=0o600)
        for interface in ("just", "native"):
            with self.subTest(interface=interface):
                self.assert_refused(interface, "create", "already has a registration file")

    def test_refuses_dirty_checkout_and_redirected_plan_directory(self):
        self.prepare_checkout("pilot-74")
        untracked = self.checkout / "work-in-progress"
        untracked.touch()
        for interface in ("just", "native"):
            with self.subTest(interface=interface):
                self.assert_refused(interface, "create", "clean, reviewed checkout")
        untracked.unlink()
        redirected = self.root / "redirected"
        redirected.mkdir()
        (self.state / "plans").symlink_to(redirected)
        for interface in ("just", "native"):
            with self.subTest(interface=interface):
                self.assert_refused(
                    interface, "create", "state file or plan directory is invalid or redirected"
                )
        self.assertFalse(list(redirected.iterdir()))

    def test_refuses_git_flags_that_hide_planning_files(self):
        self.prepare_checkout("pilot-74")
        for flag, path in (
            ("assume-unchanged", MODULE / "main.tf"),
            ("skip-worktree", Path("justfile")),
        ):
            with self.subTest(flag=flag):
                file = self.checkout / path
                reviewed = file.read_bytes()
                self.git("update-index", f"--{flag}", str(path))
                file.write_bytes(reviewed + b"\n# Unreviewed local change\n")
                self.assertEqual(self.git("--no-optional-locks", "status", "--porcelain"), "")
                for interface in ("just", "native"):
                    with self.subTest(interface=interface):
                        self.assert_refused(interface, "create", flag)
                file.write_bytes(reviewed)
                self.git("update-index", f"--no-{flag}", str(path))

    def test_create_checks_occupied_state_despite_ambient_state_list_arguments(self):
        self.prepare_checkout("pilot-74")
        (self.state / "cluster.tfstate").touch(mode=0o600)
        alternate_state = self.root / "vacant.tfstate"
        alternate_state.touch(mode=0o600)
        self.env.update(
            FIXTURE_RESOURCES="kind_cluster.pilot",
            TF_CLI_ARGS_state_list=f"-state={alternate_state}",
        )
        for interface in ("just", "native"):
            with self.subTest(interface=interface):
                self.assert_refused(interface, "create", "state already contains resources")

    def test_managed_requires_matching_registration_state_and_live_uid(self):
        self.prepare_checkout("pilot-74")
        for interface in ("just", "native"):
            with self.subTest(interface=interface):
                self.assert_refused(
                    interface, "managed", "kubeconfig or identity record is missing"
                )

        (self.state / "identity.json").write_text(
            '{"daemon":"fixture-daemon","uid":"fixture-uid"}\n', encoding="utf-8"
        )
        (self.state / "kubeconfig").touch(mode=0o600)
        (self.state / "cluster.tfstate").touch(mode=0o600)
        self.env.update(
            FIXTURE_RESOURCES="kind_cluster.pilot", EXPECTED_DAEMON="ignored-for-managed"
        )
        for interface in ("just", "native"):
            for variable, value, message in (
                ("FIXTURE_DAEMON", "other-daemon", "Docker daemon ID differs"),
                (
                    "FIXTURE_RESOURCES",
                    "kind_cluster.foreign",
                    "state must contain only kind_cluster.pilot",
                ),
                ("FIXTURE_NAME_MATCH", "false", "state or staged cluster name differs"),
                ("FIXTURE_LIVE_UID", "other-uid", "live kube-system UID could not be matched"),
            ):
                with self.subTest(interface=interface, variable=variable):
                    original = self.env[variable]
                    self.env[variable] = value
                    try:
                        self.assert_refused(interface, "managed", message)
                    finally:
                        self.env[variable] = original

        for interface in ("just", "native"):
            with self.subTest(interface=interface):
                self.tofu_deadlines.unlink(missing_ok=True)
                result = self.plan(interface, "pilot-74", "managed")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    self.tofu_deadlines.read_text().splitlines(),
                    ["120s init", "60s state", "60s console", "180s plan", "60s show"],
                )
        self.assertEqual(len(list((self.state / "plans").glob("plan.*/REVIEW.md"))), 2)

    def test_failed_plan_keeps_diagnostics_without_successful_review(self):
        self.prepare_checkout("pilot-74")
        self.env["FIXTURE_PLAN_ERROR"] = "yes"
        for interface in ("just", "native"):
            with self.subTest(interface=interface):
                result = self.plan(interface, "pilot-74", "create")
                self.assertNotEqual(result.returncode, 0)
                self.assert_lock_released()
        self.assertTrue(self.plan_attempted.exists())
        plans = self.state / "plans"
        self.assertFalse(list(plans.glob("plan.*/REVIEW.md")))
        logs = sorted(plans.glob("plan.*/plan.log"))
        self.assertEqual([log.read_text() for log in logs], ["fixture plan failure\n"] * 2)

    def test_timed_out_plan_keeps_diagnostics_and_releases_the_lock(self):
        self.prepare_checkout("pilot-74")
        self.env["FIXTURE_PLAN_ERROR"] = "timeout"
        for interface in ("just", "native"):
            with self.subTest(interface=interface):
                result = self.plan(interface, "pilot-74", "create")
                self.assertIn(result.returncode, (124, 137), result.stderr)
                self.assert_lock_released()
        plans = self.state / "plans"
        self.assertFalse(list(plans.glob("plan.*/REVIEW.md")))
        self.assertFalse(list(plans.glob("plan.*/SHA256SUMS")))
        logs = sorted(plans.glob("plan.*/plan.log"))
        self.assertEqual(len(logs), 2)
        for log in logs:
            evidence = log.read_text()
            self.assertIn("fixture plan waiting", evidence)
            self.assertIn("sending signal KILL", evidence)
