"""Exercise the optional local kind recipes with disposable fixtures."""

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
    if [[ -s "$FIXTURE_STATE/cluster.tfstate" ]]; then
      printf 'kind_cluster.pilot\\n'
    else
      printf '%s\\n' "$FIXTURE_RESOURCES"
    fi
    ;;
  console)
    read -r expression
    [[ "$expression" == *"var.cluster_name == \\"$FIXTURE_CLUSTER\\"" ]]
    printf '%s\\n' "$FIXTURE_NAME_MATCH"
    ;;
  apply)
    [[ ! -v TF_CLI_ARGS_apply && $KUBECONFIG == "$plan_dir/provider.kubeconfig" ]]
    [[ $2 == -no-color && $3 == -input=false && $4 == -lock-timeout=30s ]]
    [[ $5 == "-state=$FIXTURE_STATE/cluster.tfstate" && $6 == "-state-out=$FIXTURE_STATE/cluster.tfstate" ]]
    [[ $7 == "-backup=$FIXTURE_STATE/cluster.tfstate.backup" && $8 == "$plan_dir/saved.tfplan" && $# == 8 ]]
    if flock -n "$FIXTURE_STATE/operation.lock" true; then exit 99; fi
    printf 'apply\\n' >> "$FIXTURE_APPLY_ATTEMPTS"
    if [[ $FIXTURE_APPLY_ERROR == stale ]]; then
      printf 'Error: Saved plan is stale\\n' >&2
      exit 1
    fi
    printf 'fixture applied state\\n' > "$FIXTURE_STATE/cluster.tfstate"
    printf 'fixture apply complete\\n'
    ;;
  plan)
    [[ $KUBECONFIG == "$plan_dir/provider.kubeconfig" && $* == *'-lock-timeout=30s'* ]]
    if flock -n "$FIXTURE_STATE/operation.lock" true; then exit 99; fi
    if [[ $* == *'-detailed-exitcode'* ]]; then
      [[ $* == *"-state=$FIXTURE_STATE/cluster.tfstate"* ]]
      printf 'fixture repeat plan\\n'
      exit "$FIXTURE_REPEAT_EXIT"
    fi
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
    if [[ $2 == -json ]]; then
      printf '{"variables":{"cluster_name":{"value":"%s"}}}\\n' "$FIXTURE_PLAN_CLUSTER"
    else
      printf 'fixture readable plan\\n'
    fi
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
[[ $1 == --kubeconfig ]]
case "$2" in
  "$FIXTURE_STATE/kubeconfig"|"$FIXTURE_STATE/plans/"*/kubeconfig) ;;
  *) exit 99 ;;
esac
[[ $3 == --context && $4 == "kind-$FIXTURE_CLUSTER" ]]
if flock -n "$FIXTURE_STATE/operation.lock" true; then exit 99; fi
shift 4
if [[ $1 == --request-timeout=* ]]; then shift; fi
case "$1 $2" in
  'config view')
    printf 'https://127.0.0.1:12345'
    ;;
  'get namespace')
    [[ $3 == kube-system ]]
    printf '%s' "$FIXTURE_LIVE_UID"
    ;;
  'wait --for=condition=Ready')
    [[ $3 == --timeout=90s && $# == 6 ]]
    [[ $4 == "node/$FIXTURE_CLUSTER-control-plane" &&
       $5 == "node/$FIXTURE_CLUSTER-worker" && $6 == "node/$FIXTURE_CLUSTER-worker2" ]]
    if [[ $FIXTURE_READINESS != ready ]]; then
      printf '%s\\n' "$FIXTURE_READINESS" >&2
      exit 124
    fi
    ;;
  'get nodes')
    case "$3" in
      -o)
        printf 'node/%s\\n' "$FIXTURE_CLUSTER-worker" "$FIXTURE_CLUSTER-control-plane"
        if [[ $FIXTURE_INVENTORY != missing ]]; then printf 'node/%s\\n' "$FIXTURE_CLUSTER-worker2"; fi
        if [[ $FIXTURE_INVENTORY == extra ]]; then printf 'node/%s\\n' "$FIXTURE_CLUSTER-extra"; fi
        ;;
      -L) printf 'fixture node inventory\\n' ;;
      -l)
        case "$4" in
          'workload=control-plane,node-role.kubernetes.io/control-plane') node=control-plane ;;
          'workload=performance-generator,perfeng.io/node-type=generator') node=worker ;;
          'workload=sut,perfeng.io/node-type=sut') node=worker2 ;;
          *) exit 99 ;;
        esac
        if [[ $FIXTURE_INVENTORY == placement && $node == worker2 ]]; then node=worker; fi
        printf 'node/%s\\n' "$FIXTURE_CLUSTER-$node"
        ;;
      *) exit 99 ;;
    esac
    ;;
  "wait "--for=jsonpath*)
    [[ $4 == namespace/kube-system && $2 == "--for=jsonpath={.metadata.uid}=$FIXTURE_LIVE_UID" ]]
    ;;
  *) exit 99 ;;
