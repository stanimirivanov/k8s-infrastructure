# Local kind deployment with OpenTofu

[#81](https://github.com/stanimirivanov/k8s-infrastructure/issues/81)
qualifies the procedure on the disposable pilot. Environment #434 has a
separate cutover.

## TL;DR

- Pick an operator task below and use the laptop's existing Docker daemon,
  pinned provider mirror and private state.
- Save a plan with native commands or `just plan`. After approval, apply and
  verify the exact file with the native commands or `just apply`. Either
  interface can resume verification of the original plan.
- Bootstrap the pinned Argo CD overlay with native commands or `just bootstrap`.
  Record success after rollouts, an empty diff and a final UID check.
- Use [GitHub-hosted CI](kind-iac-rehearsal.md#github-hosted-rehearsal) for
  automated disposable qualification.

## Choose an operator task

Planning and the `just` recipes acquire the target lock. For manual apply,
verification, bootstrap, or recovery, start with a
[locked session](#open-a-locked-session).

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
export PATH="$HOME/.local/state/perfeng/kind-74/bin:$PATH"
export DOCKER_HOST=unix:///var/run/docker.sock KIND_EXPERIMENTAL_PROVIDER=docker
export TF_CLI_CONFIG_FILE="$STATE_DIR/opentofu.tfrc" TF_WORKSPACE=default
unset DOCKER_CONTEXT TF_DATA_DIR TF_CLI_ARGS TF_CLI_ARGS_init \
  TF_CLI_ARGS_state TF_CLI_ARGS_state_list TF_CLI_ARGS_console TF_CLI_ARGS_plan TF_CLI_ARGS_show \
  TF_CLI_ARGS_apply TF_LOG TF_LOG_PATH TF_LOG_CORE TF_LOG_PROVIDER KUBECTL_EXTERNAL_DIFF
umask 077
if [[ -d "$STATE_DIR" && "$(realpath -e -- "$STATE_DIR")" == "$STATE_DIR" &&
      ! -L "$STATE_DIR/operation.lock" && ( ! -e "$STATE_DIR/operation.lock" || -f "$STATE_DIR/operation.lock" ) ]]; then
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
  STATE="$STATE_DIR/cluster.tfstate"
  BACKUP="$STATE.backup"
  PLANS="$STATE_DIR/plans"
  [[ ! -L "$STATE" && ( ! -e "$STATE" || -f "$STATE" ) &&
     ! -L "$BACKUP" && ( ! -e "$BACKUP" || -f "$BACKUP" ) &&
     ! -L "$PLANS" && ( ! -e "$PLANS" || -d "$PLANS" ) &&
     -f "$TF_CLI_CONFIG_FILE" && ! -L "$TF_CLI_CONFIG_FILE" ]] ||
    { printf '%s\n' 'The state, plan directory or CLI configuration is invalid or redirected.' >&2; exit 1; }
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

Use a checkout without Git `assume-unchanged` or `skip-worktree` flags,
which can hide local changes. Both paths require GNU `timeout`. The OpenTofu
deadlines are 120 seconds for initialization, 180 seconds for planning, and
60 seconds each for state listing, console checks, and rendering the plan.
A process still running five seconds after its deadline is killed. The plan
can wait up to 30 seconds for the OpenTofu state lock.

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
  INDEX_FLAGS=$(git ls-files -v | LC_ALL=C awk '/^[a-zS] / {print $1}')
  [[ -z "$INDEX_FLAGS" ]] ||
    refuse 'Use a clean, reviewed checkout without assume-unchanged or skip-worktree flags.'
  CHECKOUT_STATUS=$(git --no-optional-locks status --porcelain=v1 --untracked-files=all --ignore-submodules=none)
  [[ -z "$CHECKOUT_STATUS" && "$(git rev-parse --verify 'HEAD^{commit}')" == "$SOURCE_REVISION" ]] ||
    refuse 'Plan from a clean, reviewed checkout at a stable HEAD.'

  export PATH="$HOME/.local/state/perfeng/kind-74/bin:$PATH"
  export DOCKER_HOST=unix:///var/run/docker.sock KIND_EXPERIMENTAL_PROVIDER=docker
  export TF_CLI_CONFIG_FILE="$STATE_DIR/opentofu.tfrc" TF_WORKSPACE=default
  unset DOCKER_CONTEXT TF_DATA_DIR TF_CLI_ARGS TF_CLI_ARGS_init TF_CLI_ARGS_apply \
    TF_CLI_ARGS_state TF_CLI_ARGS_state_list TF_CLI_ARGS_console TF_CLI_ARGS_plan TF_CLI_ARGS_show \
    TF_LOG TF_LOG_PATH TF_LOG_CORE TF_LOG_PROVIDER
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
  run_tofu() {
    local deadline=$1
    shift
    timeout --verbose --kill-after=5s "$deadline" tofu -chdir="$PLAN_DIR" "$@"
  }
  run_tofu 120s init -no-color -input=false -lockfile=readonly -backend-config="path=$STATE"

  if [[ "$INTENT" == managed ]]; then
    STATE_RESOURCES=$(run_tofu 60s state list -state="$STATE")
    [[ "$STATE_RESOURCES" == kind_cluster.pilot ]] || refuse 'The registered state must contain only kind_cluster.pilot.'
    NAME_MATCH=$(run_tofu 60s console -no-color -state="$STATE" \
      <<< "kind_cluster.pilot.name == var.cluster_name && var.cluster_name == \"$CLUSTER\"")
    [[ "$NAME_MATCH" == true ]] || refuse 'The state or staged cluster name differs from the registration.'
    kubectl --kubeconfig "$KCFG" --context "$CTX" --request-timeout=10s \
      wait --for="jsonpath={.metadata.uid}=$EXPECTED_UID" --timeout=1s namespace/kube-system ||
      refuse 'The live kube-system UID could not be matched to the registration.'
  else
    if [[ -e "$STATE" ]]; then
      STATE_RESOURCES=$(run_tofu 60s state list -state="$STATE")
      [[ -z "$STATE_RESOURCES" ]] || refuse 'The selected state already contains resources.'
    fi
    NAME_MATCH=$(run_tofu 60s console -no-color -state="$STATE" <<< "var.cluster_name == \"$CLUSTER\"")
    [[ "$NAME_MATCH" == true ]] || refuse 'The staged cluster name differs from the registration.'
  fi

  PLAN="$PLAN_DIR/saved.tfplan"
  run_tofu 180s plan -no-color -input=false -lock-timeout=30s -out="$PLAN" 2>&1 |
    tee "$PLAN_DIR/plan.log"
  run_tofu 60s show -no-color "$PLAN" > "$PLAN_DIR/plan.txt"
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
A failed or timed-out `plan` command leaves `plan.log` for diagnosis. Use
a new plan directory when the review record is missing.

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

Use the absolute path, lowercase SHA-256, approver and UTC time from the
exact-plan approval. Use an approval time in `YYYY-MM-DDTHH:MM:SSZ` form.
Either interface accepts the plan directory produced by either planner.

For a managed pilot, run this from the checkout outside the native locked
shell. Set `APPROVED_PLAN`, `APPROVED_SHA256`, `APPROVED_BY` and
`APPROVED_AT` from the approval record.

~~~sh
just apply pilot-74 managed "$APPROVED_PLAN" "$APPROVED_SHA256" "$APPROVED_BY" "$APPROVED_AT"
~~~

For a create, use `create` in place of `managed` and pass the daemon ID
from the Operations host registration as `EXPECTED_DAEMON` in the recipe's
environment. The recipe holds the lock during apply, registration and
verification. An interrupted operation reports its phase and the private
evidence directory.

For native apply, open a [new locked session](#open-a-locked-session).
Set `INTENT` to `create` or `managed` and set the four approval variables
in that shell. For a create, set `EXPECTED_DAEMON` from the Operations
host registration.

### Locate the approved plan

Set `APPROVED_PLAN` to the approved absolute path. Use a checkout that
contains the reviewed Git commit. Both interfaces compare the staged
module, lock file and profile inputs byte for byte with that commit and
refuse additional OpenTofu inputs before apply or verification.

~~~sh
{
  PLAN_DIR=$(dirname -- "${APPROVED_PLAN:?Set the approved path}")
  [[ "$(dirname -- "$PLAN_DIR")" == "$STATE_DIR/plans" && "${PLAN_DIR##*/}" == plan.* &&
     "$APPROVED_PLAN" == "$PLAN_DIR/saved.tfplan" && -f "$APPROVED_PLAN" &&
     "$(realpath -e -- "$APPROVED_PLAN")" == "$APPROVED_PLAN" ]] ||
    { printf '%s\n' 'Use the canonical saved plan under the selected profile plans directory.' >&2; exit 1; }
  REVIEW="$PLAN_DIR/REVIEW.md"
  for input in main.tf .terraform.lock.hcl target.auto.tfvars source-revision.txt REVIEW.md; do
    [[ -f "$PLAN_DIR/$input" && ! -L "$PLAN_DIR/$input" ]]
  done
  if [[ "$PROFILE" == retained-434 ]]; then
    [[ -f "$PLAN_DIR/retained_override.tf" && ! -L "$PLAN_DIR/retained_override.tf" ]]
  fi
  [[ ! -L "$PLAN_DIR/provider.kubeconfig" && ! -L "$PLAN_DIR/.terraform" ]]
  grep -Fxq -- "- Profile $PROFILE" "$REVIEW"
  grep -Fxq -- "- Plan $APPROVED_PLAN" "$REVIEW"

  check_staged_inputs() {
    local revision index staged path source=clusters/local-kind/iac-pilot
    local profile_source="$source/profiles/$PROFILE"
    local inputs=("$source/main.tf" "$source/.terraform.lock.hcl" "$profile_source/target.auto.tfvars")
    local staged_inputs=(main.tf .terraform.lock.hcl target.auto.tfvars)
    if [[ "$PROFILE" == retained-434 ]]; then
      inputs+=("$profile_source/retained-lifecycle.tf")
      staged_inputs+=(retained_override.tf)
    fi
    revision=$(cat -- "$PLAN_DIR/source-revision.txt")
    [[ "$revision" =~ ^([0-9a-f]{40}|[0-9a-f]{64})$ &&
       "$(git rev-parse --verify "$revision^{commit}" 2>/dev/null)" == "$revision" ]] &&
      grep -Fxq -- "- Source revision $revision" "$REVIEW" ||
      { printf '%s\n' 'The staged source revision does not match the review or is unavailable.' >&2; exit 1; }
    for index in "${!inputs[@]}"; do
      staged="$PLAN_DIR/${staged_inputs[index]}"
      if [[ ! -f "$staged" || -L "$staged" ]] ||
         ! git archive --format=tar "$revision" -- "${inputs[index]}" |
           tar -xOf - "${inputs[index]}" | cmp -s -- "$staged" -; then
        printf 'Staged input %s differs from the reviewed source.\n' "${staged_inputs[index]}" >&2
        exit 1
      fi
    done
    for path in "$PLAN_DIR"/{*,.*}.{tf,tofu,tfvars,tf.json,tofu.json,tfvars.json}; do
      [[ -e "$path" || -L "$path" ]] || continue
      case "${path##*/}" in
        main.tf|target.auto.tfvars) continue ;;
        retained_override.tf) [[ "$PROFILE" == retained-434 ]] && continue ;;
      esac
      printf '%s\n' 'Unexpected OpenTofu input in the saved plan directory.' >&2
      exit 1
    done
  }
  check_staged_inputs

  verify_approved_digest() {
    local digest
    digest=$(sha256sum < "$APPROVED_PLAN")
    [[ "${digest%% *}" == "${APPROVED_SHA256:?Load the original approved digest}" ]] &&
      grep -Fxq -- "- SHA-256 $APPROVED_SHA256" "$REVIEW" ||
      { printf '%s\n' 'The saved plan or review differs from the approved SHA-256.' >&2; exit 1; }
  }
  record_apply_status() {
    local status=$1 receipt
    shift
    receipt=$(mktemp "$PLAN_DIR/apply.status.XXXXXX")
    {
      printf '%s %s' "$status" "$APPROVED_SHA256"
      if (( $# )); then printf ' %s' "$@"; fi
      printf '\n'
    } > "$receipt"
    mv -T -- "$receipt" "$PLAN_DIR/apply.status"
  }
}
~~~

### Run the saved apply

First run the [daemon check](#check-the-plan-target) and the
[vacant](#vacant-target) or [managed](#managed-target) checks in this shell.
Apply checks the saved plan's cluster name and permits only the pinned
provider's `kind_cluster.pilot`. A vacant target requires a create. A managed
target permits an update or no changes. Apply checks the SHA-256 again just
before executing the plan and uses the registered local state paths.
OpenTofu can wait up to 30 seconds for its backend lock and refuses stale
plans. The overall apply has a five-minute deadline with five seconds for
termination. The kind provider allows 180 seconds for cluster readiness.

~~~sh
{
  case "${INTENT:?Select create or managed}" in create|managed) ;; *) exit 1 ;; esac
  grep -Fxq -- "- Intent $INTENT" "$REVIEW"
  [[ -n "${APPROVED_BY//[[:space:]]/}" && "$APPROVED_BY" != *$'\n'* && "$APPROVED_BY" != *$'\r'* ]]
  [[ "$APPROVED_AT" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ &&
     "$(date -u -d "$APPROVED_AT" +%Y-%m-%dT%H:%M:%SZ)" == "$APPROVED_AT" ]]
  [[ ! -e "$PLAN_DIR/apply.status" && ! -L "$PLAN_DIR/apply.status" &&
     ! -e "$PLAN_DIR/apply.log" && ! -L "$PLAN_DIR/apply.log" ]] ||
    { printf '%s\n' 'An apply was already attempted for this plan. Use the runbook recovery steps.' >&2; exit 1; }
  KUBECONFIG="$PLAN_DIR/provider.kubeconfig" timeout --verbose --kill-after=5s 60s \
    tofu -chdir="$PLAN_DIR" show -json "$APPROVED_PLAN" |
    jq -e --arg cluster "$CLUSTER" --arg intent "$INTENT" '
      if .variables.cluster_name.value != $cluster then
        error("The saved plan names a different cluster.")
      elif (.resource_changes | type == "array" and length == 1) and
        (.resource_changes[0] |
          .address == "kind_cluster.pilot" and .mode == "managed" and
          .provider_name == "registry.terraform.io/elioseverojunior/kind" and
          .previous_address == null and .deposed == null and .change.importing == null and
          .change.after.name == $cluster and
          (if $intent == "create" then .change.actions == ["create"]
           else .change.actions == ["no-op"] or .change.actions == ["update"] end)) then
        true
      else error("The saved plan contains unexpected resource changes.") end
    ' >/dev/null
  verify_approved_digest
  printf '\n- Approved SHA-256 %s\n- Approver %s\n- Approval UTC %s\n' \
    "$APPROVED_SHA256" "$APPROVED_BY" "$APPROVED_AT" >> "$REVIEW"
  record_apply_status started
  verify_approved_digest
  if KUBECONFIG="$PLAN_DIR/provider.kubeconfig" timeout --verbose --kill-after=5s 300s \
    tofu -chdir="$PLAN_DIR" apply -no-color -input=false -lock-timeout=30s \
    -state="$STATE_DIR/cluster.tfstate" -state-out="$STATE_DIR/cluster.tfstate" \
    -backup="$STATE_DIR/cluster.tfstate.backup" "$APPROVED_PLAN" > "$PLAN_DIR/apply.log" 2>&1; then
    record_apply_status applied "$EXPECTED_DAEMON"
  else
    rc=$?
    printf 'Apply exited %s. Inspect the private log at %s.\n' "$rc" "$PLAN_DIR/apply.log" >&2
    exit "$rc"
  fi
  if [[ "$INTENT" == managed ]]; then
    check_plan_daemon
    check_managed_target
    record_apply_status complete "$EXPECTED_UID" "$EXPECTED_DAEMON"
  fi
}
~~~

The apply records `applied` in `apply.status` when OpenTofu succeeds. It
records `complete` with the plan SHA-256, UID and daemon ID after validating
the registration. After a create, [register the new cluster](#register-a-new-cluster)
to complete the receipt. For an existing registration, go directly to
[verify the apply](#verify-the-apply).

## Check the plan target

Run these checks in the same locked shell as the plan or apply. First
verify the registered Docker daemon ID.

~~~sh
{
  check_plan_daemon() {
    local daemon
    daemon=$(timeout --kill-after=5s 15s docker --host "$DOCKER_HOST" info --format '{{.ID}}')
    test "$daemon" = "${EXPECTED_DAEMON:?Set the registered Docker daemon ID}" ||
      { printf '%s\n' 'Docker daemon ID differs from the registration.' >&2; exit 1; }
  }
  check_plan_daemon
}
~~~

### Vacant target

The staged name must match the selected cluster. Both Docker queries must
return empty, existing state must list no resources, and the registration
paths must be vacant. Existing resources or registration files need
[recovery review](#recover-an-interrupted-operation).

~~~sh
{
  CLUSTER_NAME_MATCH=$(timeout --verbose --kill-after=5s 60s tofu -chdir="$PLAN_DIR" console -no-color -state="$STATE_DIR/cluster.tfstate" <<< "var.cluster_name == \"$CLUSTER\"")
  test "$CLUSTER_NAME_MATCH" = true
  [[ ! -e "$KCFG" && ! -L "$KCFG" && ! -e "$STATE_DIR/identity.json" && ! -L "$STATE_DIR/identity.json" ]] ||
    { printf '%s\n' 'The selected target already has a registration file.' >&2; exit 1; }
  CLUSTER_LABELS=$(timeout --kill-after=5s 15s docker --host "$DOCKER_HOST" ps -a --filter "label=io.x-k8s.kind.cluster=$CLUSTER" --format '{{.Names}}')
  CLUSTER_NAMES=$(timeout --kill-after=5s 15s docker --host "$DOCKER_HOST" ps -a --filter "name=^/$CLUSTER-" --format '{{.Names}}')
  test -z "$CLUSTER_LABELS$CLUSTER_NAMES" ||
    { printf '%s\n' 'The selected kind name is already in use.' >&2; exit 1; }
  if [[ -e "$STATE_DIR/cluster.tfstate" ]]; then
    STATE_RESOURCES=$(timeout --verbose --kill-after=5s 60s tofu -chdir="$PLAN_DIR" state list -state="$STATE_DIR/cluster.tfstate")
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
  check_managed_target() {
    local resources name_match
    resources=$(timeout --verbose --kill-after=5s 60s tofu -chdir="$PLAN_DIR" state list -state="$STATE_DIR/cluster.tfstate")
    test "$resources" = kind_cluster.pilot
    name_match=$(timeout --verbose --kill-after=5s 60s tofu -chdir="$PLAN_DIR" console -no-color -state="$STATE_DIR/cluster.tfstate" \
      <<< "kind_cluster.pilot.name == var.cluster_name && var.cluster_name == \"$CLUSTER\"")
    test "$name_match" = true
    kubectl --kubeconfig "$KCFG" --context "$CTX" --request-timeout=10s \
      wait --for="jsonpath={.metadata.uid}=${EXPECTED_UID:?Load the registered kube-system UID}" --timeout=1s namespace/kube-system
  }
  check_managed_target
}
~~~

## Register a new cluster

Continue in the locked shell after a successful create. Before reading the
UID, compare the dedicated kubeconfig's loopback API endpoint and TLS CA
with the kubeconfig stored by the provider. The state endpoint must also
match. Save the verified target's UID and Docker daemon ID in the private
registration.

~~~sh
{
  check_plan_daemon
  RECEIPT="$PLAN_DIR/apply.status"
  [[ -f "$RECEIPT" && ! -L "$RECEIPT" ]]
  STATUS=$(cat -- "$RECEIPT")
  [[ "$STATUS" =~ ^applied\ ([0-9a-f]{64})\ ([^[:space:]]+)$ && "${BASH_REMATCH[2]}" == "$EXPECTED_DAEMON" ]] ||
    { printf '%s\n' 'The plan has no successful apply receipt for this daemon. Review the recovery steps.' >&2; exit 1; }
  APPROVED_SHA256=${BASH_REMATCH[1]}
  verify_approved_digest
  grep -Fxq -- '- Intent create' "$REVIEW"
  for file in "$PLAN_DIR/kubeconfig" "$PLAN_DIR/identity.json"; do
    [[ ! -L "$file" && ( ! -e "$file" || -f "$file" ) ]]
  done
  if [[ ! -e "$PLAN_DIR/kubeconfig" ]]; then
    [[ ! -e "$PLAN_DIR/identity.json" && ! -e "$KCFG" && ! -L "$KCFG" &&
       ! -e "$STATE_DIR/identity.json" && ! -L "$STATE_DIR/identity.json" ]]
    timeout --kill-after=5s 30s kind get kubeconfig --name "$CLUSTER" > "$PLAN_DIR/kubeconfig"
  fi
  FIELDS='jsonpath={.clusters[0].cluster.server}{" "}{.clusters[0].cluster.certificate-authority-data}'
  if ! DEDICATED=$(timeout --kill-after=5s 15s kubectl --kubeconfig "$PLAN_DIR/kubeconfig" --context "$CTX" \
       config view --minify --raw -o "$FIELDS" 2>/dev/null) ||
     ! STATE_CONFIG=$(jq -er --arg cluster "$CLUSTER" '
        select(.resources | type == "array" and length == 1) | .resources[0] |
        select(.mode == "managed" and .type == "kind_cluster" and .name == "pilot" and .module == null and
          .provider == "provider[\"registry.terraform.io/elioseverojunior/kind\"]" and
          (.instances | type == "array" and length == 1)) | .instances[0] |
        select(.index_key == null and .deposed == null and .attributes.name == $cluster) |
        .attributes.kubeconfig | strings | select(length > 0)
      ' "$STATE_DIR/cluster.tfstate" 2>/dev/null |
      timeout --kill-after=5s 15s kubectl --kubeconfig /dev/stdin --context "$CTX" \
        config view --minify --raw -o "$FIELDS" 2>/dev/null) ||
     ! ENDPOINT=$(jq -er '.resources[0].instances[0].attributes.endpoint | strings' "$STATE_DIR/cluster.tfstate" 2>/dev/null); then
    printf '%s\n' 'The saved kubeconfig differs from the dedicated kind kubeconfig.' >&2
    exit 1
  fi
  read -r API_SERVER CA <<< "$DEDICATED"
  [[ "$API_SERVER" =~ ^https://127\.0\.0\.1:[0-9]+$ && -n "$CA" &&
     "$DEDICATED" == "$STATE_CONFIG" && "$API_SERVER" == "$ENDPOINT" ]] ||
    { printf '%s\n' 'The saved kubeconfig differs from the dedicated kind kubeconfig.' >&2; exit 1; }
  NEW_UID=$(kubectl --kubeconfig "$PLAN_DIR/kubeconfig" --context "$CTX" --request-timeout=10s \
    get namespace kube-system -o 'jsonpath={.metadata.uid}')
  test -n "$NEW_UID"
  if [[ -f "$PLAN_DIR/identity.json" ]]; then
    jq -e --arg daemon "$EXPECTED_DAEMON" --arg uid "$NEW_UID" \
      '.daemon == $daemon and .uid == $uid' "$PLAN_DIR/identity.json" >/dev/null
  else
    [[ ! -e "$KCFG" && ! -L "$KCFG" && ! -e "$STATE_DIR/identity.json" && ! -L "$STATE_DIR/identity.json" ]]
    jq -n --arg daemon "$EXPECTED_DAEMON" --arg uid "$NEW_UID" \
      '{daemon: $daemon, uid: $uid}' > "$PLAN_DIR/identity.json"
  fi
  MISSING=()
  for file in kubeconfig identity.json; do
    if [[ -e "$STATE_DIR/$file" || -L "$STATE_DIR/$file" ]]; then
      [[ -f "$STATE_DIR/$file" && ! -L "$STATE_DIR/$file" ]] &&
        cmp -s -- "$PLAN_DIR/$file" "$STATE_DIR/$file" ||
        { printf '%s\n' 'The installed registration differs from the staged evidence.' >&2; exit 1; }
    else
      MISSING+=("$PLAN_DIR/$file")
    fi
  done
  if (( ${#MISSING[@]} )); then install -m 600 -- "${MISSING[@]}" "$STATE_DIR/"; fi
  EXPECTED_UID="$NEW_UID"
  kubectl --kubeconfig "$KCFG" --context "$CTX" --request-timeout=10s \
    wait --for="jsonpath={.metadata.uid}=$EXPECTED_UID" --timeout=1s namespace/kube-system
  record_apply_status complete "$EXPECTED_UID" "$EXPECTED_DAEMON"
}
~~~

An incomplete registration goes to [recovery](#recover-an-interrupted-operation).

## Verify the apply

Use the locked shell from apply or [resume verification](#resume-verification)
in a new shell. The live UID must match. The exact three named
nodes must be Ready and have the expected placement labels. Keep the native
node inventory and require exit zero from the repeat plan. Both interfaces
record readiness, inventory and placement diagnostics in private `verify.log`.

~~~sh
{
  VERIFY_LOG="$PLAN_DIR/verify.log"
  for evidence in "$VERIFY_LOG" "$PLAN_DIR/nodes.txt" "$PLAN_DIR/repeat.log"; do
    [[ ! -L "$evidence" && ( ! -e "$evidence" || -f "$evidence" ) ]] ||
      { printf '%s\n' 'A verification evidence path is redirected or invalid.' >&2; exit 1; }
  done
  printf '\nChecking cluster identity and nodes\n' >> "$VERIFY_LOG"
  kubectl --kubeconfig "$KCFG" --context "$CTX" --request-timeout=10s \
    wait --for="jsonpath={.metadata.uid}=${EXPECTED_UID:?Load the registered kube-system UID}" \
    --timeout=1s namespace/kube-system >> "$VERIFY_LOG" 2>&1
  kubectl --kubeconfig "$KCFG" --context "$CTX" --request-timeout=90s wait --for=condition=Ready --timeout=90s \
    "node/$CLUSTER-control-plane" "node/$CLUSTER-worker" "node/$CLUSTER-worker2" >> "$VERIFY_LOG" 2>&1 ||
    { printf '%s\n' 'The required nodes did not all become Ready. See verify.log.' >&2; exit 1; }
  NODES=$(kubectl --kubeconfig "$KCFG" --context "$CTX" --request-timeout=10s \
    get nodes -o name 2>> "$VERIFY_LOG" | LC_ALL=C sort)
  printf 'Observed nodes:\n%s\n' "$NODES" >> "$VERIFY_LOG"
  test "$NODES" = "$(printf 'node/%s\n' "$CLUSTER-control-plane" "$CLUSTER-worker" "$CLUSTER-worker2")" ||
    { printf '%s\n' 'Unexpected node inventory. See verify.log.' >&2; exit 1; }
  check_node_placement() {
    local selected
    selected=$(kubectl --kubeconfig "$KCFG" --context "$CTX" --request-timeout=10s \
      get nodes -l "$1" -o name 2>> "$VERIFY_LOG")
    printf 'Selector %s returned %s\n' "$1" "$selected" >> "$VERIFY_LOG"
    test "$selected" = "node/$CLUSTER-$2" ||
      { printf 'Unexpected placement for node/%s-%s. See verify.log.\n' "$CLUSTER" "$2" >&2; exit 1; }
  }
  check_node_placement workload=control-plane,node-role.kubernetes.io/control-plane control-plane
  check_node_placement workload=performance-generator,perfeng.io/node-type=generator worker
  check_node_placement workload=sut,perfeng.io/node-type=sut worker2
  kubectl --kubeconfig "$KCFG" --context "$CTX" --request-timeout=10s \
    get nodes -L workload,perfeng.io/node-type 2>> "$VERIFY_LOG" | tee "$PLAN_DIR/nodes.txt"
  check_staged_inputs
  if KUBECONFIG="$PLAN_DIR/provider.kubeconfig" timeout --verbose --kill-after=5s 180s \
    tofu -chdir="$PLAN_DIR" plan -no-color -input=false -lock-timeout=30s \
    -state="$STATE_DIR/cluster.tfstate" -detailed-exitcode 2>&1 | tee -a "$PLAN_DIR/repeat.log"; then
    check_plan_daemon
    kubectl --kubeconfig "$KCFG" --context "$CTX" --request-timeout=10s \
      wait --for="jsonpath={.metadata.uid}=$EXPECTED_UID" --timeout=1s namespace/kube-system >> "$VERIFY_LOG" 2>&1
    printf '%s\n' 'Repeat plan reports zero changes.' | tee -a "$VERIFY_LOG"
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

After apply and registration complete, run
`just verify pilot-74 "$APPROVED_PLAN"` outside the native locked shell.
The recipe acquires a fresh lock and checks the shared completion receipt,
registered target and original staged inputs.

For native verification, open a new locked session and load the
registration. [Locate the original approved plan](#locate-the-approved-plan)
to restore `PLAN_DIR`. Both apply interfaces record a shared
`apply.status` receipt with the original SHA-256, kube-system UID and Docker
daemon ID. For older manual applies without a receipt,
confirm completion in the private evidence and set `APPROVED_SHA256` from
the original approval record. Use [recovery](#recover-an-interrupted-operation)
if the status of apply or registration is unclear.

~~~sh
{
  RECEIPT="$PLAN_DIR/apply.status"
  if [[ -e "$RECEIPT" || -L "$RECEIPT" ]]; then
    [[ -f "$RECEIPT" && ! -L "$RECEIPT" ]]
    STATUS=$(cat -- "$RECEIPT")
    [[ "$STATUS" =~ ^complete\ ([0-9a-f]{64})\ ([^[:space:]]+)\ ([^[:space:]]+)$ ]] ||
      { printf '%s\n' 'The saved plan has no completed apply receipt. Review the recovery steps.' >&2; exit 1; }
    APPROVED_SHA256=${BASH_REMATCH[1]}
    [[ "${BASH_REMATCH[2]}" == "$EXPECTED_UID" && "${BASH_REMATCH[3]}" == "$EXPECTED_DAEMON" ]] ||
      { printf '%s\n' 'The apply receipt differs from the registered target.' >&2; exit 1; }
  fi
  verify_approved_digest
}
~~~

Run the [daemon](#check-the-plan-target) and [managed target](#managed-target)
checks. Continue to [verify the apply](#verify-the-apply) with the original
staged inputs.

## Bootstrap Argo CD

Bootstrap installs or reapplies pinned Argo CD on a registered cluster.
Use a clean, reviewed infrastructure checkout. Rendering requires HTTPS
access to `raw.githubusercontent.com` for the pinned upstream Argo CD
manifest. Both routes stage the overlay from Git `HEAD`, render once and
keep the manifest and evidence in a new private `bootstrap.XXXXXX`
directory under the selected target.

### Bootstrap with just

Run from outside the target's locked shell. The recipe acquires the lock.

~~~bash
just bootstrap pilot-74
~~~

### Bootstrap with native commands

Open a [locked session](#open-a-locked-session), check the private paths
and load the managed registration. Run the complete block in that shell.

~~~sh
(
  refuse() {
    printf '%s\n' "$1" >&2
    exit 1
  }

  run_kubectl() {
    local deadline=$1 config=$2
    shift 2
    timeout --kill-after=5s "$deadline" kubectl --kubeconfig "$config" --context "$CTX" "$@"
  }

  check_uid() {
    local rc
    run_kubectl 15s "$KCFG" --request-timeout=10s \
      wait --for="jsonpath={.metadata.uid}=$EXPECTED_UID" --timeout=1s namespace/kube-system || {
      rc=$?
      printf '%s\n' 'The live kube-system UID could not be matched to the registration.' >&2
      return "$rc"
    }
  }

  check_bootstrap_manifest() {
    [[ -f "$MANIFEST" && ! -L "$MANIFEST" && "$(< "$RUN_DIR/SHA256SUMS")" == "$MANIFEST_CHECKSUM" ]] &&
      (cd "$RUN_DIR" && sha256sum --check --strict --status <<< "$MANIFEST_CHECKSUM") ||
      refuse 'The rendered Argo CD manifest differs from its recorded SHA-256.'
  }

  SOURCE_REVISION=$(git rev-parse --verify 'HEAD^{commit}')
  INDEX_FLAGS=$(git ls-files -v | LC_ALL=C awk '/^[a-zS] / {print $1}')
  [[ -z "$INDEX_FLAGS" ]] ||
    refuse 'Use a clean, reviewed checkout without assume-unchanged or skip-worktree flags.'
  CHECKOUT_STATUS=$(git --no-optional-locks status --porcelain=v1 --untracked-files=all --ignore-submodules=none)
  [[ -z "$CHECKOUT_STATUS" && "$(git rev-parse --verify 'HEAD^{commit}')" == "$SOURCE_REVISION" ]] ||
    refuse 'Use a clean, reviewed checkout at a stable HEAD to bootstrap.'

  RUN_DIR=$(mktemp -d "$STATE_DIR/bootstrap.XXXXXX")
  BOOTSTRAP_PHASE='source staging'
  trap '
    if (( $? != 0 )); then
      printf "Argo CD bootstrap stopped during %s. Private evidence is in %s.\n" "$BOOTSTRAP_PHASE" "$RUN_DIR" >&2
    fi
  ' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM

  OVERLAY=clusters/local-kind/perfeng-local/addons/argocd
  printf '%s\n' "$SOURCE_REVISION" > "$RUN_DIR/source-revision.txt"
  {
    timeout --kill-after=5s 30s git archive --format=tar "$SOURCE_REVISION" -- "$OVERLAY" |
      timeout --kill-after=5s 30s tar -xf - --no-same-owner --no-same-permissions -C "$RUN_DIR"
  } 2> "$RUN_DIR/render.log"

  BOOTSTRAP_PHASE=render
  MANIFEST="$RUN_DIR/argocd.yaml"
  timeout --kill-after=5s 120s kubectl kustomize "$RUN_DIR/$OVERLAY" \
    > "$MANIFEST" 2>> "$RUN_DIR/render.log"
  [[ -s "$MANIFEST" ]] || refuse 'The Argo CD render is empty.'
  MANIFEST_CHECKSUM=$(cd "$RUN_DIR" && sha256sum -- argocd.yaml)
  printf '%s\n' "$MANIFEST_CHECKSUM" > "$RUN_DIR/SHA256SUMS"

  BOOTSTRAP_PHASE='pre-apply identity'
  check_uid > "$RUN_DIR/identity.log" 2>&1

  BOOTSTRAP_PHASE='namespace ownership'
  OWNER_PATH='{.metadata.name}{" "}{.metadata.labels.app\.kubernetes\.io/name}{" "}{.metadata.labels.app\.kubernetes\.io/managed-by}{" "}{.metadata.labels.app\.kubernetes\.io/part-of}'
  ARGO_OWNER=$(run_kubectl 15s "$KCFG" --request-timeout=10s \
    get namespace argocd --ignore-not-found -o "jsonpath=$OWNER_PATH" 2>> "$RUN_DIR/identity.log")
  [[ -z "$ARGO_OWNER" || "$ARGO_OWNER" == 'argocd argocd k8s-infrastructure kubernetes-substrate' ]] ||
    refuse 'The argocd namespace lacks the infrastructure ownership labels.'

  BOOTSTRAP_PHASE=apply
  check_bootstrap_manifest
  run_kubectl 120s "$KCFG" --request-timeout=30s apply --server-side --field-manager=k8s-infrastructure \
    -f "$MANIFEST" > "$RUN_DIR/apply.log" 2>&1

  BOOTSTRAP_PHASE=rollout
  run_kubectl 250s "$KCFG" --request-timeout=240s -n argocd rollout status --timeout=240s \
    deployment/argocd-applicationset-controller deployment/argocd-dex-server \
    deployment/argocd-notifications-controller deployment/argocd-redis \
    deployment/argocd-repo-server deployment/argocd-server statefulset/argocd-application-controller \
    > "$RUN_DIR/rollout.log" 2>&1

  BOOTSTRAP_PHASE=diff
  check_bootstrap_manifest
  run_kubectl 120s "$KCFG" --request-timeout=30s diff --server-side --field-manager=k8s-infrastructure \
    -f "$MANIFEST" > "$RUN_DIR/diff.log" 2> "$RUN_DIR/diff.stderr.log" || {
    rc=$?
    printf 'Argo CD server-side diff exited %s (1 means changes, greater than 1 means error).\n' "$rc" >&2
    exit "$rc"
  }
  [[ ! -s "$RUN_DIR/diff.log" ]] || refuse 'The Argo CD server-side diff is not empty.'

  BOOTSTRAP_PHASE='final identity'
  check_uid >> "$RUN_DIR/identity.log" 2>&1

  BOOTSTRAP_PHASE='result recording'
  printf '# Argo CD bootstrap\n\n- Profile %s\n- Source revision %s\n- Manifest SHA-256 %s\n- All seven rollouts passed\n- Server-side diff is empty\n- Registered UID rechecked\n' \
    "$PROFILE" "$SOURCE_REVISION" "${MANIFEST_CHECKSUM%% *}" > "$RUN_DIR/RESULT.md.tmp"
  mv -T -- "$RUN_DIR/RESULT.md.tmp" "$RUN_DIR/RESULT.md"
  printf 'Argo CD bootstrap verified. Private evidence is in %s.\n' "$RUN_DIR"
)
~~~

An existing Argo CD namespace needs the exact infrastructure ownership
labels. Both paths check the registered UID and namespace before applying
the hashed file with the `k8s-infrastructure` server-side field manager.
They check all seven rollouts, verify an empty server-side diff of the
same hashed file, and recheck the registered UID before recording success.

Keep `source-revision.txt`, `argocd.yaml`, `SHA256SUMS`, the logs and
`RESULT.md` within private Operations evidence. The result includes the
manifest digest. To check the checksum later, run
`sha256sum --check --strict SHA256SUMS` inside the run directory. Exit the
locked shell after the native task completes. Argo CD reconciles consumer
workload definitions from the consumer's GitOps source.

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
- If `apply.status` records `started`, inspect the private `apply.log`, state
  and inventory before considering a new plan. An attempted plan requires
  recovery review. A fresh plan needs its own approval.
- If `apply.status` records `applied`, OpenTofu succeeded and the completion
  receipt is pending. To recover a create, open a locked session, locate
  the approved plan and check the registered daemon from the Operations
  host registration. Run the [registration procedure](#register-a-new-cluster)
  for that plan. It checks any staged UID against the live cluster and
  finishes installing the matching registration before recording completion.
  If writing the staged kubeconfig stopped before writing an identity or
  installing either file, move the incomplete kubeconfig to a private
  recovery subdirectory inside the plan directory before retrying.
  If a managed apply could not validate its registration, inspect the
  registration and original apply evidence before taking further action.
- If `apply.status` records `complete` and registration is present, resume
  verification of the same plan. Inspect `verify.log`, `nodes.txt` and
  `repeat.log` for any incomplete checks.
- If bootstrap lacks `RESULT.md`, use the phase reported on the terminal.
  Inspect `render.log` for staging and render, `identity.log` for UID and
  namespace queries, and `apply.log`, `rollout.log`, `diff.log` and
  `diff.stderr.log` for the later phases. An initial UID or ownership
  refusal occurs before apply. After an incomplete apply, confirm the live
  UID and namespace ownership, inspect the recorded failure and rerun from
  the reviewed checkout once the issue is resolved.

Containers without matching state or a different UID need explicit recovery
review before another write. Teardown requires separate approval.
