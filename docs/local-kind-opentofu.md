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

## Select a registered target

Use a reviewed infrastructure checkout and the Operations account that owns
the registered state. On the qualified Linux AMD64 laptop the pinned tools
are in `~/.local/state/perfeng/kind-74/bin`. Use OpenTofu 1.13.10,
kind v0.31.0 and kubectl v1.35.0.

| Profile | Cluster | Private state directory |
| --- | --- | --- |
| `pilot-74` | `perfeng-iac-pilot-74` | `~/.local/state/perfeng/kind-74` |
| `retained-434` | `perfeng-retained-434` | `~/.local/state/perfeng/kind-434` |

The following commands select the disposable profile. Run them from the
reviewed infrastructure checkout. Inside the locked shell, set
`EXPECTED_DAEMON` from the Operations host registration. For a managed target,
set `EXPECTED_UID` from that target's private `identity.json`.

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
flock -w 15 "$STATE_DIR/operation.lock" bash --noprofile --norc
~~~

Use the Operations-owned mode `0700` state directory and mode `0600` state,
kubeconfig and lock files. Keep the tools and read-only mirror from the
registration. The mirror archive's SHA-256 is
`7c9d7a8817d0a3293e4a358d9ab88f16c69ef13c6479130e5a790c0b2d1a4e46`,
as recorded in the dependency lock. Check host capacity and the inotify limit
before creating a second cluster. The two-cluster #74 rehearsal needed an
inotify instance limit of 256.

The lock is shared with the earlier operator path. Hold it while verifying
identity and running a native operation. Exit the shell to release it while
a plan is in review. Reacquire it and repeat the target checks before applying.

## Save and review a plan

Create one private directory per plan. The files staged alongside the saved
plan are OpenTofu configuration and the dependency lock.

~~~sh
install -d -m 700 "$STATE_DIR/plans"
PLAN_DIR=$(mktemp -d "$STATE_DIR/plans/plan.XXXXXX")
install -m 600 "$SOURCE/main.tf" "$SOURCE/.terraform.lock.hcl" "$PLAN_DIR/"
install -m 600 "$SOURCE/profiles/$PROFILE/target.auto.tfvars" "$PLAN_DIR/"
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
test "$(tofu -chdir="$PLAN_DIR" console -no-color -state="$STATE_DIR/cluster.tfstate" <<< 'kind_cluster.pilot.name')" = "\"$CLUSTER\""
kubectl --kubeconfig "$STATE_DIR/kubeconfig" --context "kind-$CLUSTER" --request-timeout=10s \
  wait --for="jsonpath={.metadata.uid}=${EXPECTED_UID:?Set the registered kube-system UID}" --timeout=1s namespace/kube-system
~~~

Use a separate kubeconfig path for provider operations. Inspect the entire
readable plan. A fresh pilot has one create at `kind_cluster.pilot`. Match
the name, pinned image, one control plane, two labelled workers and loopback
API. An unchanged managed target reports zero changes.

~~~sh
KUBECONFIG="$PLAN_DIR/provider.kubeconfig" tofu -chdir="$PLAN_DIR" plan \
  -no-color -input=false -lock-timeout=30s -out="$PLAN_DIR/saved.tfplan"
tofu -chdir="$PLAN_DIR" show -no-color "$PLAN_DIR/saved.tfplan"
sha256sum "$PLAN_DIR/saved.tfplan"
~~~

Record the profile, source revision, absolute plan path and SHA-256 together
in the private review. Keep the plan binary, state and kubeconfigs on the
laptop. Use the path and digest from the approval for apply.

## Apply the reviewed file

In the locked shell, set `APPROVED_PLAN` and `APPROVED_SHA256` from that
review. Set `PLAN_DIR` to the approved file's directory. Check the daemon and
the vacant or managed target again using the commands above.

The command below refuses a redirected plan path or incorrect digest before
running OpenTofu. OpenTofu locks the embedded backend and refuses a stale
saved plan. Run the whole command together.

~~~sh
test "$(dirname -- "$PLAN_DIR")" = "$STATE_DIR/plans" &&
test "$APPROVED_PLAN" = "$PLAN_DIR/saved.tfplan" &&
test "$(realpath -e -- "$APPROVED_PLAN")" = "$APPROVED_PLAN" &&
printf '%s  %s\n' "${APPROVED_SHA256:?Set the approved digest}" "$APPROVED_PLAN" | sha256sum --check --strict - &&
KUBECONFIG="$PLAN_DIR/provider.kubeconfig" tofu -chdir="$PLAN_DIR" apply -no-color -input=false -lock-timeout=30s "$APPROVED_PLAN"
~~~

