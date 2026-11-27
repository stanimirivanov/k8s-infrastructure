# Local kind deployment with OpenTofu

[#81](https://github.com/stanimirivanov/k8s-infrastructure/issues/81)
qualifies the local procedure on the disposable pilot. Environment #434 has a
separate cutover.

## TL;DR

- Use the laptop's existing Docker daemon, pinned provider mirror and private
  OpenTofu backend. Keep the state path and resource address for each profile.
- Review a saved plan, then apply the selected file with its approved SHA-256.
- Verify the live cluster UID and Argo CD namespace ownership before applying
  the pinned Kustomize overlay.
- Run automated disposable qualification in [GitHub-hosted CI](kind-iac-rehearsal.md#github-hosted-rehearsal).

## Prerequisites

Use a clean, reviewed infrastructure checkout and the Operations account that owns
the registered state. On the qualified Linux AMD64 laptop the pinned tools
are in `~/.local/state/perfeng/kind-74/bin`. Use OpenTofu 1.13.10,
kind v0.31.0, kubectl v1.35.0 and `jq` for the private JSON registration.

Check host capacity and the inotify limit before creating a second cluster.
The two-cluster #74 rehearsal needed an inotify instance limit of 256.

## Select a registered target

| Profile | Cluster | Private state directory |
| --- | --- | --- |
| `pilot-74` | `perfeng-iac-pilot-74` | `~/.local/state/perfeng/kind-74` |
| `retained-434` | `perfeng-retained-434` | `~/.local/state/perfeng/kind-434` |

The following commands select the disposable profile. Run them from the
reviewed infrastructure checkout.

~~~sh
export PROFILE=pilot-74 CLUSTER=perfeng-iac-pilot-74
export STATE_DIR="$HOME/.local/state/perfeng/kind-74"
export SOURCE="$PWD/clusters/local-kind/iac-pilot"
export OVERLAY="$PWD/clusters/local-kind/perfeng-local/addons/argocd"
export PATH="$HOME/.local/state/perfeng/kind-74/bin:$PATH"
export DOCKER_HOST=unix:///var/run/docker.sock
export TF_CLI_CONFIG_FILE="$STATE_DIR/opentofu.tfrc"
unset DOCKER_CONTEXT TF_WORKSPACE TF_DATA_DIR TF_CLI_ARGS TF_CLI_ARGS_init TF_CLI_ARGS_plan TF_CLI_ARGS_apply KUBECTL_EXTERNAL_DIFF
umask 077
flock -w 15 "$STATE_DIR/operation.lock" bash --noprofile --norc -o pipefail
~~~

The lock starts Bash without startup files so aliases or functions from
your shell configuration cannot change the procedure. `pipefail` preserves
a command failure when its output is recorded with `tee`.

For a managed target, load the recorded identifiers in the locked shell.
Both reads must succeed before you continue.

~~~sh
EXPECTED_DAEMON=$(jq -er '.daemon | strings | select(length > 0)' "$STATE_DIR/identity.json") &&
EXPECTED_UID=$(jq -er '.uid | strings | select(length > 0)' "$STATE_DIR/identity.json")
~~~

For a vacant target, set `EXPECTED_DAEMON` from the Operations host registration.

Use the Operations-owned mode `0700` state directory and mode `0600` state,
kubeconfig, identity and lock files. Keep the tools and read-only mirror from the
registration. The mirror archive's SHA-256 is
`7c9d7a8817d0a3293e4a358d9ab88f16c69ef13c6479130e5a790c0b2d1a4e46`,
as recorded in the dependency lock.

The lock is shared with the earlier operator path. Hold it while verifying
identity and running a native operation. Exit the shell to release it while
a plan is in review. Reacquire it and repeat the target checks before applying.

## Save and review a plan

Create one private directory per plan. The reviewed root module is a single
`main.tf`. Each profile supplies `target.auto.tfvars`. Keep the staged
configuration, dependency lock, and saved plan fixed between review and apply.

~~~sh
install -d -m 700 "$STATE_DIR/plans" &&
PLAN_DIR=$(mktemp -d "$STATE_DIR/plans/plan.XXXXXX") &&
install -m 600 "$SOURCE/main.tf" "$SOURCE/.terraform.lock.hcl" "$SOURCE/profiles/$PROFILE/target.auto.tfvars" "$PLAN_DIR/" &&
git rev-parse HEAD > "$PLAN_DIR/source-revision.txt"
~~~

For the retained profile, stage the checked-in lifecycle rule as an
OpenTofu override. It preserves the existing `kind_cluster.pilot` address
and refuses replacement. Use that profile only after its cutover is approved.

~~~sh
install -m 600 "$SOURCE/profiles/retained-434/retained-lifecycle.tf" "$PLAN_DIR/retained_override.tf"
~~~

Initialize the staged profile with its canonical backend.

~~~sh
tofu -chdir="$PLAN_DIR" init -no-color -input=false -lockfile=readonly -backend-config="path=$STATE_DIR/cluster.tfstate"
~~~

Before planning or applying, check the Docker daemon and target. Stop if any
check fails.

~~~sh
test "$(docker --host "$DOCKER_HOST" info --format '{{.ID}}')" = "${EXPECTED_DAEMON:?Set the registered Docker daemon ID}"
docker --host "$DOCKER_HOST" ps -a --filter "label=io.x-k8s.kind.cluster=$CLUSTER" --format '{{.Names}}'
docker --host "$DOCKER_HOST" ps -a --filter "name=^/$CLUSTER-" --format '{{.Names}}'
~~~

For a create, confirm the exact kind name is vacant, including any container
with the cluster name as its prefix. Use `tofu -chdir="$PLAN_DIR" state list`
when state exists. For a create it must be empty, or absent on the first
registration. A managed target must have the single
`kind_cluster.pilot` state entry with the registered name. Also verify the
live UID using its dedicated kubeconfig and context.

~~~sh
test "$(tofu -chdir="$PLAN_DIR" console -no-color -state="$STATE_DIR/cluster.tfstate" <<< 'kind_cluster.pilot.name == var.cluster_name')" = true &&
kubectl --kubeconfig "$STATE_DIR/kubeconfig" --context "kind-$CLUSTER" --request-timeout=10s \
  wait --for="jsonpath={.metadata.uid}=${EXPECTED_UID:?Set the registered kube-system UID}" --timeout=1s namespace/kube-system
~~~

The console check evaluates the state name against the staged profile.
Use `tofu -chdir="$PLAN_DIR" state show kind_cluster.pilot` for inspection.

Use a separate kubeconfig path for provider operations. Save the readable
plan and checksum only after planning succeeds.

~~~sh
KUBECONFIG="$PLAN_DIR/provider.kubeconfig" tofu -chdir="$PLAN_DIR" plan \
  -no-color -input=false -lock-timeout=30s -out="$PLAN_DIR/saved.tfplan" &&
tofu -chdir="$PLAN_DIR" show -no-color "$PLAN_DIR/saved.tfplan" > "$PLAN_DIR/plan.txt" &&
sha256sum "$PLAN_DIR/saved.tfplan" > "$PLAN_DIR/SHA256SUMS" &&
SOURCE_REVISION=$(cat "$PLAN_DIR/source-revision.txt") &&
printf '# Plan review\n\n- Profile %s\n- Source revision %s\n- Plan %s\n' \
  "$PROFILE" "$SOURCE_REVISION" "$PLAN_DIR/saved.tfplan" > "$PLAN_DIR/REVIEW.md" &&
cat "$PLAN_DIR/plan.txt" "$PLAN_DIR/SHA256SUMS"
~~~

Review the full plan with its SHA-256 and record the approval against the
absolute plan path. An unchanged managed target reports zero changes. For
a fresh pilot, check each item:

- Exactly one create at `kind_cluster.pilot`
- The registered cluster name
- The pinned node image tag and digest
- One control plane and two workers with the generator and SUT placement labels
- A loopback API at `127.0.0.1`

Keep the plan binary, state, kubeconfigs and review files on the laptop.

## Apply the reviewed file

In the locked shell, set `APPROVED_PLAN` and `APPROVED_SHA256` from that
review. Also set `APPROVED_BY` and `APPROVED_AT` from the approval record,
using its UTC timestamp. Set `PLAN_DIR` to the approved file's directory.
Check the daemon and the vacant or managed target again using the commands above.

The command below refuses a redirected plan path or incorrect digest before
running OpenTofu. OpenTofu locks the embedded backend and refuses a stale
saved plan. Run the whole command together.

~~~sh
test "$(dirname -- "$PLAN_DIR")" = "$STATE_DIR/plans" &&
test "$APPROVED_PLAN" = "$PLAN_DIR/saved.tfplan" &&
test "$(realpath -e -- "$APPROVED_PLAN")" = "$APPROVED_PLAN" &&
printf '%s  %s\n' "${APPROVED_SHA256:?Set the approved digest}" "$APPROVED_PLAN" | sha256sum --check --strict - &&
printf '\n- Approved SHA-256 %s\n- Approver %s\n- Approval UTC %s\n' \
  "$APPROVED_SHA256" "${APPROVED_BY:?Set the approver}" "${APPROVED_AT:?Set the approval time}" >> "$PLAN_DIR/REVIEW.md" &&
KUBECONFIG="$PLAN_DIR/provider.kubeconfig" tofu -chdir="$PLAN_DIR" apply \
  -no-color -input=false -lock-timeout=30s "$APPROVED_PLAN" 2>&1 | tee "$PLAN_DIR/apply.log"
~~~

After a create, register the new kubeconfig and freshly observed UID. For a
managed target, continue to the repeat check with the loaded registration.

~~~sh
kind get kubeconfig --name "$CLUSTER" > "$PLAN_DIR/kubeconfig" &&
API_SERVER=$(kubectl --kubeconfig "$PLAN_DIR/kubeconfig" --context "kind-$CLUSTER" config view --minify \
  -o 'jsonpath={.clusters[0].cluster.server}') &&
[[ "$API_SERVER" =~ ^https://127\.0\.0\.1:[0-9]+$ ]] &&
NEW_UID=$(kubectl --kubeconfig "$PLAN_DIR/kubeconfig" --context "kind-$CLUSTER" --request-timeout=10s \
  get namespace kube-system -o 'jsonpath={.metadata.uid}') &&
test -n "$NEW_UID" &&
jq -n --arg daemon "${EXPECTED_DAEMON:?Set the registered Docker daemon ID}" --arg uid "$NEW_UID" \
  '{daemon: $daemon, uid: $uid}' > "$PLAN_DIR/identity.json" &&
[[ ! -e "$STATE_DIR/kubeconfig" && ! -L "$STATE_DIR/kubeconfig" && ! -e "$STATE_DIR/identity.json" && ! -L "$STATE_DIR/identity.json" ]] &&
install -m 600 "$PLAN_DIR/kubeconfig" "$PLAN_DIR/identity.json" "$STATE_DIR/" &&
EXPECTED_UID="$NEW_UID"
~~~

Run the repeat plan, recheck the registered UID, and inspect the node
inventory. All three expected nodes must be Ready.

~~~sh
if KUBECONFIG="$PLAN_DIR/provider.kubeconfig" tofu -chdir="$PLAN_DIR" plan \
  -no-color -input=false -lock-timeout=30s -detailed-exitcode 2>&1 | tee "$PLAN_DIR/repeat.log"; then
  echo 'Repeat plan reports zero changes.'
else
  rc=$?
  printf 'Repeat plan exited %s (2 means changes, 1 means error).\n' "$rc" >&2
  exit "$rc"
fi
kubectl --kubeconfig "$STATE_DIR/kubeconfig" --context "kind-$CLUSTER" --request-timeout=10s \
  wait --for="jsonpath={.metadata.uid}=${EXPECTED_UID:?Load the registered kube-system UID}" --timeout=1s namespace/kube-system &&
kubectl --kubeconfig "$STATE_DIR/kubeconfig" --context "kind-$CLUSTER" get nodes | tee "$PLAN_DIR/nodes.txt"
~~~

The [disposable rehearsal](kind-iac-rehearsal.md#github-hosted-rehearsal)
covers DNS, PVC read/write and the full create and teardown lifecycle.

## Bootstrap pinned Argo CD

Use the same operation lock and the UID from the private registration.
Render the pinned overlay once and keep its digest with the run evidence.

~~~sh
RUN_DIR=$(mktemp -d "$STATE_DIR/bootstrap.XXXXXX") &&
git rev-parse HEAD > "$RUN_DIR/source-revision.txt" &&
kubectl kustomize "$OVERLAY" > "$RUN_DIR/argocd.yaml" &&
test -s "$RUN_DIR/argocd.yaml" &&
sha256sum "$RUN_DIR/argocd.yaml" > "$RUN_DIR/SHA256SUMS"
~~~

Check the namespace through the registered kubeconfig and exact context.
A previously installed namespace must have the infrastructure ownership
labels. Run the whole guarded block together to record success after apply,
rollouts, and the diff have passed.

~~~sh
KCFG="$STATE_DIR/kubeconfig"
CTX="kind-$CLUSTER"
OWNER_PATH='{.metadata.name}{" "}{.metadata.labels.app\.kubernetes\.io/name}{" "}{.metadata.labels.app\.kubernetes\.io/managed-by}{" "}{.metadata.labels.app\.kubernetes\.io/part-of}'

kubectl --kubeconfig "$KCFG" --context "$CTX" --request-timeout=10s \
  wait --for="jsonpath={.metadata.uid}=${EXPECTED_UID:?Set the registered kube-system UID}" --timeout=1s namespace/kube-system &&
ARGO_OWNER=$(kubectl --kubeconfig "$KCFG" --context "$CTX" --request-timeout=10s \
  get namespace argocd --ignore-not-found -o "jsonpath=$OWNER_PATH") &&
{ test -z "$ARGO_OWNER" || test "$ARGO_OWNER" = 'argocd argocd k8s-infrastructure kubernetes-substrate'; } &&
sha256sum --check --strict "$RUN_DIR/SHA256SUMS" &&
kubectl --kubeconfig "$KCFG" --context "$CTX" apply --server-side --field-manager=k8s-infrastructure \
  -f "$RUN_DIR/argocd.yaml" 2>&1 | tee "$RUN_DIR/apply.log" &&
kubectl --kubeconfig "$KCFG" --context "$CTX" -n argocd rollout status --timeout=240s \
  deployment/argocd-applicationset-controller deployment/argocd-dex-server \
  deployment/argocd-notifications-controller deployment/argocd-redis \
  deployment/argocd-repo-server deployment/argocd-server statefulset/argocd-application-controller \
  2>&1 | tee "$RUN_DIR/rollout.log" &&
kubectl --kubeconfig "$KCFG" --context "$CTX" diff --server-side --field-manager=k8s-infrastructure \
  -f "$RUN_DIR/argocd.yaml" 2>&1 | tee "$RUN_DIR/diff.log" &&
SOURCE_REVISION=$(cat "$RUN_DIR/source-revision.txt") &&
printf '# Argo CD bootstrap\n\n- Profile %s\n- Source revision %s\n- All seven rollouts passed\n- Server-side diff is empty\n' \
  "$PROFILE" "$SOURCE_REVISION" > "$RUN_DIR/RESULT.md"
~~~

Keep the manifest checksum and result with the private Operations evidence.
Argo CD reconciles consumer workload definitions from the consumer's
GitOps source.

## Recover from an interrupted apply

If the operation lock is busy, identify the process holding the exact file.

~~~sh
lslocks --notruncate --output PID,COMMAND,PATH | grep -F -- "$STATE_DIR/operation.lock"
~~~

If it is your previous locked shell, exit that shell. If it belongs to an
active operation, let that operation finish. Keep the lock file in place.

Under the operation lock, compare `tofu state list`, Docker inventory for
the exact cluster name, and the live UID when the API is reachable. Preserve
the failed operation's evidence. If the provider cleaned up and state changed,
review a fresh plan. Containers without a matching state entry or a different
cluster UID need explicit recovery review before another write. Keep teardown
approval separate from create and bootstrap.
