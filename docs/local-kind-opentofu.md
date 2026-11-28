# Local kind deployment with OpenTofu

[#81](https://github.com/stanimirivanov/k8s-infrastructure/issues/81)
qualifies the procedure on the disposable pilot. Environment #434 has a
separate cutover.

## TL;DR

- Pick an operator task below and use the laptop's existing Docker daemon,
  pinned provider mirror and private state.
- Save a plan with native commands or the optional `just plan` recipe.
  Review the plan, then open a locked shell to apply the exact approved
  file. Verify the cluster and the zero-change repeat plan.
- Bootstrap the pinned Argo CD overlay only after verifying the registered
  cluster UID. Record success after rollouts, an empty diff and a final UID check.
- Use [GitHub-hosted CI](kind-iac-rehearsal.md#github-hosted-rehearsal) for
  automated disposable qualification.

## Choose an operator task

Planning acquires the lock in the chosen procedure. For the other tasks,
start with [a locked session](#open-a-locked-session).

| Situation | Task | Completion evidence |
| --- | --- | --- |
| Plan a vacant pilot or a change to a managed cluster | [Save a plan for review](#save-a-plan-for-review) | Private plan path, readable plan, source revision and SHA-256 |
| Resume with an approved plan | [Apply the approved plan](#apply-the-approved-plan), [register a new cluster](#register-a-new-cluster) after a create, then [verify the apply](#verify-the-apply) | Apply log, exact Ready node inventory, and a zero-change repeat plan |
| The apply and registration finished but verification stopped | [Resume verification](#resume-verification) | Exact Ready node inventory and a zero-change repeat plan |
| Install or reapply Argo CD on a registered cluster | [Bootstrap Argo CD](#bootstrap-argo-cd) | Private result, manifest checksum, rollout and diff logs |
| An operation stopped or the expected evidence is missing | [Recover an interrupted operation](#recover-an-interrupted-operation) | Known state, containers and UID before another write |

## Open a locked session

Use a clean, reviewed infrastructure checkout and the Operations account
that owns the private target directory. On the qualified Linux AMD64 laptop,
use OpenTofu 1.13.10, kind v0.31.0, kubectl v1.35.0 and `jq`. Check host
capacity and the inotify limit before creating a second cluster. The
two-cluster #74 rehearsal needed an inotify instance limit of 256.

| Profile | Cluster | Private state directory |
| --- | --- | --- |
| `pilot-74` | `perfeng-iac-pilot-74` | `~/.local/state/perfeng/kind-74` |
| `retained-434` | `perfeng-retained-434` | `~/.local/state/perfeng/kind-434` |

The commands select the pilot. Use the retained profile only after its
cutover is approved, with the profile, cluster and directory from the table.

~~~sh
export PROFILE=pilot-74 CLUSTER=perfeng-iac-pilot-74
export STATE_DIR="$HOME/.local/state/perfeng/kind-74"
export OVERLAY="$PWD/clusters/local-kind/perfeng-local/addons/argocd"
export PATH="$HOME/.local/state/perfeng/kind-74/bin:$PATH"
export DOCKER_HOST=unix:///var/run/docker.sock KIND_EXPERIMENTAL_PROVIDER=docker
export TF_CLI_CONFIG_FILE="$STATE_DIR/opentofu.tfrc"
unset DOCKER_CONTEXT TF_WORKSPACE TF_DATA_DIR TF_CLI_ARGS TF_CLI_ARGS_init \
  TF_CLI_ARGS_state TF_CLI_ARGS_console TF_CLI_ARGS_plan TF_CLI_ARGS_show \
  TF_CLI_ARGS_apply KUBECTL_EXTERNAL_DIFF
umask 077
if [[ -d "$STATE_DIR" && "$(realpath -e -- "$STATE_DIR")" == "$STATE_DIR" && ! -L "$STATE_DIR/operation.lock" ]]; then
  flock -w 15 "$STATE_DIR/operation.lock" bash --noprofile --norc -e -o pipefail
else
  printf '%s\n' 'The registered state directory or lock path is missing or redirected.' >&2
  false
fi
~~~

Run each complete command block in the locked Bash shell. A command failure
ends that shell and releases the lock. `pipefail` makes a failed command
stop the operation even when `tee` records its output. Return to the
[recovery steps](#recover-an-interrupted-operation) if a task fails.

Use the registered tools and read-only provider mirror. The mirror archive's
SHA-256 is `7c9d7a8817d0a3293e4a358d9ab88f16c69ef13c6479130e5a790c0b2d1a4e46`,
as recorded in the dependency lock. Keep the private directory mode `0700`
and the state, kubeconfig, identity and lock files mode `0600`.

Check the private paths and set the dedicated kubeconfig and context.

~~~sh
{
  [[ ! -L "$STATE_DIR/cluster.tfstate" && ! -L "$STATE_DIR/plans" ]] ||
    { printf '%s\n' 'The state file or plan directory is redirected.' >&2; exit 1; }
  KCFG="$STATE_DIR/kubeconfig"
  CTX="kind-$CLUSTER"
}
~~~

For a managed target, load both identifiers from its registration. For a
vacant target, set `EXPECTED_DAEMON` from the Operations host registration
and continue to the selected task.

~~~sh
{
  [[ -f "$KCFG" && ! -L "$KCFG" && -f "$STATE_DIR/identity.json" && ! -L "$STATE_DIR/identity.json" ]] ||
    { printf '%s\n' 'The kubeconfig or identity record is missing or redirected.' >&2; exit 1; }
  EXPECTED_DAEMON=$(jq -er '.daemon | strings | select(length > 0)' "$STATE_DIR/identity.json")
  EXPECTED_UID=$(jq -er '.uid | strings | select(length > 0)' "$STATE_DIR/identity.json")
}
~~~

## Save a plan for review

Choose a registered profile from the table above and an explicit intent.
Use `create` for a vacant target and supply `EXPECTED_DAEMON` from the
Operations host registration. Use `managed` for a registered target with
state, a dedicated kubeconfig and `identity.json`.

Either planning path takes the target's `operation.lock`, archives the
selected tracked inputs from the clean, reviewed Git `HEAD`, and saves the
result in a new private `plans/plan.*` directory. It releases the lock on
exit so the plan can be reviewed separately from apply.

### Plan with just

After the [one-time installation](#install-just-on-linux-amd64), run `just`
from the checkout to list available recipes. To plan a vacant pilot after
setting `EXPECTED_DAEMON` from Operations, run this command.

~~~sh
EXPECTED_DAEMON="$EXPECTED_DAEMON" just plan pilot-74 create
~~~

For an existing pilot registration, run `just plan pilot-74 managed`.
Select `retained-434` only through the separate approved cutover.

### Plan with native commands

In Bash at the checkout root, set `PROFILE` to the registered profile and
`INTENT` to `create` or `managed`. For a vacant pilot, set
`PROFILE=pilot-74` and `INTENT=create`, and set `EXPECTED_DAEMON` from
Operations. Run the whole block.

~~~bash
(
  set -euo pipefail

  refuse() {
    printf '%s\n' "$1" >&2
    exit 1
  }

  PROFILE=${PROFILE:?Select a registered profile}
  INTENT=${INTENT:?Select create or managed}
  case "$PROFILE" in
    pilot-74)
      CLUSTER=perfeng-iac-pilot-74
      STATE_DIR="$HOME/.local/state/perfeng/kind-74"
      ;;
    retained-434)
      CLUSTER=perfeng-retained-434
      STATE_DIR="$HOME/.local/state/perfeng/kind-434"
      ;;
    *) refuse 'Unknown kind profile. Choose pilot-74 or retained-434.' ;;
  esac
  case "$INTENT" in
    create|managed) ;;
    *) refuse 'Plan intent must be create or managed.' ;;
  esac

  SOURCE_REVISION=$(git rev-parse --verify 'HEAD^{commit}')
  CHECKOUT_STATUS=$(git status --porcelain=v1 --untracked-files=all --ignore-submodules=none)
  [[ -z "$CHECKOUT_STATUS" && "$(git rev-parse --verify 'HEAD^{commit}')" == "$SOURCE_REVISION" ]] ||
    refuse 'Plan from a clean, reviewed checkout at a stable HEAD.'

  export PATH="$HOME/.local/state/perfeng/kind-74/bin:$PATH"
  export DOCKER_HOST=unix:///var/run/docker.sock KIND_EXPERIMENTAL_PROVIDER=docker
  export TF_CLI_CONFIG_FILE="$STATE_DIR/opentofu.tfrc"
  unset DOCKER_CONTEXT TF_WORKSPACE TF_DATA_DIR TF_CLI_ARGS TF_CLI_ARGS_init \
    TF_CLI_ARGS_state TF_CLI_ARGS_console TF_CLI_ARGS_plan TF_CLI_ARGS_show
  umask 077

  LOCK="$STATE_DIR/operation.lock"
  [[ -d "$STATE_DIR" && "$(realpath -e -- "$STATE_DIR")" == "$STATE_DIR" &&
     ! -L "$LOCK" && ( ! -e "$LOCK" || -f "$LOCK" ) ]] ||
    refuse 'The registered state directory or lock path is missing or redirected.'
  exec 9>>"$LOCK"
  flock -w 15 9 || refuse 'The target lock is busy. Use the runbook recovery steps.'

  STATE="$STATE_DIR/cluster.tfstate"
  PLANS="$STATE_DIR/plans"
  KCFG="$STATE_DIR/kubeconfig"
  IDENTITY="$STATE_DIR/identity.json"
  CTX="kind-$CLUSTER"
  [[ ! -L "$STATE" && ( ! -e "$STATE" || -f "$STATE" ) &&
     ! -L "$PLANS" && ( ! -e "$PLANS" || -d "$PLANS" ) ]] ||
    refuse 'The state file or plan directory is invalid or redirected.'
  [[ -f "$TF_CLI_CONFIG_FILE" && ! -L "$TF_CLI_CONFIG_FILE" ]] ||
    refuse 'The registered OpenTofu CLI configuration is missing or redirected.'

  if [[ "$INTENT" == managed ]]; then
    [[ -f "$KCFG" && ! -L "$KCFG" && -f "$IDENTITY" && ! -L "$IDENTITY" ]] ||
      refuse 'The kubeconfig or identity record is missing or redirected.'
    EXPECTED_DAEMON=$(jq -er '.daemon | strings | select(length > 0)' "$IDENTITY") ||
      refuse 'The identity record has no valid Docker daemon ID.'
    EXPECTED_UID=$(jq -er '.uid | strings | select(length > 0)' "$IDENTITY") ||
      refuse 'The identity record has no valid kube-system UID.'
  else
    : "${EXPECTED_DAEMON:?Set the registered Docker daemon ID}"
    [[ ! -e "$KCFG" && ! -L "$KCFG" && ! -e "$IDENTITY" && ! -L "$IDENTITY" ]] ||
      refuse 'The selected target already has a registration file.'
  fi

  DAEMON=$(timeout 15s docker --host "$DOCKER_HOST" info --format '{{.ID}}')
  [[ "$DAEMON" == "$EXPECTED_DAEMON" ]] || refuse 'Docker daemon ID differs from the registration.'
  if [[ "$INTENT" == create ]]; then
    CLUSTER_LABELS=$(timeout 15s docker --host "$DOCKER_HOST" ps -a \
      --filter "label=io.x-k8s.kind.cluster=$CLUSTER" --format '{{.Names}}')
    CLUSTER_NAMES=$(timeout 15s docker --host "$DOCKER_HOST" ps -a \
      --filter "name=^/$CLUSTER-" --format '{{.Names}}')
    [[ -z "$CLUSTER_LABELS$CLUSTER_NAMES" ]] || refuse 'The selected kind name is already in use.'
  fi

  # Archive only the tracked inputs belonging to this profile.
  SOURCE=clusters/local-kind/iac-pilot
  PROFILE_SOURCE="$SOURCE/profiles/$PROFILE"
  INPUTS=("$SOURCE/main.tf" "$SOURCE/.terraform.lock.hcl" "$PROFILE_SOURCE/target.auto.tfvars")
  if [[ "$PROFILE" == retained-434 ]]; then
    INPUTS+=("$PROFILE_SOURCE/retained-lifecycle.tf")
  fi
  install -d -m 700 "$PLANS"
  PLAN_DIR=$(mktemp -d "$PLANS/plan.XXXXXX")
  git archive --format=tar "$SOURCE_REVISION" -- "${INPUTS[@]}" |
    tar -xf - --strip-components=3 --no-same-owner --no-same-permissions -C "$PLAN_DIR"
  for INPUT in "${INPUTS[@]}"; do
    STAGED="$PLAN_DIR/${INPUT#"$SOURCE/"}"
    [[ -f "$STAGED" && ! -L "$STAGED" ]] || refuse 'A reviewed input is missing or redirected.'
    chmod 600 -- "$STAGED"
  done
  mv -- "$PLAN_DIR/profiles/$PROFILE/target.auto.tfvars" "$PLAN_DIR/target.auto.tfvars"
  if [[ "$PROFILE" == retained-434 ]]; then
    mv -- "$PLAN_DIR/profiles/$PROFILE/retained-lifecycle.tf" "$PLAN_DIR/retained_override.tf"
  fi
  rmdir -- "$PLAN_DIR/profiles/$PROFILE" "$PLAN_DIR/profiles"
  printf '%s\n' "$SOURCE_REVISION" > "$PLAN_DIR/source-revision.txt"
  export KUBECONFIG="$PLAN_DIR/provider.kubeconfig"
  tofu -chdir="$PLAN_DIR" init -no-color -input=false -lockfile=readonly -backend-config="path=$STATE"

  if [[ "$INTENT" == managed ]]; then
    STATE_RESOURCES=$(tofu -chdir="$PLAN_DIR" state list)
    [[ "$STATE_RESOURCES" == kind_cluster.pilot ]] || refuse 'The registered state must contain only kind_cluster.pilot.'
    NAME_MATCH=$(tofu -chdir="$PLAN_DIR" console -no-color -state="$STATE" \
      <<< "kind_cluster.pilot.name == var.cluster_name && var.cluster_name == \"$CLUSTER\"")
    [[ "$NAME_MATCH" == true ]] || refuse 'The state or staged cluster name differs from the registration.'
    kubectl --kubeconfig "$KCFG" --context "$CTX" --request-timeout=10s \
      wait --for="jsonpath={.metadata.uid}=$EXPECTED_UID" --timeout=1s namespace/kube-system ||
      refuse 'The live kube-system UID could not be matched to the registration.'
  else
    if [[ -e "$STATE" ]]; then
      STATE_RESOURCES=$(tofu -chdir="$PLAN_DIR" state list)
      [[ -z "$STATE_RESOURCES" ]] || refuse 'The selected state already contains resources.'
    fi
    NAME_MATCH=$(tofu -chdir="$PLAN_DIR" console -no-color -state="$STATE" <<< "var.cluster_name == \"$CLUSTER\"")
    [[ "$NAME_MATCH" == true ]] || refuse 'The staged cluster name differs from the registration.'
  fi

  PLAN="$PLAN_DIR/saved.tfplan"
  tofu -chdir="$PLAN_DIR" plan -no-color -input=false -lock-timeout=30s -out="$PLAN" 2>&1 |
    tee "$PLAN_DIR/plan.log"
  tofu -chdir="$PLAN_DIR" show -no-color "$PLAN" > "$PLAN_DIR/plan.txt"
  sha256sum -- "$PLAN" > "$PLAN_DIR/SHA256SUMS"
  read -r PLAN_SHA256 _ < "$PLAN_DIR/SHA256SUMS"
  cat "$PLAN_DIR/plan.txt" "$PLAN_DIR/SHA256SUMS"
  printf '# Plan review\n\n- Profile %s\n- Intent %s\n- Source revision %s\n- Plan %s\n- SHA-256 %s\n' \
    "$PROFILE" "$INTENT" "$SOURCE_REVISION" "$PLAN" "${PLAN_SHA256#\\}" > "$PLAN_DIR/REVIEW.md.tmp"
  mv -- "$PLAN_DIR/REVIEW.md.tmp" "$PLAN_DIR/REVIEW.md"
)
~~~

### Review the saved plan

Keep the staged root module, selected profile, provider lock and saved plan
fixed between review and apply. The plan directory contains the source
revision, readable `plan.txt`, checksum file `SHA256SUMS`, and `REVIEW.md`
with the profile, intent, revision, absolute saved-plan path and digest.
A failed plan leaves `plan.log` for diagnosis. Use a new plan directory
when the review record is missing.

Review the whole plan. An unchanged managed target reports zero changes.
For a fresh pilot, check each item:

- Exactly one create at `kind_cluster.pilot`
- The registered cluster name and pinned node image tag and digest
- One control plane and two workers with generator and SUT placement labels
- A loopback API at `127.0.0.1`

Record approval against the absolute plan path and SHA-256 after planning
releases the lock. Keep the plan, approval and state on the laptop.

### Install just on Linux AMD64

The optional recipe uses the [just 1.59.0 Linux AMD64 release](https://github.com/casey/just/releases/tag/1.59.0).
Run this once from the reviewed checkout root in Bash. The repository's
[checksum pin](../clusters/local-kind/iac-pilot/just-linux-amd64.sha256)
verifies the archive before extraction.

~~~bash
(
  set -euo pipefail
  [[ "$(uname -s)" == Linux && "$(uname -m)" == x86_64 ]]
  TOOL_DIR="$HOME/.local/share/perfeng/kind-tools/just-1.59.0/bin"
  CHECKSUM="$PWD/clusters/local-kind/iac-pilot/just-linux-amd64.sha256"
  ARCHIVE=just-1.59.0-x86_64-unknown-linux-musl.tar.gz
  DOWNLOAD=$(mktemp -d)
  trap 'rm -f -- "$DOWNLOAD/$ARCHIVE" "$DOWNLOAD/just"; rmdir -- "$DOWNLOAD"' EXIT
  cd -- "$DOWNLOAD"
  curl -fsSL --proto '=https' --max-time 60 \
    "https://github.com/casey/just/releases/download/1.59.0/$ARCHIVE" -o "$ARCHIVE"
  sha256sum --check --strict "$CHECKSUM"
  tar -xzf "$ARCHIVE" just
  mkdir -p -- "$TOOL_DIR"
  install -m 0755 just "$TOOL_DIR/just"
  "$TOOL_DIR/just" --version
)
~~~

Add the pinned tool directory to `PATH` in the operator shell.

~~~sh
export PATH="$HOME/.local/share/perfeng/kind-tools/just-1.59.0/bin:$PATH"
~~~

## Apply the approved plan

Open a [new locked session](#open-a-locked-session). Set `APPROVED_SHA256`,
`APPROVED_BY` and `APPROVED_AT` from the approval record, using its UTC
timestamp.

### Locate the approved plan

Set `APPROVED_PLAN` to the approved absolute path.

~~~sh
{
  PLAN_DIR=$(dirname -- "${APPROVED_PLAN:?Set the approved path}")
  test "$(dirname -- "$PLAN_DIR")" = "$STATE_DIR/plans"
  test "$APPROVED_PLAN" = "$PLAN_DIR/saved.tfplan"
  CANONICAL_PLAN=$(realpath -e -- "$APPROVED_PLAN")
  test "$CANONICAL_PLAN" = "$APPROVED_PLAN"
}
~~~

### Run the saved apply

Repeat the [daemon check](#check-the-plan-target) and the
[vacant](#vacant-target) or [managed](#managed-target) checks in this shell.
The SHA-256 check runs just before the saved-plan apply. OpenTofu waits up
to 30 seconds for the backend lock and refuses a stale plan. The pinned
provider configuration gives the kind cluster 180 seconds to become Ready.

~~~sh
{
  printf '%s  %s\n' "${APPROVED_SHA256:?Set the approved digest}" "$APPROVED_PLAN" | sha256sum --check --strict -
  printf '\n- Approved SHA-256 %s\n- Approver %s\n- Approval UTC %s\n' \
    "$APPROVED_SHA256" "${APPROVED_BY:?Set the approver}" "${APPROVED_AT:?Set the approval time}" >> "$PLAN_DIR/REVIEW.md"
  KUBECONFIG="$PLAN_DIR/provider.kubeconfig" tofu -chdir="$PLAN_DIR" apply \
    -no-color -input=false -lock-timeout=30s "$APPROVED_PLAN" 2>&1 | tee "$PLAN_DIR/apply.log"
}
~~~

After a create, [register the new cluster](#register-a-new-cluster). For an
existing registration, go directly to [verify the apply](#verify-the-apply).

## Check the plan target

Run these checks in the same locked shell as the plan or apply. First
verify the registered Docker daemon ID.

~~~sh
{
  DAEMON=$(docker --host "$DOCKER_HOST" info --format '{{.ID}}')
  test "$DAEMON" = "${EXPECTED_DAEMON:?Set the registered Docker daemon ID}" ||
    { printf '%s\n' 'Docker daemon ID differs from the registration.' >&2; exit 1; }
}
~~~

### Vacant target

The staged name must match the selected cluster. Both Docker queries must
return empty, existing state must list no resources, and the registration
paths must be vacant. Existing resources or registration files need
[recovery review](#recover-an-interrupted-operation).

~~~sh
{
  CLUSTER_NAME_MATCH=$(tofu -chdir="$PLAN_DIR" console -no-color -state="$STATE_DIR/cluster.tfstate" <<< "var.cluster_name == \"$CLUSTER\"")
  test "$CLUSTER_NAME_MATCH" = true
  [[ ! -e "$KCFG" && ! -L "$KCFG" && ! -e "$STATE_DIR/identity.json" && ! -L "$STATE_DIR/identity.json" ]] ||
    { printf '%s\n' 'The selected target already has a registration file.' >&2; exit 1; }
  CLUSTER_LABELS=$(docker --host "$DOCKER_HOST" ps -a --filter "label=io.x-k8s.kind.cluster=$CLUSTER" --format '{{.Names}}')
  CLUSTER_NAMES=$(docker --host "$DOCKER_HOST" ps -a --filter "name=^/$CLUSTER-" --format '{{.Names}}')
  test -z "$CLUSTER_LABELS$CLUSTER_NAMES" ||
    { printf '%s\n' 'The selected kind name is already in use.' >&2; exit 1; }
  if [[ -e "$STATE_DIR/cluster.tfstate" ]]; then
    STATE_RESOURCES=$(tofu -chdir="$PLAN_DIR" state list)
    test -z "$STATE_RESOURCES" ||
      { printf '%s\n' 'The selected state already contains resources.' >&2; exit 1; }
  fi
}
~~~

### Managed target

Require the single `kind_cluster.pilot` state entry, the staged cluster name
and the registered live UID. Use `tofu -chdir="$PLAN_DIR" state show
kind_cluster.pilot` for inspection.

~~~sh
{
  STATE_RESOURCES=$(tofu -chdir="$PLAN_DIR" state list)
  test "$STATE_RESOURCES" = kind_cluster.pilot
  STATE_NAME_MATCH=$(tofu -chdir="$PLAN_DIR" console -no-color -state="$STATE_DIR/cluster.tfstate" \
    <<< "kind_cluster.pilot.name == var.cluster_name && var.cluster_name == \"$CLUSTER\"")
  test "$STATE_NAME_MATCH" = true
  kubectl --kubeconfig "$KCFG" --context "$CTX" --request-timeout=10s \
    wait --for="jsonpath={.metadata.uid}=${EXPECTED_UID:?Load the registered kube-system UID}" --timeout=1s namespace/kube-system
}
~~~

## Register a new cluster

Continue in the locked shell after a successful create. The kubeconfig must
point to the loopback API. Save the observed UID and Docker daemon ID in the
private registration.

~~~sh
{
  kind get kubeconfig --name "$CLUSTER" > "$PLAN_DIR/kubeconfig"
  API_SERVER=$(kubectl --kubeconfig "$PLAN_DIR/kubeconfig" --context "$CTX" config view --minify \
    -o 'jsonpath={.clusters[0].cluster.server}')
  [[ "$API_SERVER" =~ ^https://127\.0\.0\.1:[0-9]+$ ]]
  NEW_UID=$(kubectl --kubeconfig "$PLAN_DIR/kubeconfig" --context "$CTX" --request-timeout=10s \
    get namespace kube-system -o 'jsonpath={.metadata.uid}')
  test -n "$NEW_UID"
  jq -n --arg daemon "${EXPECTED_DAEMON:?Set the registered Docker daemon ID}" --arg uid "$NEW_UID" \
    '{daemon: $daemon, uid: $uid}' > "$PLAN_DIR/identity.json"
  [[ ! -e "$KCFG" && ! -L "$KCFG" && ! -e "$STATE_DIR/identity.json" && ! -L "$STATE_DIR/identity.json" ]]
  install -m 600 "$PLAN_DIR/kubeconfig" "$PLAN_DIR/identity.json" "$STATE_DIR/"
  EXPECTED_UID="$NEW_UID"
}
~~~

An incomplete registration goes to [recovery](#recover-an-interrupted-operation).

## Verify the apply

Use the locked shell from apply or [resume verification](#resume-verification)
in a new shell. The live UID must match. The exact three named
nodes must be Ready and have the expected placement labels. Keep the native
node inventory and require exit zero from the repeat plan.

~~~sh
{
  kubectl --kubeconfig "$KCFG" --context "$CTX" --request-timeout=10s \
    wait --for="jsonpath={.metadata.uid}=${EXPECTED_UID:?Load the registered kube-system UID}" --timeout=1s namespace/kube-system
  kubectl --kubeconfig "$KCFG" --context "$CTX" --request-timeout=90s wait --for=condition=Ready --timeout=90s \
    "node/$CLUSTER-control-plane" "node/$CLUSTER-worker" "node/$CLUSTER-worker2"
  NODES=$(kubectl --kubeconfig "$KCFG" --context "$CTX" --request-timeout=10s get nodes -o name | LC_ALL=C sort)
  test "$NODES" = "$(printf 'node/%s\n' "$CLUSTER-control-plane" "$CLUSTER-worker" "$CLUSTER-worker2")"
  CONTROL_PLANE=$(kubectl --kubeconfig "$KCFG" --context "$CTX" --request-timeout=10s \
    get nodes -l workload=control-plane,node-role.kubernetes.io/control-plane -o name)
  test "$CONTROL_PLANE" = "node/$CLUSTER-control-plane"
  GENERATOR=$(kubectl --kubeconfig "$KCFG" --context "$CTX" --request-timeout=10s \
    get nodes -l workload=performance-generator,perfeng.io/node-type=generator -o name)
  test "$GENERATOR" = "node/$CLUSTER-worker"
  SUT=$(kubectl --kubeconfig "$KCFG" --context "$CTX" --request-timeout=10s \
    get nodes -l workload=sut,perfeng.io/node-type=sut -o name)
  test "$SUT" = "node/$CLUSTER-worker2"
  kubectl --kubeconfig "$KCFG" --context "$CTX" --request-timeout=10s \
    get nodes -L workload,perfeng.io/node-type | tee "$PLAN_DIR/nodes.txt"
  if KUBECONFIG="$PLAN_DIR/provider.kubeconfig" tofu -chdir="$PLAN_DIR" plan \
    -no-color -input=false -lock-timeout=30s -detailed-exitcode 2>&1 | tee "$PLAN_DIR/repeat.log"; then
    echo 'Repeat plan reports zero changes.'
  else
    rc=$?
    printf 'Repeat plan exited %s (2 means changes, 1 means error).\n' "$rc" >&2
    exit "$rc"
  fi
}
~~~

Exit the locked shell when verification is complete. The
[hosted disposable rehearsal](kind-iac-rehearsal.md#github-hosted-rehearsal)
also covers DNS, PVC read/write and teardown.

### Resume verification

After a completed apply and registration, open a new locked session and
load the registration. [Locate the original approved plan](#locate-the-approved-plan)
to restore `PLAN_DIR`, then run the [daemon](#check-the-plan-target) and
[managed target](#managed-target) checks. Return to [verify the apply](#verify-the-apply).
If the apply or registration status is unclear, use [recovery](#recover-an-interrupted-operation).

## Bootstrap Argo CD

Open a [locked session](#open-a-locked-session) and load the registration.
Render the pinned overlay once and keep its digest with the run evidence.

~~~sh
{
  RUN_DIR=$(mktemp -d "$STATE_DIR/bootstrap.XXXXXX")
  git rev-parse HEAD > "$RUN_DIR/source-revision.txt"
  kubectl kustomize "$OVERLAY" > "$RUN_DIR/argocd.yaml"
  test -s "$RUN_DIR/argocd.yaml"
  sha256sum "$RUN_DIR/argocd.yaml" > "$RUN_DIR/SHA256SUMS"
}
~~~

An existing Argo CD namespace must have the infrastructure ownership
labels. The result file is written after the manifest check, apply, all
seven rollouts, an empty diff and the final UID check.

~~~sh
{
  OWNER_PATH='{.metadata.name}{" "}{.metadata.labels.app\.kubernetes\.io/name}{" "}{.metadata.labels.app\.kubernetes\.io/managed-by}{" "}{.metadata.labels.app\.kubernetes\.io/part-of}'
  kubectl --kubeconfig "$KCFG" --context "$CTX" --request-timeout=10s \
    wait --for="jsonpath={.metadata.uid}=${EXPECTED_UID:?Load the registered kube-system UID}" --timeout=1s namespace/kube-system
  ARGO_OWNER=$(kubectl --kubeconfig "$KCFG" --context "$CTX" --request-timeout=10s \
    get namespace argocd --ignore-not-found -o "jsonpath=$OWNER_PATH")
  if [[ -n "$ARGO_OWNER" ]]; then
    test "$ARGO_OWNER" = 'argocd argocd k8s-infrastructure kubernetes-substrate' ||
      { printf '%s\n' 'The argocd namespace lacks the infrastructure ownership labels.' >&2; exit 1; }
  fi
  sha256sum --check --strict "$RUN_DIR/SHA256SUMS"
  kubectl --kubeconfig "$KCFG" --context "$CTX" --request-timeout=30s \
    apply --server-side --field-manager=k8s-infrastructure \
    -f "$RUN_DIR/argocd.yaml" 2>&1 | tee "$RUN_DIR/apply.log"
  kubectl --kubeconfig "$KCFG" --context "$CTX" --request-timeout=240s -n argocd rollout status --timeout=240s \
    deployment/argocd-applicationset-controller deployment/argocd-dex-server \
    deployment/argocd-notifications-controller deployment/argocd-redis \
    deployment/argocd-repo-server deployment/argocd-server statefulset/argocd-application-controller \
    2>&1 | tee "$RUN_DIR/rollout.log"
  kubectl --kubeconfig "$KCFG" --context "$CTX" --request-timeout=30s \
    diff --server-side --field-manager=k8s-infrastructure \
    -f "$RUN_DIR/argocd.yaml" 2>&1 | tee "$RUN_DIR/diff.log"
  kubectl --kubeconfig "$KCFG" --context "$CTX" --request-timeout=10s \
    wait --for="jsonpath={.metadata.uid}=$EXPECTED_UID" --timeout=1s namespace/kube-system
  SOURCE_REVISION=$(cat "$RUN_DIR/source-revision.txt")
  printf '# Argo CD bootstrap\n\n- Profile %s\n- Source revision %s\n- All seven rollouts passed\n- Server-side diff is empty\n- Registered UID rechecked\n' \
    "$PROFILE" "$SOURCE_REVISION" > "$RUN_DIR/RESULT.md"
}
~~~

Exit the locked shell when the task completes. Keep the manifest checksum,
logs and result with private Operations evidence. Argo CD reconciles
consumer workload definitions from the consumer's GitOps source.

## Recover an interrupted operation

If the lock is busy, identify the holder of the exact file.

~~~sh
lslocks --notruncate --output PID,COMMAND,PATH | grep -F -- "$STATE_DIR/operation.lock"
~~~

Exit your previous locked shell if it is idle. Let an active operation
finish and keep the lock file in place.

Open a new locked shell for inspection. Compare `tofu state list` in the
staged directory, Docker inventory for the exact kind name, and the live
UID when the API is reachable. Keep the plan and partial logs.

- If preparation stopped before `REVIEW.md`, use a new plan directory.
- If apply stopped, inspect state and inventory before deciding whether a
  fresh plan is needed. Obtain approval for a fresh plan if state changed.
- If the state entry or containers exist and either registration file is
  missing, preserve the plan directory and any staged kubeconfig and UID
  file. Review that partial registration before saving new identity files.
- If bootstrap lacks `RESULT.md`, check the terminal error and any apply,
  rollout and diff logs. An identity or ownership refusal appears before
  any apply log. Repeat bootstrap only after confirming the live UID and
  ownership.

Containers without matching state or a different UID need explicit recovery
review before another write. Teardown requires separate approval.