After a create, capture the new kubeconfig and read the live UID from the
expected context. Confirm the server is a loopback HTTPS URL before
registering the file. For a managed target, use its existing registration.

~~~sh
kind get kubeconfig --name "$CLUSTER" > "$PLAN_DIR/new.kubeconfig"
kubectl --kubeconfig "$PLAN_DIR/new.kubeconfig" --context "kind-$CLUSTER" config view --minify \
  -o 'jsonpath={.clusters[0].cluster.server}{"\n"}'
NEW_UID=$(kubectl --kubeconfig "$PLAN_DIR/new.kubeconfig" --context "kind-$CLUSTER" --request-timeout=10s \
  get namespace kube-system -o 'jsonpath={.metadata.uid}')
test -n "$NEW_UID" && test ! -e "$STATE_DIR/kubeconfig" && test ! -L "$STATE_DIR/kubeconfig" &&
install -m 600 "$PLAN_DIR/new.kubeconfig" "$STATE_DIR/kubeconfig"
~~~

Record `NEW_UID` and `EXPECTED_DAEMON` in the private target registration.

Run the repeat plan and check the node inventory. Exit code 0 from OpenTofu
is the zero-change result. All three expected nodes must be Ready.

~~~sh
KUBECONFIG="$PLAN_DIR/provider.kubeconfig" tofu -chdir="$PLAN_DIR" plan \
  -no-color -input=false -lock-timeout=30s -detailed-exitcode
kubectl --kubeconfig "$STATE_DIR/kubeconfig" --context "kind-$CLUSTER" get nodes
~~~

The [disposable rehearsal](kind-iac-rehearsal.md#github-hosted-rehearsal)
covers DNS, PVC read/write and the full create and teardown lifecycle.

## Bootstrap pinned Argo CD

Use the same operation lock and the UID from the private registration.
Check the namespace through the registered kubeconfig and exact context.
A previously installed namespace must have the infrastructure ownership
labels. Run the whole guarded command together.

~~~sh
KCFG="$STATE_DIR/kubeconfig"
CTX="kind-$CLUSTER"
OWNER_PATH='{.metadata.name}{" "}{.metadata.labels.app\.kubernetes\.io/name}{" "}{.metadata.labels.app\.kubernetes\.io/managed-by}{" "}{.metadata.labels.app\.kubernetes\.io/part-of}'

kubectl --kubeconfig "$KCFG" --context "$CTX" --request-timeout=10s \
  wait --for="jsonpath={.metadata.uid}=${EXPECTED_UID:?Set the registered kube-system UID}" --timeout=1s namespace/kube-system &&
ARGO_OWNER=$(kubectl --kubeconfig "$KCFG" --context "$CTX" --request-timeout=10s \
  get namespace argocd --ignore-not-found -o "jsonpath=$OWNER_PATH") &&
{ test -z "$ARGO_OWNER" || test "$ARGO_OWNER" = 'argocd argocd k8s-infrastructure kubernetes-substrate'; } &&
kubectl --kubeconfig "$KCFG" --context "$CTX" apply --server-side --field-manager=k8s-infrastructure -k "$OVERLAY"
~~~

~~~sh
kubectl --kubeconfig "$KCFG" --context "$CTX" -n argocd rollout status --timeout=240s \
  deployment/argocd-applicationset-controller deployment/argocd-dex-server \
  deployment/argocd-notifications-controller deployment/argocd-redis \
  deployment/argocd-repo-server deployment/argocd-server statefulset/argocd-application-controller
kubectl --kubeconfig "$KCFG" --context "$CTX" diff --server-side --field-manager=k8s-infrastructure -k "$OVERLAY"
~~~

The rollout checks and diff must exit successfully. Argo CD reconciles
consumer workload definitions from the consumer's GitOps source.

## Recover from an interrupted apply

Under the operation lock, compare `tofu state list`, Docker inventory for
the exact cluster name, and the live UID when the API is reachable. Preserve
the failed operation's evidence. If the provider cleaned up and state changed,
review a fresh plan. Containers without a matching state entry or a different
cluster UID need explicit recovery review before another write. Keep teardown
approval separate from create and bootstrap.
