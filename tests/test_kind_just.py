"""Exercise the optional local kind recipes with disposable fixtures."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
JUST = os.environ.get("KIND_TEST_JUST")
MODULE = Path("clusters/local-kind/iac-pilot")
RUNBOOK = Path("docs/local-kind-opentofu.md")
OVERLAY = Path("clusters/local-kind/perfeng-local/addons/argocd")
BOOTSTRAP_MANIFEST = b"apiVersion: v1\nkind: Namespace\nmetadata:\n  name: argocd\n"
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
    config=$(jq -cn --arg server "$FIXTURE_STATE_CONFIG_SERVER" --arg ca "$FIXTURE_STATE_CONFIG_CA" \
      '{clusters: [{cluster: {server: $server, "certificate-authority-data": $ca}}]}')
    jq -cn --arg name "$FIXTURE_CLUSTER" --arg endpoint "$FIXTURE_STATE_ENDPOINT" --arg config "$config" \
      '{resources: [{mode: "managed", type: "kind_cluster", name: "pilot",
        provider: "provider[\\"registry.terraform.io/elioseverojunior/kind\\"]",
        instances: [{attributes: {name: $name, endpoint: $endpoint, kubeconfig: $config}}]}]}' \
      > "$FIXTURE_STATE/cluster.tfstate"
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
      if [[ -e "$FIXTURE_STATE/kubeconfig" ]]; then actions='["no-op"]'; else actions='["create"]'; fi
      changes=$(printf '[{"address":"kind_cluster.pilot","mode":"managed","provider_name":"registry.terraform.io/elioseverojunior/kind","change":{"actions":%s,"after":{"name":"%s"}}}]' \
        "$actions" "$FIXTURE_PLAN_CLUSTER")
      printf '{"variables":{"cluster_name":{"value":"%s"}},"resource_changes":%s}\\n' \
        "$FIXTURE_PLAN_CLUSTER" "${FIXTURE_PLAN_RESOURCE_CHANGES:-$changes}"
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
change_manifest() {
  local manifest
  manifest=$(cat -- "$FIXTURE_BOOTSTRAP_STAGED_MANIFEST")
  printf 'fixture altered manifest\\n' > "$manifest"
  (cd "${manifest%/*}" && sha256sum -- argocd.yaml > SHA256SUMS)
}
if [[ $1 == kustomize ]]; then
  [[ $# == 2 && $2 == "$FIXTURE_STATE"/bootstrap.*/"$FIXTURE_OVERLAY" ]]
  if flock -n "$FIXTURE_STATE/operation.lock" true; then exit 99; fi
  grep -Fq 'argo-cd/v3.5.2/manifests/install.yaml' "$2/kustomization.yaml"
  [[ -f $2/namespace.yaml && -f $2/redis-pull-policy.yaml ]]
  printf 'render\\n' >> "$FIXTURE_BOOTSTRAP_ACTIVITY"
  printf '%s/argocd.yaml\\n' "${2%/"$FIXTURE_OVERLAY"}" > "$FIXTURE_BOOTSTRAP_STAGED_MANIFEST"
  printf '%s' "$FIXTURE_BOOTSTRAP_MANIFEST"
  exit 0
fi
[[ $1 == --kubeconfig ]]
config=$2
case "$2" in
  "$FIXTURE_STATE/kubeconfig"|"$FIXTURE_STATE/plans/"*/kubeconfig|/dev/stdin) ;;
  *) exit 99 ;;
esac
[[ $3 == --context && $4 == "kind-$FIXTURE_CLUSTER" ]]
if flock -n "$FIXTURE_STATE/operation.lock" true; then exit 99; fi
shift 4
if [[ $1 == --request-timeout=* ]]; then shift; fi
case "$1 $2" in
  'config view')
    [[ $3 == --minify ]]
    if [[ $4 == --raw ]]; then
      [[ $5 == -o && $6 == 'jsonpath={.clusters[0].cluster.server}{" "}{.clusters[0].cluster.certificate-authority-data}' ]]
      jq -er '.clusters[0].cluster | .server + " " + .["certificate-authority-data"]' "$config"
    else
      jq -er '.clusters[0].cluster.server' "$config"
    fi
    ;;
  'get namespace')
    if [[ $3 == argocd ]]; then
      [[ $# == 6 && $4 == --ignore-not-found && $5 == -o ]]
      [[ $6 == 'jsonpath={.metadata.name}{" "}{.metadata.labels.app\\.kubernetes\\.io/name}{" "}{.metadata.labels.app\\.kubernetes\\.io/managed-by}{" "}{.metadata.labels.app\\.kubernetes\\.io/part-of}' ]]
      printf 'owner\\n' >> "$FIXTURE_BOOTSTRAP_ACTIVITY"
      if [[ $FIXTURE_BOOTSTRAP_TAMPER == before-apply ]]; then change_manifest; fi
      if [[ -e $FIXTURE_ARGO_OWNER ]]; then cat -- "$FIXTURE_ARGO_OWNER"; fi
      exit 0
    fi
    [[ $3 == kube-system ]]
    touch "$FIXTURE_UID_READ"
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
    live_uid=$FIXTURE_LIVE_UID
    if [[ -v FIXTURE_BOOTSTRAP_ACTIVITY ]]; then
      if grep -Fxq diff "$FIXTURE_BOOTSTRAP_ACTIVITY"; then live_uid=$FIXTURE_BOOTSTRAP_FINAL_UID; fi
      printf 'uid\\n' >> "$FIXTURE_BOOTSTRAP_ACTIVITY"
    fi
    [[ $4 == namespace/kube-system && $2 == "--for=jsonpath={.metadata.uid}=$live_uid" ]]
    ;;
  'apply --server-side'|'diff --server-side')
    [[ ! -v KUBECTL_EXTERNAL_DIFF ]]
    [[ $# == 5 && $3 == --field-manager=k8s-infrastructure && $4 == -f ]]
    [[ $5 == "$FIXTURE_STATE"/bootstrap.*/argocd.yaml && ! -L $5 ]]
    cmp -- "$5" <(printf '%s' "$FIXTURE_BOOTSTRAP_MANIFEST")
    printf '%s\\n' "$1" >> "$FIXTURE_BOOTSTRAP_ACTIVITY"
    if [[ $1 == apply ]]; then
      printf 'argocd argocd k8s-infrastructure kubernetes-substrate' > "$FIXTURE_ARGO_OWNER"
      printf 'fixture Argo CD applied\\n'
    else
      case "$FIXTURE_BOOTSTRAP_DIFF" in
        changes) printf '%s\\n' 'fixture diff changes'; exit 1 ;;
        error) printf '%s\\n' 'fixture diff error' >&2; exit 2 ;;
        unexpected-output) printf '%s\\n' 'fixture diff output' ;;
      esac
    fi
    ;;
  '-n argocd')
    [[ $* == '-n argocd rollout status --timeout=240s deployment/argocd-applicationset-controller deployment/argocd-dex-server deployment/argocd-notifications-controller deployment/argocd-redis deployment/argocd-repo-server deployment/argocd-server statefulset/argocd-application-controller' ]]
    printf 'rollout\\n' >> "$FIXTURE_BOOTSTRAP_ACTIVITY"
    if [[ $FIXTURE_BOOTSTRAP_ROLLOUT == failed ]]; then
      printf 'fixture rollout failed\\n' >&2
      exit 1
    fi
    if [[ $FIXTURE_BOOTSTRAP_TAMPER == before-diff ]]; then change_manifest; fi
    printf 'fixture Argo CD rolled out\\n'
    ;;
  *) exit 99 ;;
esac
"""