esac
"""

KIND = """#!/bin/bash
set -eu
[[ $1 == get && $2 == kubeconfig && $3 == --name && $4 == "$FIXTURE_CLUSTER" ]]
if flock -n "$FIXTURE_STATE/operation.lock" true; then exit 99; fi
printf 'fixture dedicated kubeconfig\\n'
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
        for signature in (
            "plan PROFILE INTENT",
            "apply PROFILE INTENT ABSOLUTE_PLAN_PATH SHA256 APPROVER APPROVAL_UTC",
            "verify PROFILE ABSOLUTE_PLAN_PATH",
        ):
            with self.subTest(recipe=signature):
                self.assertRegex(result.stdout, rf"(?m)^\s+{signature}\b")

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
            ("kind", KIND),
            ("timeout", TIMEOUT),
        ):
            tool = tools / name
            tool.write_text(contents, encoding="utf-8")
            tool.chmod(0o700)
        (self.state / "opentofu.tfrc").touch(mode=0o600)
        self.plan_attempted = self.root / "plan-attempted"
        self.tofu_deadlines = self.root / "tofu-deadlines"
        self.apply_attempts = self.root / "apply-attempts"
        real_timeout = shutil.which("timeout", path=os.defpath)
        if real_timeout is None:
            self.fail("timeout is required for the local kind planning fixtures")
        self.env.update(
            EXPECTED_DAEMON="fixture-daemon",
            FIXTURE_DAEMON="fixture-daemon",
            FIXTURE_CLUSTER="perfeng-iac-pilot-74"
            if profile == "pilot-74"
            else "perfeng-retained-434",
            FIXTURE_PLAN_CLUSTER="perfeng-iac-pilot-74"
            if profile == "pilot-74"
            else "perfeng-retained-434",
            FIXTURE_APPLY_ATTEMPTS=str(self.apply_attempts),
            FIXTURE_APPLY_ERROR="none",
            FIXTURE_INVENTORY="correct",
            FIXTURE_READINESS="ready",
            FIXTURE_REPEAT_EXIT="0",
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

    def saved_plan(self, interface: str, profile: str, intent: str):
        result = self.plan(interface, profile, intent)
        self.assertEqual(result.returncode, 0, result.stderr)
        return next((self.state / "plans").glob("plan.*/saved.tfplan"))

    def apply(self, profile: str, intent: str, saved: Path | str, digest: str):
        return self.command(
            JUST or "just",
            "apply",
            profile,
            intent,
            str(saved),
            digest,
            "Fixture Approver",
            "2026-11-28T01:02:03Z",
            cwd=self.checkout,
        )

    def register_existing_cluster(self):
        (self.state / "identity.json").write_text(
            '{"daemon":"fixture-daemon","uid":"fixture-uid"}\n', encoding="utf-8"
        )
        (self.state / "kubeconfig").touch(mode=0o600)
        (self.state / "cluster.tfstate").touch(mode=0o600)
        self.env.update(FIXTURE_RESOURCES="kind_cluster.pilot")

    def test_applies_a_native_managed_plan_and_verifies_with_staged_inputs(self):
        profile = "pilot-74"
        self.prepare_checkout(profile)
        self.register_existing_cluster()
        saved = self.saved_plan("native", profile, "managed")
        digest = hashlib.sha256(saved.read_bytes()).hexdigest()
        (self.checkout / MODULE / "main.tf").unlink()
        self.env["TF_CLI_ARGS_apply"] = "-lock=false"
        result = self.apply(profile, "managed", saved, digest)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("zero changes", result.stdout)
        self.assertEqual((saved.parent / "apply.log").read_text(), "fixture apply complete\n")
        self.assertEqual((saved.parent / "nodes.txt").read_text(), "fixture node inventory\n")
        self.assertEqual((saved.parent / "repeat.log").read_text(), "fixture repeat plan\n")
        self.assertEqual(self.apply_attempts.read_text(), "apply\n")
        retried = self.apply(profile, "managed", saved, digest)
        self.assertNotEqual(retried.returncode, 0)
        self.assertIn("already attempted", retried.stderr)
        self.assertEqual(self.apply_attempts.read_text(), "apply\n")
        self.assert_lock_released()

    def native_block(self, heading: str):
        runbook = (self.checkout / RUNBOOK).read_text(encoding="utf-8")
        section = runbook.split(f"{heading}\n", 1)[1]
        return section.split("~~~sh\n", 1)[1].split("~~~", 1)[0]

    def native_operation(self, *headings: str):
        session = self.native_block("## Open a locked session").split('if [[ -d "$STATE_DIR"', 1)[0]
        source = "\n".join(
            (
                session,
                'exec 9>>"$STATE_DIR/operation.lock"',
                "flock -w 15 9",
                'KCFG="$STATE_DIR/kubeconfig"',
                'CTX="kind-$CLUSTER"',
                'EXPECTED_DAEMON=$(jq -er ".daemon" "$STATE_DIR/identity.json")',
                'EXPECTED_UID=$(jq -er ".uid" "$STATE_DIR/identity.json")',
                *(self.native_block(heading) for heading in headings),
            )
        )
        return self.command(
            "bash",
            "--noprofile",
            "--norc",
            "-e",
            "-o",
            "pipefail",
            cwd=self.checkout,
            input_text=source,
        )

    def verify(self, profile: str, saved: Path):
        return self.command(JUST or "just", "verify", profile, str(saved), cwd=self.checkout)

    def test_native_apply_of_a_recipe_plan_can_resume_with_the_recipe(self):
        profile = "pilot-74"
        self.prepare_checkout(profile)
        self.register_existing_cluster()
        saved = self.saved_plan("just", profile, "managed")
        digest = hashlib.sha256(saved.read_bytes()).hexdigest()
        self.env.update(
            INTENT="managed",
            APPROVED_PLAN=str(saved),
            APPROVED_SHA256=digest,
            APPROVED_BY="Fixture Approver",
            APPROVED_AT="2026-11-28T01:02:03Z",
        )
        result = self.native_operation(
            "### Locate the approved plan",
            "## Check the plan target",
            "### Managed target",
            "### Run the saved apply",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.verify(profile, saved)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("zero changes", result.stdout)
        self.assertEqual(self.apply_attempts.read_text(), "apply\n")
        self.assert_lock_released()

    def test_native_verification_resumes_an_interrupted_recipe_apply(self):
        profile = "pilot-74"
        self.prepare_checkout(profile)
        self.register_existing_cluster()
        saved = self.saved_plan("native", profile, "managed")
        digest = hashlib.sha256(saved.read_bytes()).hexdigest()
        self.env["FIXTURE_READINESS"] = "fixture wait interrupted"
        result = self.apply(profile, "managed", saved, digest)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("verification phase", result.stderr)
        log = saved.parent / "verify.log"
        self.assertIn("fixture wait interrupted", log.read_text())
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assert_lock_released()

        self.env.update(FIXTURE_READINESS="ready", APPROVED_PLAN=str(saved))
        result = self.native_operation(
            "### Locate the approved plan",
            "### Resume verification",
            "## Check the plan target",
            "### Managed target",
            "## Verify the apply",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("zero changes", result.stdout)
        self.assertEqual(self.apply_attempts.read_text(), "apply\n")
        self.assert_lock_released()

    def test_create_records_the_uid_from_the_dedicated_kubeconfig(self):
        profile = "pilot-74"
        self.prepare_checkout(profile)
        saved = self.saved_plan("native", profile, "create")
        digest = hashlib.sha256(saved.read_bytes()).hexdigest()
        result = self.apply(profile, "create", saved, digest)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("zero changes", result.stdout)
        for filename, expected in (
            ("kubeconfig", "fixture dedicated kubeconfig"),
            ("identity.json", "fixture-uid"),
        ):
            registered = self.state / filename
            self.assertEqual(registered.read_bytes(), (saved.parent / filename).read_bytes())
            self.assertIn(expected, registered.read_text())
            self.assertEqual(registered.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.apply_attempts.read_text(), "apply\n")
        self.assert_lock_released()

    def test_incomplete_create_registration_preserves_evidence_for_recovery(self):
        profile = "pilot-74"
        self.prepare_checkout(profile)
        saved = self.saved_plan("just", profile, "create")
        digest = hashlib.sha256(saved.read_bytes()).hexdigest()
        self.env["FIXTURE_LIVE_UID"] = ""
        result = self.apply(profile, "create", saved, digest)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("registration phase", result.stderr)
        self.assertIn("has no UID", result.stderr)
        self.assertTrue((saved.parent / "kubeconfig").exists())
        self.assertFalse((self.state / "identity.json").exists())
        self.assertFalse((saved.parent / "repeat.log").exists())
        result = self.verify(profile, saved)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("kubeconfig or identity record is missing", result.stderr)
        self.assertEqual(self.apply_attempts.read_text(), "apply\n")
        self.assert_lock_released()

    def test_apply_refuses_a_wrong_daemon_uid_or_saved_plan_cluster(self):
        profile = "retained-434"
        self.prepare_checkout(profile)
        self.register_existing_cluster()
        saved = self.saved_plan("just", profile, "managed")
        digest = hashlib.sha256(saved.read_bytes()).hexdigest()
        for variable, wrong, message in (
            ("FIXTURE_DAEMON", "other-daemon", "Docker daemon ID differs"),
            ("FIXTURE_LIVE_UID", "other-uid", "live kube-system UID"),
            (
                "FIXTURE_PLAN_CLUSTER",
                "perfeng-iac-pilot-74",
                "saved plan names a different cluster",
            ),
        ):
            with self.subTest(variable=variable):
                previous = self.env[variable]
                self.env[variable] = wrong
                result = self.apply(profile, "managed", saved, digest)
                self.env[variable] = previous
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertFalse(self.apply_attempts.exists())
                self.assertFalse((saved.parent / "apply.status").exists())
                self.assert_lock_released()

    def test_stale_apply_keeps_backend_refusal_and_never_plans_or_verifies(self):
        profile = "pilot-74"
        self.prepare_checkout(profile)
        saved = self.saved_plan("native", profile, "create")
        digest = hashlib.sha256(saved.read_bytes()).hexdigest()
        self.env["FIXTURE_APPLY_ERROR"] = "stale"
        self.plan_attempted.unlink()
        result = self.apply(profile, "create", saved, digest)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("apply phase", result.stderr)
        self.assertIn("Saved plan is stale", (saved.parent / "apply.log").read_text())
        self.assertFalse(self.plan_attempted.exists())
        self.assertFalse((self.state / "identity.json").exists())
        result = self.verify(profile, saved)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no completed apply receipt", result.stderr)
        self.assertEqual(self.apply_attempts.read_text(), "apply\n")
        self.assert_lock_released()

    def test_verify_requires_ready_nodes_exact_inventory_placement_and_zero_changes(self):
        profile = "pilot-74"
        self.prepare_checkout(profile)
        self.register_existing_cluster()
        saved = self.saved_plan("just", profile, "managed")
        digest = hashlib.sha256(saved.read_bytes()).hexdigest()
        result = self.apply(profile, "managed", saved, digest)
        self.assertEqual(result.returncode, 0, result.stderr)
        for variable, wrong, message in (
            ("FIXTURE_READINESS", "one node unready", "did not all become Ready"),
            ("FIXTURE_INVENTORY", "missing", "Unexpected node inventory"),
            ("FIXTURE_INVENTORY", "extra", "Unexpected node inventory"),
            ("FIXTURE_INVENTORY", "placement", "Unexpected placement"),
            ("FIXTURE_REPEAT_EXIT", "2", "Repeat plan exited 2"),
        ):
            with self.subTest(variable=variable, wrong=wrong):
                previous = self.env[variable]
                self.env[variable] = wrong
                result = self.verify(profile, saved)
                self.env[variable] = previous
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertIn("verification phase", result.stderr)
                self.assert_lock_released()
        result = self.verify(profile, saved)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("zero changes", result.stdout)
        self.assertEqual(self.apply_attempts.read_text(), "apply\n")

    def test_apply_refuses_an_unapproved_digest_and_noncanonical_plan(self):
        profile = "pilot-74"
        self.prepare_checkout(profile)
        saved = self.saved_plan("native", profile, "create")
        digest = hashlib.sha256(saved.read_bytes()).hexdigest()
        alias = saved.with_name("link.tfplan")
        alias.symlink_to(saved)
        outside = self.root / "saved.tfplan"
        shutil.copy2(saved, outside)
        for path, sha, message in (
            (saved, "0" * 64, "approved SHA-256"),
            (saved.relative_to(self.root), digest, "canonical saved plan"),
            (f"{saved.parent}/../{saved.parent.name}/saved.tfplan", digest, "canonical saved plan"),
            (outside, digest, "canonical saved plan"),
            (alias, digest, "canonical saved plan"),
        ):
            with self.subTest(path=path, digest=sha):
                result = self.apply(profile, "create", path, sha)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertFalse((saved.parent / "apply.log").exists())
                self.assertFalse((saved.parent / "apply.status").exists())
                self.assert_lock_released()

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