KIND = """#!/bin/bash
set -eu
[[ $1 == get && $2 == kubeconfig && $3 == --name && $4 == "$FIXTURE_CLUSTER" ]]
if flock -n "$FIXTURE_STATE/operation.lock" true; then exit 99; fi
jq -cn --arg server "$FIXTURE_DEDICATED_SERVER" --arg ca "$FIXTURE_DEDICATED_CA" \
  '{fixture: "fixture dedicated kubeconfig", clusters: [{cluster: {server: $server, "certificate-authority-data": $ca}}]}'
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
            "bootstrap PROFILE",
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
        for path in (
            Path("justfile"),
            RUNBOOK,
            *(MODULE / name for name in PROFILE_FILES),
            *(path.relative_to(REPO) for path in (REPO / OVERLAY).iterdir()),
        ):
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
        self.uid_read = self.root / "uid-read"
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
            FIXTURE_UID_READ=str(self.uid_read),
            FIXTURE_STATE_ENDPOINT="https://127.0.0.1:12345",
            FIXTURE_STATE_CONFIG_SERVER="https://127.0.0.1:12345",
            FIXTURE_STATE_CONFIG_CA="Zml4dHVyZS1jYQ==",
            FIXTURE_DEDICATED_SERVER="https://127.0.0.1:12345",
            FIXTURE_DEDICATED_CA="Zml4dHVyZS1jYQ==",
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
        plans = self.state / "plans"
        previous = set(plans.glob("plan.*/saved.tfplan"))
        result = self.plan(interface, profile, intent)
        self.assertEqual(result.returncode, 0, result.stderr)
        (saved,) = set(plans.glob("plan.*/saved.tfplan")) - previous
        return saved

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

    def register_existing_cluster(self, uid: str = "fixture-uid", daemon: str = "fixture-daemon"):
        (self.state / "identity.json").write_text(
            json.dumps({"daemon": daemon, "uid": uid}) + "\n", encoding="utf-8"
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

    def native_block(self, heading: str, index: int = 0):
        runbook = (self.checkout / RUNBOOK).read_text(encoding="utf-8")
        section = runbook.split(f"{heading}\n", 1)[1].split("\n## ", 1)[0]
        return re.findall(r"^~~~sh\n(.*?)^~~~$", section, re.MULTILINE | re.DOTALL)[index]

    def native_operation(self, *headings: str):
        session = "## Open a locked session"
        source = [self.native_block(session, 1)]
        if self.env.get("INTENT") != "create":
            source.append(self.native_block(session, 2))
        source.extend(self.native_block(heading) for heading in headings)
        return self.command(
            "bash",
            "--noprofile",
            "--norc",
            "-e",
            "-o",
            "pipefail",
            "-c",
            self.native_block(session),
            cwd=self.checkout,
            input_text="\n".join(source),
        )

    def native_apply(self, intent: str, saved: Path, digest: str, *after_apply: str):
        self.env.update(
            INTENT=intent,
            APPROVED_PLAN=str(saved),
            APPROVED_SHA256=digest,
            APPROVED_BY="Fixture Approver",
            APPROVED_AT="2026-11-28T01:02:03Z",
        )
        target = "### Vacant target" if intent == "create" else "### Managed target"
        return self.native_operation(
            "### Locate the approved plan",
            "## Check the plan target",
            target,
            "### Run the saved apply",
            *after_apply,
        )

    def verify(self, profile: str, saved: Path):
        return self.command(JUST or "just", "verify", profile, str(saved), cwd=self.checkout)

    def native_verify(self, saved: Path):
        self.env.update(INTENT="managed", APPROVED_PLAN=str(saved))
        return self.native_operation(
            "### Locate the approved plan",
            "### Resume verification",
            "## Check the plan target",
            "### Managed target",
            "## Verify the apply",
        )

    def prepare_bootstrap(self):
        self.prepare_checkout("pilot-74")
        self.register_existing_cluster()
        self.bootstrap_activity = self.root / "bootstrap-activity"
        self.argo_owner = self.root / "argo-owner"
        self.env.update(
            FIXTURE_BOOTSTRAP_ACTIVITY=str(self.bootstrap_activity),
            FIXTURE_BOOTSTRAP_MANIFEST=BOOTSTRAP_MANIFEST.decode(),
            FIXTURE_BOOTSTRAP_STAGED_MANIFEST=str(self.root / "bootstrap-manifest-path"),
            FIXTURE_BOOTSTRAP_FINAL_UID="fixture-uid",
            FIXTURE_BOOTSTRAP_DIFF="empty",
            FIXTURE_BOOTSTRAP_ROLLOUT="ready",
            FIXTURE_BOOTSTRAP_TAMPER="none",
            FIXTURE_ARGO_OWNER=str(self.argo_owner),
            FIXTURE_OVERLAY=str(OVERLAY),
            KUBECONFIG="/dev/null",
            KUBECTL_EXTERNAL_DIFF="false",
        )

    def bootstrap(self, interface: str):
        self.bootstrap_activity.unlink(missing_ok=True)
        if interface == "just":
            return self.command(JUST or "just", "bootstrap", "pilot-74", cwd=self.checkout)
        return self.native_operation("### Bootstrap with native commands")

    def test_bootstrap_and_repeat_share_the_same_private_evidence(self):
        self.prepare_bootstrap()
        previous = set()
        digest = hashlib.sha256(BOOTSTRAP_MANIFEST).hexdigest()
        for interface in ("just", "native"):
            with self.subTest(interface=interface):
                result = self.bootstrap(interface)
                self.assertEqual(result.returncode, 0, result.stderr)
                (run,) = set(self.state.glob("bootstrap.*")) - previous
                previous.add(run)
                self.assertEqual(
                    self.bootstrap_activity.read_text().splitlines(),
                    ["render", "uid", "owner", "apply", "rollout", "diff", "uid"],
                )
                self.assertEqual(run.stat().st_mode & 0o777, 0o700)
                for name, contents in (
                    ("source-revision.txt", self.revision + "\n"),
                    ("SHA256SUMS", digest + "  argocd.yaml\n"),
                    ("argocd.yaml", BOOTSTRAP_MANIFEST.decode()),
                    ("apply.log", "fixture Argo CD applied\n"),
                    ("rollout.log", "fixture Argo CD rolled out\n"),
                    ("diff.log", ""),
                    ("diff.stderr.log", ""),
                    ("render.log", ""),
                    ("identity.log", ""),
                ):
                    evidence = run / name
                    self.assertEqual(evidence.read_text(), contents)
                    self.assertEqual(evidence.stat().st_mode & 0o777, 0o600)
                receipt = run / "RESULT.md"
                self.assertEqual(
                    receipt.read_text(),
                    "# Argo CD bootstrap\n\n"
                    f"- Profile pilot-74\n- Source revision {self.revision}\n- Manifest SHA-256 {digest}\n"
                    "- All seven rollouts passed\n- Server-side diff is empty\n- Registered UID rechecked\n",
                )
                self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
                self.assertFalse((run / "RESULT.md.tmp").exists())
                self.assertNotIn("fixture Argo CD", result.stdout + result.stderr)
                self.assert_lock_released()

    def test_bootstrap_refuses_a_dirty_checkout(self):
        self.prepare_bootstrap()
        (self.checkout / OVERLAY / "namespace.yaml").write_text("dirty overlay\n")
        for interface in ("just", "native"):
            with self.subTest(interface=interface):
                result = self.bootstrap(interface)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("clean, reviewed checkout", result.stderr)
                self.assertFalse(self.bootstrap_activity.exists())
                self.assertEqual(list(self.state.glob("bootstrap.*")), [])
                self.assert_lock_released()

    def assert_bootstrap_failure(self, interface: str, phase: str, activity: str):
        previous = set(self.state.glob("bootstrap.*"))
        result = self.bootstrap(interface)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"bootstrap stopped during {phase}", result.stderr)
        (run,) = set(self.state.glob("bootstrap.*")) - previous
        self.assertEqual(self.bootstrap_activity.read_text().split(), activity.split())
        self.assertFalse((run / "RESULT.md").exists())
        self.assertFalse((run / "RESULT.md.tmp").exists())
        self.assert_lock_released()
        return run, result

    def test_bootstrap_refuses_wrong_uid_and_foreign_namespace_before_apply(self):
        self.prepare_bootstrap()
        for interface in ("just", "native"):
            with self.subTest(interface=interface, scenario="wrong uid"):
                self.register_existing_cluster(uid="wrong-uid")
                run, _ = self.assert_bootstrap_failure(
                    interface, "pre-apply identity", "render uid"
                )
                self.assertFalse((run / "apply.log").exists())
            self.register_existing_cluster()
            for owner in (
                "argocd   ",
                "argocd argocd foreign-manager kubernetes-substrate",
                "argocd argocd k8s-infrastructure other-part",
            ):
                with self.subTest(interface=interface, owner=owner):
                    self.argo_owner.write_text(owner)
                    run, _ = self.assert_bootstrap_failure(
                        interface, "namespace ownership", "render uid owner"
                    )
                    self.assertFalse((run / "apply.log").exists())

    def test_bootstrap_refuses_a_manifest_rewrite_even_if_its_checksum_is_replaced(self):
        self.prepare_bootstrap()
        for interface in ("just", "native"):
            for when, phase, activity in (
                ("before-apply", "apply", "render uid owner"),
                ("before-diff", "diff", "render uid owner apply rollout"),
            ):
                with self.subTest(interface=interface, when=when):
                    self.env["FIXTURE_BOOTSTRAP_TAMPER"] = when
                    _, result = self.assert_bootstrap_failure(interface, phase, activity)
                    self.assertIn("differs from its recorded SHA-256", result.stderr)

    def test_bootstrap_records_no_success_on_rollout_diff_or_final_identity_failure(self):
        self.prepare_bootstrap()
        for interface in ("just", "native"):
            with self.subTest(interface=interface, scenario="rollout"):
                self.env["FIXTURE_BOOTSTRAP_ROLLOUT"] = "failed"
                run, result = self.assert_bootstrap_failure(
                    interface, "rollout", "render uid owner apply rollout"
                )
                self.assertEqual((run / "rollout.log").read_text(), "fixture rollout failed\n")
                self.assertFalse((run / "diff.log").exists())
                self.assertNotIn("fixture rollout failed", result.stderr)
            self.env["FIXTURE_BOOTSTRAP_ROLLOUT"] = "ready"
            for diff, status, log, contents in (
                ("changes", 1, "diff.log", "fixture diff changes\n"),
                ("error", 2, "diff.stderr.log", "fixture diff error\n"),
                ("unexpected-output", 1, "diff.log", "fixture diff output\n"),
            ):
                with self.subTest(interface=interface, scenario=diff):
                    self.env["FIXTURE_BOOTSTRAP_DIFF"] = diff
                    run, result = self.assert_bootstrap_failure(
                        interface, "diff", "render uid owner apply rollout diff"
                    )
                    self.assertEqual(result.returncode, status)
                    self.assertEqual((run / log).read_text(), contents)
                    self.assertNotIn(contents.strip(), result.stdout + result.stderr)
            self.env["FIXTURE_BOOTSTRAP_DIFF"] = "empty"
            with self.subTest(interface=interface, scenario="final identity"):
                self.env["FIXTURE_BOOTSTRAP_FINAL_UID"] = "changed-uid"
                run, _ = self.assert_bootstrap_failure(
                    interface, "final identity", "render uid owner apply rollout diff uid"
                )
                self.assertEqual((run / "diff.log").read_text(), "")
                self.assertIn("UID could not be matched", (run / "identity.log").read_text())
            self.env["FIXTURE_BOOTSTRAP_FINAL_UID"] = "fixture-uid"

    def test_native_apply_of_a_recipe_plan_can_resume_with_the_recipe(self):
        profile = "pilot-74"
        self.prepare_checkout(profile)
        self.register_existing_cluster()
        saved = self.saved_plan("just", profile, "managed")
        digest = hashlib.sha256(saved.read_bytes()).hexdigest()
        result = self.native_apply("managed", saved, digest)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.verify(profile, saved)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("zero changes", result.stdout)
        self.assertEqual(self.apply_attempts.read_text(), "apply\n")
        self.assert_lock_released()

    def test_apply_refuses_resource_actions_outside_the_approved_target_and_intent(self):
        profile = "pilot-74"
        self.prepare_checkout(profile)
        self.register_existing_cluster()
        saved = self.saved_plan("just", profile, "managed")
        digest = hashlib.sha256(saved.read_bytes()).hexdigest()
        cluster_change = {
            "address": "kind_cluster.pilot",
            "mode": "managed",
            "provider_name": "registry.terraform.io/elioseverojunior/kind",
            "change": {"actions": ["no-op"], "after": {"name": self.env["FIXTURE_CLUSTER"]}},
        }
        unrelated_change = {**cluster_change, "address": "kind_cluster.unrelated"}
        for changes in (
            [],
            [cluster_change, unrelated_change],
            [unrelated_change],
            [{**cluster_change, "mode": "data"}],
            [{**cluster_change, "provider_name": "registry.example.invalid/other/kind"}],
            *(
                [{**cluster_change, "change": {**cluster_change["change"], "actions": actions}}]
                for actions in (["delete"], ["delete", "create"], ["create", "delete"], ["create"])
            ),
        ):
            self.env["FIXTURE_PLAN_RESOURCE_CHANGES"] = json.dumps(changes)
            for interface in ("just", "native"):
                with self.subTest(changes=changes, interface=interface):
                    if interface == "just":
                        result = self.apply(profile, "managed", saved, digest)
                    else:
                        result = self.native_apply("managed", saved, digest)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("unexpected resource changes", result.stderr)
                    self.assertFalse(self.apply_attempts.exists())
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

        self.env["FIXTURE_READINESS"] = "ready"
        result = self.native_verify(saved)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("zero changes", result.stdout)
        self.assertEqual(self.apply_attempts.read_text(), "apply\n")
        self.assert_lock_released()

    def test_native_verification_records_private_evidence_for_readiness_and_placement(self):
        profile = "pilot-74"
        self.prepare_checkout(profile)
        self.register_existing_cluster()
        saved = self.saved_plan("just", profile, "managed")
        digest = hashlib.sha256(saved.read_bytes()).hexdigest()
        result = self.native_apply("managed", saved, digest)
        self.assertEqual(result.returncode, 0, result.stderr)
        log = saved.parent / "verify.log"

        self.env["FIXTURE_READINESS"] = "fixture wait interrupted"
        result = self.native_verify(saved)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not all become Ready", result.stderr)
        self.assertIn("fixture wait interrupted", log.read_text())
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertFalse((saved.parent / "repeat.log").exists())

        self.env.update(FIXTURE_READINESS="ready", FIXTURE_INVENTORY="placement")
        result = self.native_verify(saved)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unexpected placement", result.stderr)
        self.assertIn(
            f"Selector workload=sut,perfeng.io/node-type=sut returned node/{self.env['FIXTURE_CLUSTER']}-worker",
            log.read_text(),
        )
        self.assertFalse((saved.parent / "repeat.log").exists())
        self.assert_lock_released()

    def test_verify_refuses_a_coherent_replacement_registration(self):
        profile = "pilot-74"
        self.prepare_checkout(profile)
        self.register_existing_cluster()
        saved = self.saved_plan("just", profile, "managed")
        digest = hashlib.sha256(saved.read_bytes()).hexdigest()
        self.env["FIXTURE_READINESS"] = "one node unready"
        result = self.apply(profile, "managed", saved, digest)
        self.assertIn("did not all become Ready", result.stderr)
        self.env["FIXTURE_READINESS"] = "ready"
        for uid, daemon in (("replacement-uid", "fixture-daemon"), ("fixture-uid", "other-daemon")):
            self.register_existing_cluster(uid=uid, daemon=daemon)
            self.env.update(FIXTURE_LIVE_UID=uid, FIXTURE_DAEMON=daemon)
            for interface in ("just", "native"):
                with self.subTest(interface=interface, uid=uid, daemon=daemon):
                    result = (
                        self.verify(profile, saved)
                        if interface == "just"
                        else self.native_verify(saved)
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("apply receipt differs from the registered target", result.stderr)
                    self.assertFalse((saved.parent / "repeat.log").exists())
                    self.assert_lock_released()

    def test_apply_and_verify_refuse_altered_or_extra_staged_inputs(self):
        profile = "pilot-74"
        self.prepare_checkout(profile)
        self.register_existing_cluster()
        saved = self.saved_plan("native", profile, "managed")
        digest = hashlib.sha256(saved.read_bytes()).hexdigest()
        for operation in ("apply", "verify"):
            if operation == "verify":
                self.env["FIXTURE_READINESS"] = "one node unready"
                result = self.apply(profile, "managed", saved, digest)
                self.assertIn("did not all become Ready", result.stderr)
                self.env["FIXTURE_READINESS"] = "ready"
                self.assertFalse((saved.parent / "repeat.log").exists())
            for filename, message in (
                ("main.tf", "differs from the reviewed source"),
                (".terraform.lock.hcl", "differs from the reviewed source"),
                ("target.auto.tfvars", "differs from the reviewed source"),
                ("source-revision.txt", "source revision does not match"),
                ("injected.tf", "Unexpected OpenTofu input"),
                ("injected.tofu", "Unexpected OpenTofu input"),
                ("injected.auto.tfvars", "Unexpected OpenTofu input"),
                ("injected.tf.json", "Unexpected OpenTofu input"),
                ("injected.tofu.json", "Unexpected OpenTofu input"),
                ("injected.auto.tfvars.json", "Unexpected OpenTofu input"),
            ):
                path = saved.parent / filename
                original = path.read_bytes() if path.exists() else None
                path.write_bytes(b"fixture account data must stay private\n")
                try:
                    for interface in ("just", "native"):
                        with self.subTest(
                            operation=operation, interface=interface, filename=filename
                        ):
                            if operation == "apply" and interface == "just":
                                result = self.apply(profile, "managed", saved, digest)
                            elif operation == "apply":
                                result = self.native_apply("managed", saved, digest)
                            elif interface == "just":
                                result = self.verify(profile, saved)
                            else:
                                result = self.native_verify(saved)
                            self.assertNotEqual(result.returncode, 0)
                            self.assertIn(message, result.stderr)
                            self.assertNotIn("fixture account data", result.stderr + result.stdout)
                            self.assertFalse((saved.parent / "repeat.log").exists())
                            self.assert_lock_released()
                finally:
                    if original is None:
                        path.unlink()
                    else:
                        path.write_bytes(original)
            if operation == "apply":
                self.assertFalse(self.apply_attempts.exists())

    def test_native_legacy_verification_requires_the_original_approved_digest(self):
        profile = "pilot-74"
        self.prepare_checkout(profile)
        self.register_existing_cluster()
        saved = self.saved_plan("native", profile, "managed")
        digest = hashlib.sha256(saved.read_bytes()).hexdigest()
        result = self.native_verify(saved)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("original approved digest", result.stderr)
        self.env["APPROVED_SHA256"] = digest
        result = self.native_verify(saved)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("zero changes", result.stdout)
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

    def test_create_refuses_to_read_a_uid_when_the_kubeconfig_differs_from_state(self):
        profile = "pilot-74"
        self.prepare_checkout(profile)
        for interface in ("just", "native"):
            for variable, wrong in (
                ("FIXTURE_STATE_ENDPOINT", "https://127.0.0.1:43210"),
                ("FIXTURE_STATE_CONFIG_SERVER", "https://127.0.0.1:43210"),
                ("FIXTURE_DEDICATED_CA", "b3RoZXItY2E="),
            ):
                with self.subTest(interface=interface, variable=variable):
                    saved = self.saved_plan("just", profile, "create")
                    digest = hashlib.sha256(saved.read_bytes()).hexdigest()
                    previous = self.env[variable]
                    self.env[variable] = wrong
                    if interface == "just":
                        result = self.apply(profile, "create", saved, digest)
                    else:
                        result = self.native_apply(
                            "create", saved, digest, "## Register a new cluster"
                        )
                    self.env[variable] = previous
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("saved kubeconfig differs", result.stderr)
                    self.assertFalse(self.uid_read.exists())
                    self.assertFalse((self.state / "kubeconfig").exists())
                    self.assertFalse((self.state / "identity.json").exists())
                    self.assert_lock_released()
                    (self.state / "cluster.tfstate").unlink()

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
        self.assertEqual(
            (saved.parent / "apply.status").read_text(), f"applied {digest} fixture-daemon\n"
        )
        result = self.verify(profile, saved)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no completed apply receipt", result.stderr)
        self.assertEqual(self.apply_attempts.read_text(), "apply\n")
        self.assert_lock_released()

        self.env.update(INTENT="create", APPROVED_PLAN=str(saved), FIXTURE_LIVE_UID="fixture-uid")
        result = self.native_operation(
            "### Locate the approved plan",
            "## Check the plan target",
            "## Register a new cluster",
            "## Verify the apply",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("zero changes", result.stdout)
        self.assertEqual(
            (saved.parent / "apply.status").read_text(),
            f"complete {digest} fixture-uid fixture-daemon\n",
        )
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
