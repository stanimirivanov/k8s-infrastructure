# Operate a registered kind target

## TL;DR

- Commit one declaration in the environment repository and reference its
  reviewed infrastructure pin.
- Operations uses a private registry shared by all kind targets on the Docker
  daemon. Register the target before making a plan.
- Save a private creation plan from the selected clean environment checkout. Obtain
  separate approval for its exact absolute path and SHA-256 before applying it.
- Use `verify` to finish identity registration, check baseline readiness, and
  record a zero-change repeat plan after OpenTofu confirms apply. Both entry
  points accept the same plans and receipts.
- Reconcile the pinned Argo CD overlay with `bootstrap`. For reviewed disposable
  targets, save and approve a separate encrypted plan before `destroy`.
- Use the qualification procedure on a fresh, disposable non-Perfeng target
  to test DNS, storage, repeat bootstrap, and verified teardown.
- Use the [reviewed legacy revision](https://github.com/stanimirivanov/k8s-infrastructure/tree/86ddee734de4903136459f7c6369d3610c1f7242)
  and its [runbook](https://github.com/stanimirivanov/k8s-infrastructure/blob/86ddee734de4903136459f7c6369d3610c1f7242/docs/local-kind-opentofu.md)
  for existing targets until their individual cutovers are reviewed.

## Declare a target

The environment owns the declaration and can share a provider pin among its
targets. Commit a JSON file such as `targets/sandbox.json`.

```json
{
  "profile": "kind-baseline",
  "clusterName": "stargazer-sandbox",
  "disposable": true,
  "providerPin": "deployments/infrastructure.json"
}
```

The provider pin is tracked in the same environment checkout. Its
`schemaVersion` is `1`, its `source.repository` is
`https://github.com/stanimirivanov/k8s-infrastructure.git`, and its
`source.revision` is the full reviewed commit ID. Existing pins can retain
their other fields. Obtain the exact commit locally in the infrastructure
checkout before operating. The baseline profile defines one control-plane node,
one worker, a loopback API endpoint, OpenTofu 1.13.10, kind provider 0.0.4,
and the pinned Kubernetes v1.35.0 node image. The only target-specific
settings are the cluster name and whether the environment declares the target
disposable. Use a lowercase cluster name of at most 49 characters, beginning
with a letter and ending with a letter or digit.

This procedure requires a pin whose baseline root uses `main.tf` and the
dependency lock. A pin with additional root OpenTofu or Terraform
configuration, including overrides and automatically loaded variable files,
requires a reviewed profile change.

## Reserve the target

On the Operations host, use Bash, Git, Docker, jq, GNU coreutils, and flock.
Prepare an absolute, canonical directory with mode `0700` and export its path
as `KIND_REGISTRATIONS`. All kind targets sharing a Docker daemon must use
this one Operations-owned registry. Choose an explicit lowercase
`owner/target` key such as `stargazer/sandbox` and keep the reviewed
environment checkout at a stable path. Put the candidate and dedicated paths
outside both checkouts and the registry, in private mode-`0700` directories.

Prepare a private mode-`0600` candidate with the target key, declaration
locator, explicit local Docker socket, and dedicated absolute paths.

```json
{
  "target": "stargazer/sandbox",
  "declaration": "targets/sandbox.json",
  "dockerHost": "unix:///var/run/docker.sock",
  "state": "/srv/operations/stargazer/sandbox/cluster.tfstate",
  "lock": "/srv/operations/stargazer/sandbox/operation.lock",
  "kubeconfig": "/srv/operations/stargazer/sandbox/kubeconfig"
}
```

Set `ENVIRONMENT` and `CANDIDATE` to the absolute paths to the chosen
checkout root and candidate. With the pinned Just binary from the
[installation procedure](local-kind-opentofu.md#install-just-on-linux-amd64),
run from the infrastructure checkout.

```bash
just register stargazer/sandbox "$ENVIRONMENT" "$CANDIDATE"
```

The operation checks the reviewed declaration and local provider pin, reads the
daemon ID from the selected Docker socket, and publishes
`$KIND_REGISTRATIONS/stargazer/sandbox.json` under the registry lock. The
registration records the checkout location, name, daemon ID, context, and
canonical paths. Repeating the same registration returns the existing file.
A conflict calls for reviewing the existing reservation and the candidate with
Operations before proceeding.

## Save a plan for review

Install the pinned OpenTofu and use the [qualified provider archive and
filesystem mirror](kind-iac-rehearsal.md#setup). Export `KIND_PROVIDER_MIRROR`
as the canonical absolute path to the mirror directory. Each attempt writes a
private CLI configuration that installs only the locked kind provider from
that mirror. The target lock stays held while the procedure checks the
registration, verifies the daemon, stages the inputs, and saves the plan.
Creation requires a reservation without a UID and vacancy of the registered
state, kubeconfig, backend sidecars, exact kind cluster label, and both baseline
container names. Once a UID is registered, use `verify` with the applied plan.
OpenTofu embeds prior state in a saved plan, and the kind state holds kubeconfig
credentials. Verification runs a repeat plan without writing a saved plan.

```bash
just plan stargazer/sandbox "$ENVIRONMENT"
```

The operation prints the private `REVIEW.md` path in
`$KIND_REGISTRATIONS/.plans/<owner>/<target>/plan.*`. Read `plan.txt` and the
review record together. The reviewed inputs are saved alongside
`registration.json` and `binding.json`. The latter binds both reviewed
commits plus the declaration, pin and registration digests. The saved
OpenTofu binary plan carries the SHA-256 of `binding.json`. In that directory,
`sha256sum -c SHA256SUMS` verifies the exact `saved.tfplan` bytes.

Keep the attempt in private Operations storage while review or an approved
apply needs it. After rejection, supersession, or completion of the approved
operation, have Operations confirm that the plan is no longer needed, hold the
registered target lock, and delete the exact `plan.*` attempt directory
identified by `REVIEW.md`. Use the printed private diagnostics path for a
failed attempt once diagnosis is complete. Retain only the required non-secret
review or diagnostic evidence in Operations storage and keep plan contents out
of Git and shared logs.

## Apply the exact approved plan and verify readiness

The approver reviews `REVIEW.md` and `plan.txt` and approves the absolute
`saved.tfplan` path and its SHA-256. Capture the approver name and UTC time
from the approval record. Set `PLAN`, `PLAN_SHA256`, `APPROVER`, and
`APPROVAL_UTC` to those values. Use the pinned kind and kubectl tools from the
[rehearsal setup](kind-iac-rehearsal.md#setup) and install `unzip` for inspecting
the saved plan archive.

```bash
just apply stargazer/sandbox "$PLAN" "$PLAN_SHA256" "$APPROVER" "$APPROVAL_UTC"
```

The apply procedure holds the target lock while checking the plan bytes,
reviewed commit snapshots, registration, daemon, and state paths. It checks
the plan archive's embedded module and provider lock against the reviewed
provider snapshot. The approved plan must use the locked provider with the
registered Docker socket and a loopback API endpoint. The pinned OpenTofu
1.13.10 `graph` command checks the saved plan against the current backend
state. The procedure reads the native JSON trace in memory and matches the
exact read, write, and backup paths. It checks the daemon and target vacancy
after that graph check, before recording the apply attempt. OpenTofu receives
the exact state paths explicitly and uses a fresh private directory for its
scratch kubeconfig. Plans from the initial registered-target planning procedure
remain compatible. The original reviewed commits must still be available in
the local checkouts. Resumed verification checks the state digest in the
successful apply receipt.

After OpenTofu confirms success, the procedure checks that the exact context,
loopback API endpoint, and CA in the Docker-derived kubeconfig match state.
It then installs the private kubeconfig and reads the live `kube-system` UID
through the dedicated path and context. First creation adds that UID to the
registration under the registry lock. Verification requires the registered UID
before and after the repeat plan.

Verification waits for the expected nodes to become Ready and checks that a
refreshed OpenTofu repeat plan reports zero changes. It then records a fresh
node inventory that matches the names and role labels from the saved plan
and shows each expected node as Ready. The separately approved live
qualification covers DNS and PVC read/write. If the identity or verification
phase stops after a confirmed apply, use the same plan.

```bash
just verify stargazer/sandbox "$PLAN"
```

## Bootstrap Argo CD from the reviewed provider pin

Review the environment commit, provider revision and the baseline profile's
[Argo CD overlay](../clusters/local-kind/kind-baseline/addons/argocd/kustomization.yaml)
before authorizing bootstrap. An Argo CD-only upgrade can use a reviewed provider
pin and bootstrap by itself. The registered `plan` and `apply` commands cover initial
creation. For prerequisite substrate changes on a target with a registered UID,
have Operations review a separate managed plan with the current owner. Bind the
plan to the registered state and lock, verify the daemon, dedicated kubeconfig
and UID before and after execution, and qualify the new profile before bootstrap.
For a change of owner, follow the separate [substrate cutover](architecture/substrate-capability.md#compatibility-and-first-import)
before switching the operating path. Bootstrap installs the provider's controllers.
The environment owns its Applications and workload credentials.

```bash
just bootstrap stargazer/sandbox "$ENVIRONMENT"
```

The command takes the target lock, checks the registered state and dedicated
kubeconfig, and checks the daemon and the live `kube-system` UID. It stages the
overlay from the exact pinned Git commit and renders it once. The render needs
access to the upstream Argo CD manifest at its pinned Git commit, and the cluster
needs access to the controller images. An absent `argocd` namespace is acceptable.
An existing namespace must have the provider's ownership labels. Before apply,
the command also checks that each existing object in the rendered inventory,
including cluster-scoped objects, has `infra.k8s-infrastructure.io/owner=registered-kind-argocd`.
Review a bootstrap ownership cutover separately for an existing installation with
unlabeled objects. The command applies the checked manifest with the
`k8s-infrastructure` server-side field manager.

The printed `bootstrapped.json` path identifies the private attempt. A successful
attempt has the rendered `argocd.yaml`, `SHA256SUMS`, apply and rollout logs, an
empty `diff.log`, and the final UID in the receipt. A second run produces its
own receipt with the same manifest digest and an empty diff when the pinned
inputs and target remain stable.

## Save and apply a separate destroy plan

Confirm that the current reviewed declaration marks the target `disposable: true`
and that destroying it may remove all cluster workloads and local volumes.
The registered target must have a verified live UID. Use the private
provider mirror selected for creation.

```bash
just plan-destroy stargazer/sandbox "$ENVIRONMENT"
```

Review the single expected `kind_cluster.baseline` deletion in `plan.txt` and
the target, revisions, intent, path, and SHA-256 in `REVIEW.md`. Both files are
in a private `destroy.*` directory, alongside the encrypted `saved.tfplan`.
The destroy binding includes the SHA-256 of the registered state used for
planning. The procedure checks the live identity and backend and saves no
plaintext plan or copy of the prior state. OpenTofu encrypts the plan with a
per-target key held separately under `$KIND_REGISTRATIONS/.destroy-keys/`.
Operations should retain the key privately for as long as it needs to open an
approved plan. The dedicated state, state backup, and kubeconfig stay private
under their registered paths. Review cleanup of those files separately from
node removal and registration retirement.

Obtain explicit destroy approval of the target key, absolute `DESTROY_PLAN`
path and exact `DESTROY_SHA256`. Set `DESTROY_APPROVER` and
`DESTROY_APPROVAL_UTC` from that approval. A changed reviewed environment,
state, or plan needs a fresh destroy plan and approval.

```bash
just destroy stargazer/sandbox "$DESTROY_PLAN" "$DESTROY_SHA256" "$DESTROY_APPROVER" "$DESTROY_APPROVAL_UTC"
```

Destroy verifies a private ciphertext snapshot against the exact approved digest.
OpenTofu inspects that snapshot, checks the backend, and applies the deletion after
the target binding, daemon, state digest, and live UID checks. The procedure records
the original approved path and removes the temporary snapshot on exit. The
procedure retires the registration only after checking the empty state and
the absence of the nodes by kind cluster label and exact baseline container
names. It prints the path of `retired-registration.json` in the private destroy
attempt, where `destroy-verified.json` retains the evidence. The original create
and bootstrap evidence remains in their attempt directories.

## Run with native commands

Use the same prerequisites, candidate, and paths. Start a Bash shell and
set its positional arguments for the chosen task. Set `PROVIDER_CHECKOUT`
to the absolute path of the infrastructure checkout root that contains the pinned
provider commit.

```bash
# Reserve the target.
set -- "$PROVIDER_CHECKOUT" register stargazer/sandbox "$ENVIRONMENT" "$CANDIDATE"
```

```bash
# Save a plan for the registered target.
set -- "$PROVIDER_CHECKOUT" plan stargazer/sandbox "$ENVIRONMENT"
```

```bash
# Apply the exact separately approved plan.
set -- "$PROVIDER_CHECKOUT" apply stargazer/sandbox "$PLAN" "$PLAN_SHA256" "$APPROVER" "$APPROVAL_UTC"
```

```bash
# Resume verification after a confirmed apply.
set -- "$PROVIDER_CHECKOUT" verify stargazer/sandbox "$PLAN"
```

```bash
# Reconcile the reviewed Argo CD overlay.
set -- "$PROVIDER_CHECKOUT" bootstrap stargazer/sandbox "$ENVIRONMENT"
```

```bash
# Save a separate encrypted destroy plan for review.
set -- "$PROVIDER_CHECKOUT" plan-destroy stargazer/sandbox "$ENVIRONMENT"
```

```bash
# Apply the exact separately approved destroy plan and verify retirement.
set -- "$PROVIDER_CHECKOUT" destroy stargazer/sandbox "$DESTROY_PLAN" "$DESTROY_SHA256" "$DESTROY_APPROVER" "$DESTROY_APPROVAL_UTC"
```

Run the complete block below after setting arguments for one task. The
subshell releases both types of lock when the task exits.

<!-- registered-kind-commands -->
```bash
(
  set +x
  set -euo pipefail
  umask 077
  unset RUNTIME EXPORTED_CONFIG TRACE_PLAN_BACKEND

  refuse() { printf '%s\n' "$1" >&2; exit 1; }
  digest() { sha256sum | cut -d ' ' -f 1; }
  private_dir() {
    [[ -d "$1" && -O "$1" && "$(realpath -e -- "$1")" == "$1" &&
       "$(stat -c '%a' -- "$1")" == 700 ]] || refuse "Use a canonical, private directory: $1"
  }
  outside_checkouts() {
    case "$1" in
      "$ENVIRONMENT"|"$ENVIRONMENT"/*|"$PROVIDER"|"$PROVIDER"/*)
        refuse 'Keep private Operations paths outside both checkouts.' ;;
    esac
  }
  private_file() {
    [[ -f "$1" && ! -L "$1" && -O "$1" && "$(stat -c '%a %h' -- "$1")" == '600 1' ]] ||
      refuse "Use a dedicated mode-0600 file: $1"
  }
  read_record() {
    jq -ces 'select(length == 1) | .[0] | objects' "$1"
  }
  # Prevent Git redirection or replacement objects from overriding the selected commit.
  unset GIT_DIR GIT_COMMON_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_NAMESPACE \
    GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES
  export GIT_NO_LAZY_FETCH=1
  git_at() {
    local checkout=$1
    shift
    timeout --kill-after=5s 30s git --no-replace-objects --literal-pathspecs \
      -C "$checkout" -c core.fsmonitor=false "$@"
  }
  stage_blob() {
    local checkout=$1 revision=$2 path=$3 entry
    entry=$(git_at "$checkout" ls-tree "$revision" -- "$path")
    [[ "$entry" == '100644 blob '* || "$entry" == '100755 blob '* ]] &&
      git_at "$checkout" cat-file blob "$revision:$path" ||
      refuse "A reviewed, regular Git input is required: $path"
  }
  clean_revision() {
    local revision flags status
    [[ "$(git_at "$ENVIRONMENT" rev-parse --show-toplevel)" == "$ENVIRONMENT" ]] ||
      refuse 'Select the environment checkout root.'
    revision=$(git_at "$ENVIRONMENT" rev-parse --verify 'HEAD^{commit}') || refuse 'The environment has no reviewed HEAD.'
    flags=$(git_at "$ENVIRONMENT" ls-files -v | LC_ALL=C awk '/^[a-zS] / {print $1}') || refuse 'Could not read the environment index.'
    status=$(git_at "$ENVIRONMENT" --no-optional-locks status --porcelain=v1 --untracked-files=all --ignore-submodules=none) ||
      refuse 'Could not check the environment checkout for modifications.'
    [[ -z "$flags$status" && "$(git_at "$ENVIRONMENT" rev-parse --verify 'HEAD^{commit}')" == "$revision" ]] ||
      refuse 'Use a clean, reviewed environment checkout at a stable HEAD.'
    printf '%s\n' "$revision"
  }
  record_paths() {
    # OpenTofu's local backend strips one leading dot from the state filename.
    jq -er '[.state, .lock, .kubeconfig, (.state + ".backup"),
      (.state | split("/") | .[-1] |= ("." + ltrimstr(".") + ".lock.info") | join("/"))]
      | select(length == (unique | length)) | .[]' <<< "$1" ||
      refuse 'The state, lock, kubeconfig, and backend sidecar paths must be distinct.'
  }
  canonical_private_path() {
    local path=$1 parent leaf
    leaf=${path##*/}
    [[ "$path" == /* && -n "$leaf" && "$leaf" != . && "$leaf" != .. ]] ||
      refuse 'State, target lock and kubeconfig need dedicated absolute paths.'
    parent=$(realpath -e -- "${path%/*}") || refuse 'Create the private parent directory first.'
    private_dir "$parent"
    path="$parent/$leaf"
    [[ "$path" != *[[:cntrl:]]* ]] || refuse 'Use paths without control characters.'
    outside_checkouts "$path"
    case "$path" in
      "$REGISTRY"|"$REGISTRY"/*)
        refuse 'Keep state, lock and kubeconfig outside the registry.' ;;
    esac
    if [[ -e "$path" || -L "$path" ]]; then private_file "$path"; fi
    printf '%s\n' "$path"
  }
  observe_daemon() {
    local daemon
    daemon=$(timeout --kill-after=5s 15s docker --host "$1" info --format '{{.ID}}') ||
      refuse 'Could not observe the selected Docker daemon.'
    [[ "$daemon" =~ ^[a-zA-Z0-9][a-zA-Z0-9:._-]*$ ]] || refuse 'Docker did not report a daemon ID.'
    printf '%s\n' "$daemon"
  }
  load_registration() {
    local file=$1 key=$2 row paths path
    private_file "$file"
    row=$(read_record "$file")
    jq -e --arg target "$key" '
      def text: strings | length > 0 and (test("[[:cntrl:]]") | not);
      (del(.kubeSystemUid) | keys == ["clusterName", "context", "daemonId", "declaration", "dockerHost",
        "environment", "kubeconfig", "lock", "schemaVersion", "state", "target"])
      and (if has("kubeSystemUid") then (.kubeSystemUid | strings | test("^[a-zA-Z0-9][a-zA-Z0-9-]*$")) else true end)
      and .schemaVersion == 1 and .target == $target
      and (.clusterName | strings | test("^[a-z]([a-z0-9-]{0,47}[a-z0-9])?$"))
      and .context == ("kind-" + .clusterName)
      and (.daemonId | strings | test("^[a-zA-Z0-9][a-zA-Z0-9:._-]*$"))
      and (.declaration | text) and (.dockerHost | text) and (.environment | text and startswith("/"))
      and ([.state, .lock, .kubeconfig] | all(.[]; text and startswith("/")))
      ' <<< "$row" >/dev/null || refuse "Invalid target registration: $file"
    paths=$(record_paths "$row")
    while IFS= read -r path; do
      [[ "$(canonical_private_path "$path")" == "$path" ]] ||
        refuse "A registered path is redirected: $path"
    done <<< "$paths"
    printf '%s\n' "$row"
  }

  path_exists() { [[ -e "$1" || -L "$1" ]]; }
  prepare_plan_root() {
    local directory
    for directory in "$REGISTRY/.plans" "$REGISTRY/.plans/$OWNER" "$PLAN_ROOT"; do
      mkdir -p -- "$directory"
      private_dir "$directory"
    done
  }
  lock_registry() {
    local directory_lock="$REGISTRY/.registry.lock"
    if path_exists "$directory_lock"; then private_file "$directory_lock"; fi
    exec 8>>"$directory_lock"
    flock -w 15 8 || refuse 'The registration directory lock is busy.'
  }
  check_daemon() {
    [[ "$(observe_daemon "$DOCKER_HOST")" == "$DAEMON" ]] ||
      refuse 'Docker daemon ID differs from the registration.'
  }
  check_vacancy() {
    local path
    while IFS= read -r path; do
      [[ "$path" == "$LOCK" ]] || ! path_exists "$path" ||
        refuse 'A create plan requires vacant state, kubeconfig and backend sidecar paths.'
    done <<< "$(record_paths "$INPUT")"
    check_nodes_absent 'The reserved kind name is already in use.'
  }
  check_nodes_absent() {
    local filter containers
    for filter in "label=io.x-k8s.kind.cluster=$CLUSTER" "name=^/$CLUSTER-(control-plane|worker)$"; do
      containers=$(timeout --kill-after=5s 15s docker --host "$DOCKER_HOST" ps -a \
        --filter "$filter" --format '{{.Names}}') || refuse 'Could not verify Docker node absence.'
      [[ -z "$containers" ]] || refuse "$1"
    done
  }
  run_tofu() {
    local deadline=$1
    shift
    local -a logging=() encryption=()
    if [[ ${TRACE_PLAN_BACKEND:-} == yes ]]; then logging=(TF_LOG=JSON TF_LOG_PROVIDER=OFF); fi
    if [[ "$INTENT" == destroy ]]; then
      check_destroy_key
      encryption=("TF_ENCRYPTION=$DESTROY_ENCRYPTION")
    fi
    # Use kind's single default kubeconfig path in the new private runtime or plan directory.
    timeout --verbose --kill-after=5s "$deadline" env -i PATH="$PATH" HOME="${RUNTIME:-$PLAN_DIR}" \
      DOCKER_HOST="$DOCKER_HOST" KIND_EXPERIMENTAL_PROVIDER=docker TF_CLI_CONFIG_FILE="$TOFU_CONFIG" \
      TF_DATA_DIR="${RUNTIME:-$PLAN_DIR/.terraform}" TF_WORKSPACE=default CHECKPOINT_DISABLE=1 \
      "${logging[@]}" "${encryption[@]}" tofu -chdir="$PLAN_DIR" "$@"
  }
  check_destroy_key() {
    private_file "$DESTROY_KEY"
    [[ "$(wc -c < "$DESTROY_KEY")" == 65 ]] && LC_ALL=C grep -aqxE '[a-f0-9]{64}' -- "$DESTROY_KEY" ||
      refuse 'The per-target destroy plan key has an invalid format.'
  }
  load_destroy_key() {
    local directory key
    for directory in "$REGISTRY/.destroy-keys" "$REGISTRY/.destroy-keys/$OWNER"; do
      if [[ "$OPERATION" == plan-destroy ]]; then mkdir -p -- "$directory"; fi
      private_dir "$directory"
    done
    DESTROY_KEY="$REGISTRY/.destroy-keys/$TARGET.key"
    if [[ "$OPERATION" == plan-destroy ]] && ! path_exists "$DESTROY_KEY"; then
      key=$(od -vAn -N32 -tx1 /dev/urandom | tr -d ' \n')
      [[ "$key" =~ ^[a-f0-9]{64}$ ]] || refuse 'Could not generate the per-target destroy plan key.'
      write_record "$DESTROY_KEY" "$key"
    fi
    check_destroy_key
    # Native OpenTofu reads the passphrase. Only an interpolation-safe path enters the child environment.
    printf -v DESTROY_ENCRYPTION 'key_provider "pbkdf2" "destroy" { passphrase = trimspace(file(base64decode("%s"))) }\nmethod "aes_gcm" "destroy" { keys = key_provider.pbkdf2.destroy }\nplan {\n  method = method.aes_gcm.destroy\n  enforced = true\n}\n' \
      "$(printf '%s' "$DESTROY_KEY" | base64 -w0)"
  }
  check_plan_shape() {
    run_tofu 60s show -json "$PLAN" |
      jq -ce --arg cluster "$CLUSTER" --arg binding "$BINDING_SHA256" --arg provider "$PROVIDER_ADDRESS" '
        select(.variables.cluster_name.value == $cluster and .variables.plan_binding_sha256.value == $binding)
        | select(.configuration.provider_config ==
            {kind: {name: "kind", full_name: $provider, version_constraint: "0.0.4"}})
        | select(any(.configuration.root_module.resources[]?;
            .address == "kind_cluster.baseline" and .provider_config_key == "kind"))
        | select((.resource_drift // []) == [] and (.deferred_changes // []) == [])
        | .resource_changes | select(type == "array" and length == 1) | .[0]
        | select(.address == "kind_cluster.baseline" and .mode == "managed" and .provider_name == $provider
          and .previous_address == null and .deposed == null and .change.importing == null
          and .change.after.name == $cluster
          and .change.after.networking.api_server_address == "127.0.0.1"
          and .change.actions == ["create"])
        | .change.after.node
        | select(map(.role) == ["control-plane", "worker"])
        | map({name: ($cluster + "-" + .role), role: .role,
               labels: ((.labels // {}) + (if .role == "control-plane"
                 then {"node-role.kubernetes.io/control-plane": ""} else {} end))})
    '
  }
  check_destroy_plan() {
    local sealed_plan=$1
    private_file "$sealed_plan"
    jq -es '
      def base64: strings | test("^[A-Za-z0-9+/]+={0,2}$");
      length == 1 and (.[0] |
        keys == ["encrypted_data", "encryption_version", "meta"] and .encryption_version == "v0"
        and (.encrypted_data | base64) and (.meta | keys == ["key_provider.pbkdf2.destroy"])
        and (.meta["key_provider.pbkdf2.destroy"] | base64))
    ' "$sealed_plan" >/dev/null 2>&1 || refuse 'The saved destroy plan must be an encrypted OpenTofu plan.'
    # Prior state in show JSON contains credentials. Only retain the result of checking the expected delete.
    run_tofu 60s show -json "$sealed_plan" 2>/dev/null |
      jq -e --arg cluster "$CLUSTER" --arg binding "$BINDING_SHA256" --arg provider "$PROVIDER_ADDRESS" '
        .variables.cluster_name.value == $cluster and .variables.plan_binding_sha256.value == $binding
        and .configuration.provider_config ==
          {kind: {name: "kind", full_name: $provider, version_constraint: "0.0.4"}}
        and (.configuration.root_module | (.module_calls // {}) == {} and (.outputs // {}) == {})
        and (.configuration.root_module.resources | length == 1 and
          .[0].address == "kind_cluster.baseline" and .[0].provider_config_key == "kind"
          and (.[0].provisioners // []) == [])
        and ((.resource_drift // []) == [] and (.deferred_changes // []) == [] and (.output_changes // {}) == {})
        and (.resource_changes | length == 1 and (.[0] |
          .address == "kind_cluster.baseline" and .mode == "managed" and .provider_name == $provider
          and .previous_address == null and .deposed == null and .change.importing == null
          and .change.actions == ["delete"] and .change.after == null
          and .change.before.name == $cluster and .change.before.networking.api_server_address == "127.0.0.1"
          and (.change.before.node | map(.role) == ["control-plane", "worker"])))
      ' >/dev/null 2>&1 || refuse 'OpenTofu did not save the expected bound baseline destroy plan.'
  }
  check_backend() {
    # OpenTofu 1.13.10 graph checks the embedded plan backend and state freshness read-only.
    local checked_plan=${1:-$PLAN} trace backend
    backend=$(printf 'backend/local: state manager for workspace "default" will:\n - read initial snapshot from %s\n - write new snapshots to %s\n - create any backup at %s.backup' \
      "$STATE" "$STATE" "$STATE")
    trace=$(TRACE_PLAN_BACKEND=yes run_tofu 60s graph -no-color -plan="$checked_plan" 2>&1 >/dev/null) ||
      refuse 'OpenTofu could not validate the saved backend and state. Check for a stale plan.'
    jq -eRn --arg backend "$backend" '
      [inputs | fromjson? | select(."@level" == "trace") | ."@message"] as $messages
      | ($messages | index("Meta.BackendForLocalPlan: instantiated backend of type *local.Local")) != null
        and ($messages | index($backend)) != null
    ' <<< "$trace" >/dev/null || refuse 'The saved plan backend differs from the registered local state.'
  }
  check_snapshot() {
    local path file
    for file in declaration.json provider-pin.json registration.json binding.json main.tf \
      .terraform.lock.hcl target.auto.tfvars.json opentofu.tfrc; do
      private_file "$PLAN_DIR/$file"
    done
    [[ "$(digest < "$PLAN_DIR/declaration.json")" == "$(jq -r .declarationSha256 <<< "$BINDING")" &&
       "$(digest < "$PLAN_DIR/provider-pin.json")" == "$(jq -r .providerPinSha256 <<< "$BINDING")" &&
       "$(digest < "$PLAN_DIR/registration.json")" == "$(jq -r .registrationSha256 <<< "$BINDING")" ]] ||
      refuse 'The saved registration or declaration snapshot differs from its binding.'
    stage_blob "$ENVIRONMENT" "$ENV_REVISION" "$DECLARATION_PATH" | cmp -s - "$PLAN_DIR/declaration.json" &&
      stage_blob "$ENVIRONMENT" "$ENV_REVISION" "$PIN_PATH" | cmp -s - "$PLAN_DIR/provider-pin.json" ||
      refuse 'The saved declaration or provider pin differs from the reviewed environment commit.'
    for file in "${PROFILE_FILES[@]}"; do
      stage_blob "$PROVIDER" "$PROVIDER_REVISION" "$MODULE/$file" | cmp -s - "$PLAN_DIR/$file" ||
        refuse 'The staged module differs from the reviewed provider commit.'
    done
    for path in "$PLAN_DIR"/* "$PLAN_DIR"/.[!.]*; do
      case "${path##*/}" in
        main.tf|target.auto.tfvars.json) ;;
        *.tf|*.tf.json|*.tofu|*.tofu.json|terraform.tfvars|terraform.tfvars.json|*.auto.tfvars|*.auto.tfvars.json)
          if path_exists "$path"; then refuse 'Unexpected configuration in the saved plan directory.'; fi ;;
      esac
    done
    jq -e --arg cluster "$CLUSTER" --arg binding "$BINDING_SHA256" '
      . == {cluster_name: $cluster, plan_binding_sha256: $binding}
    ' "$PLAN_DIR/target.auto.tfvars.json" >/dev/null || refuse 'The staged inputs differ from the saved binding.'
    local mirror
    mirror=$(jq -er --arg provider "$PROVIDER_ADDRESS" '
      .provider_installation[0].filesystem_mirror[0].path as $path
      | select(. == {provider_installation: [{filesystem_mirror: [{path: $path, include: [$provider]}]}]})
      | $path | strings
    ' "$TOFU_CONFIG") || refuse 'The saved CLI configuration must install only the locked kind provider from its mirror.'
    [[ -d "$mirror" && "$(realpath -e -- "$mirror")" == "$mirror" && "$mirror" != *[[:cntrl:]]* ]] ||
      refuse 'The saved provider mirror is redirected or unavailable.'
  }
  check_digest() {
    private_file "$PLAN"
    private_file "$PLAN_DIR/SHA256SUMS"
    [[ "$PLAN_SHA256" =~ ^[a-f0-9]{64}$ && "$(digest < "$PLAN")" == "$PLAN_SHA256" &&
       "$(cat -- "$PLAN_DIR/SHA256SUMS")" == "$PLAN_SHA256  saved.tfplan" ]] ||
      refuse 'The saved plan SHA-256 does not match the exact approved file.'
  }
  read_plan_archive() {
    timeout --kill-after=5s 15s env -i PATH="$PATH" unzip "$@"
  }
  check_plan_archive() {
    # OpenTofu 1.13.10 embeds the configuration and serializes its dependency lock.
    local members expected
    expected=$(printf '%s\n' .terraform.lock.hcl tfconfig/m-/main.tf tfconfig/modules.json tfplan tfschemas tfstate tfstate-prev)
    members=$(read_plan_archive -Z1 -- "$PLAN" | LC_ALL=C sort)
    [[ "$members" == "$expected" ]] || refuse 'The saved plan archive has unexpected or duplicate members.'
    read_plan_archive -p -- "$PLAN" tfconfig/m-/main.tf | cmp -s - "$PLAN_DIR/main.tf" ||
      refuse 'The embedded module differs from the staged and reviewed provider module.'
    read_plan_archive -p -- "$PLAN" tfconfig/modules.json |
      jq -es '. == [[{Key: "", Dir: "."}]]' >/dev/null ||
      refuse 'The saved plan archive does not contain a single root module.'
    read_plan_archive -p -- "$PLAN" .terraform.lock.hcl |
      cmp -s - <(sed '1s/by "terraform init"/by "tofu init"/' "$PLAN_DIR/.terraform.lock.hcl") ||
      refuse 'The embedded dependency lock differs from the reviewed provider lock.'
  }
  write_record() {
    local file=$1 contents=$2 temporary
    [[ -n "$contents" ]] || refuse 'Could not prepare the private operation record.'
    temporary=$(mktemp "${file%/*}/.record.XXXXXX")
    printf '%s\n' "$contents" > "$temporary"
    mv -T -- "$temporary" "$file"
    sync -f -- "$file"
  }
  operation_record() {
    jq -nS --arg target "$TARGET" --arg plan "$PLAN" --arg digest "$PLAN_SHA256" \
      --arg binding "$BINDING_SHA256" --arg daemon "$DAEMON" --arg state "$STATE" '
      {schemaVersion: 1, target: $target, plan: $plan, planSha256: $digest,
        bindingSha256: $binding, daemonId: $daemon, state: $state}'
  }
  evidence() {
    private_file "$1"
    read_record "$1"
  }
  may_write_log() {
    if path_exists "$1"; then private_file "$1"; fi
  }
  run_kubectl() {
    local deadline=$1 config=$2
    shift 2
    timeout --kill-after=5s "$deadline" env -i PATH="$PATH" HOME="$RUNTIME" \
      kubectl --kubeconfig "$config" --context "$CONTEXT" "$@"
  }
  config_identity() {
    run_kubectl 15s "$1" config view --minify --raw -o json 2>/dev/null |
      jq -ce --arg context "$CONTEXT" '
        select(.contexts | length == 1 and .[0].name == $context)
        | .contexts[0].context as $selected
        | select(.clusters | length == 1 and .[0].name == $selected.cluster)
        | select(.users | length == 1 and .[0].name == $selected.user
            and (.[0].user | keys == ["client-certificate-data", "client-key-data"]))
        | .clusters[0].cluster
        | select(.server | strings | test("^https://127\\.0\\.0\\.1:[0-9]+$"))
        | select(."certificate-authority-data" | strings | test("^[A-Za-z0-9+/]+={0,2}$"))
        | select((."insecure-skip-tls-verify" // false) == false and ."proxy-url" == null
                 and ."tls-server-name" == null and ."certificate-authority" == null)
        | {server, ca: ."certificate-authority-data"}
      '
  }
  state_attributes() {
    private_file "$STATE"
    jq -ce --arg cluster "$CLUSTER" --arg provider "provider[\"$PROVIDER_ADDRESS\"]" '
      select(.resources | type == "array" and length == 1) | .resources[0]
      | select(.mode == "managed" and .type == "kind_cluster" and .name == "baseline"
               and .module == null and .provider == $provider and (.instances | length == 1))
      | .instances[0] | select(.index_key == null and .deposed == null and .attributes.name == $cluster)
      | .attributes | select(.kubeconfig | strings | length > 0)
    ' "$STATE" || refuse 'The registered state must contain only the expected baseline kind cluster.'
  }
  check_config() {
    local attributes=$1 config=$2 state_config dedicated endpoint
    endpoint=$(jq -er '.endpoint | strings' <<< "$attributes")
    state_config=$(jq -er .kubeconfig <<< "$attributes" | config_identity /dev/stdin) &&
      dedicated=$(config_identity "$config") &&
      [[ "$state_config" == "$dedicated" && "$(jq -r .server <<< "$dedicated")" == "$endpoint" ]] ||
      refuse 'The dedicated kubeconfig context, endpoint or CA differs from the registered state.'
  }
  live_uid() {
    local uid
    uid=$(run_kubectl 15s "$KUBECONFIG_PATH" --request-timeout=10s \
      get namespace kube-system -o 'jsonpath={.metadata.uid}') || refuse 'Could not read the live kube-system UID.'
    [[ "$uid" =~ ^[a-zA-Z0-9][a-zA-Z0-9-]*$ ]] || refuse 'The live kube-system UID is invalid.'
    printf '%s\n' "$uid"
  }
  check_uid() {
    [[ "$(live_uid)" == "$EXPECTED_UID" ]] || refuse 'The live kube-system UID differs from the registration.'
  }
  check_managed_identity() {
    local attributes
    [[ -n "$EXPECTED_UID" ]] || refuse 'This operation requires a registered live kube-system UID.'
    check_daemon
    attributes=$(state_attributes)
    private_file "$KUBECONFIG_PATH"
    check_config "$attributes" "$KUBECONFIG_PATH"
    check_uid
  }
  checked_manifest() {
    local bytes
    private_file "$MANIFEST"
    private_file "$PLAN_DIR/SHA256SUMS"
    bytes=$(cat -- "$MANIFEST" && printf '.') || refuse 'Could not read the rendered Argo CD manifest.'
    bytes=${bytes%.}
    [[ "$(cat -- "$PLAN_DIR/SHA256SUMS")" == "$MANIFEST_SHA  argocd.yaml" &&
       "$(printf '%s' "$bytes" | digest)" == "$MANIFEST_SHA" ]] ||
      refuse 'The rendered Argo CD manifest differs from its recorded SHA-256.'
    # Preserve the hashed bytes, even if the file changes while kubectl reads its input.
    printf '%s' "$bytes"
  }
  bootstrap_argocd() {
    local file ownership owner_path manifest_bytes
    check_managed_identity
    mkdir -m 700 -- "$PLAN_DIR/overlay"
    for file in kustomization.yaml namespace.yaml redis-pull-policy.yaml; do
      stage_blob "$PROVIDER" "$PROVIDER_REVISION" "$MODULE/addons/argocd/$file" > "$PLAN_DIR/overlay/$file"
    done
    MANIFEST="$PLAN_DIR/argocd.yaml"
    timeout --kill-after=5s 120s env -i PATH="$PATH" HOME="$RUNTIME" \
      kubectl kustomize "$PLAN_DIR/overlay" > "$MANIFEST" 2> "$PLAN_DIR/render.log"
    [[ -s "$MANIFEST" ]] || refuse 'The Argo CD render is empty.'
    MANIFEST_SHA=$(digest < "$MANIFEST")
    printf '%s  argocd.yaml\n' "$MANIFEST_SHA" > "$PLAN_DIR/SHA256SUMS"

    owner_path='{.metadata.name}{" "}{.metadata.labels.app\.kubernetes\.io/name}{" "}{.metadata.labels.app\.kubernetes\.io/managed-by}{" "}{.metadata.labels.app\.kubernetes\.io/part-of}'
    ownership=$(run_kubectl 15s "$KUBECONFIG_PATH" --request-timeout=10s \
      get namespace argocd --ignore-not-found -o "jsonpath=$owner_path" 2> "$PLAN_DIR/ownership.log")
    [[ -z "$ownership" || "$ownership" == 'argocd argocd k8s-infrastructure kubernetes-substrate' ]] ||
      refuse 'The argocd namespace lacks the infrastructure ownership labels.'
    manifest_bytes=$(checked_manifest && printf '.')
    # Project only the ownership label, including for Secret and cluster-scoped objects.
    ownership=$(printf '%s' "${manifest_bytes%.}" | run_kubectl 90s "$KUBECONFIG_PATH" --request-timeout=10s \
      get -f - --ignore-not-found --no-headers -o 'custom-columns=OWNER:.metadata.labels.infra\.k8s-infrastructure\.io/owner' \
      2>/dev/null && printf '.') || refuse 'Could not verify existing Argo CD object ownership.'
    printf '%s' "${ownership%.}" | awk '$0 != "registered-kind-argocd" { exit 1 }' ||
      refuse 'An existing Argo CD object lacks the registered baseline ownership label.'
    check_managed_identity
    printf '%s' "${manifest_bytes%.}" | run_kubectl 120s "$KUBECONFIG_PATH" --request-timeout=30s \
      apply --server-side --field-manager=k8s-infrastructure -f - > "$PLAN_DIR/apply.log" 2>&1

    run_kubectl 250s "$KUBECONFIG_PATH" --request-timeout=240s -n argocd rollout status --timeout=240s \
      deployment/argocd-applicationset-controller deployment/argocd-dex-server \
      deployment/argocd-notifications-controller deployment/argocd-redis \
      deployment/argocd-repo-server deployment/argocd-server statefulset/argocd-application-controller \
      > "$PLAN_DIR/rollout.log" 2>&1 || refuse 'An expected Argo CD rollout failed. See rollout.log.'

    manifest_bytes=$(checked_manifest && printf '.')
    printf '%s' "${manifest_bytes%.}" | run_kubectl 120s "$KUBECONFIG_PATH" --request-timeout=30s \
      diff --server-side --field-manager=k8s-infrastructure -f - > "$PLAN_DIR/diff.log" 2> "$PLAN_DIR/diff.stderr.log" ||
      refuse 'The Argo CD server-side diff found changes or failed. See diff.log and diff.stderr.log.'
    [[ ! -s "$PLAN_DIR/diff.log" ]] || refuse 'The Argo CD server-side diff is not empty.'
    checked_manifest >/dev/null
    check_managed_identity
    write_record "$PLAN_DIR/bootstrapped.json" "$(jq -nS --arg target "$TARGET" \
      --arg environment "$ENV_REVISION" --arg provider "$PROVIDER_REVISION" --arg daemon "$DAEMON" \
      --arg uid "$EXPECTED_UID" --arg manifest "$MANIFEST_SHA" \
      '{schemaVersion: 1, target: $target, profile: "kind-baseline", environmentRevision: $environment,
        providerRevision: $provider, daemonId: $daemon, kubeSystemUid: $uid, manifestSha256: $manifest,
        controllerRollouts: 7, emptyServerSideDiff: true}')"
    printf '%s\n' "$PLAN_DIR/bootstrapped.json"
  }
  record_identity() {
    local attributes uid observation recorded
    check_daemon
    attributes=$(state_attributes)
    if ! path_exists "$KUBECONFIG_PATH"; then
      [[ -z "$EXPECTED_UID" && ! -e "$PLAN_DIR/identity-observed.json" ]] ||
        refuse 'The registered kubeconfig is missing.'
      EXPORTED_CONFIG=$(mktemp "${KUBECONFIG_PATH%/*}/.kind-config.XXXXXX")
      timeout --kill-after=5s 30s env -i PATH="$PATH" HOME="$RUNTIME" DOCKER_HOST="$DOCKER_HOST" \
        KIND_EXPERIMENTAL_PROVIDER=docker kind get kubeconfig --name "$CLUSTER" > "$EXPORTED_CONFIG"
      check_config "$attributes" "$EXPORTED_CONFIG"
      mv -T -- "$EXPORTED_CONFIG" "$KUBECONFIG_PATH"
      EXPORTED_CONFIG=''
      sync -f -- "$KUBECONFIG_PATH"
    fi
    private_file "$KUBECONFIG_PATH"
    check_config "$attributes" "$KUBECONFIG_PATH"
    uid=$(live_uid)
    [[ -z "$EXPECTED_UID" || "$EXPECTED_UID" == "$uid" ]] ||
      refuse 'The live kube-system UID differs from the registration.'
    observation=$(jq -cS --arg uid "$uid" '. + {kubeSystemUid: $uid}' <<< "$APPLIED")
    if path_exists "$PLAN_DIR/identity-observed.json"; then
      recorded=$(evidence "$PLAN_DIR/identity-observed.json")
      [[ "$(jq -cS . <<< "$recorded")" == "$observation" ]] ||
        refuse 'The observed live identity differs from the original apply evidence.'
    else
      [[ -z "$EXPECTED_UID" ]] || refuse 'The original UID observation is missing.'
      write_record "$PLAN_DIR/identity-observed.json" "$observation"
    fi
    if [[ -z "$EXPECTED_UID" ]]; then
      (
        lock_registry
        [[ "$(load_registration "$REGISTRATION" "$TARGET")" == "$INPUT" ]] ||
          refuse 'The registration changed before the live UID could be recorded.'
        write_record "$REGISTRATION" "$(jq -cS --arg uid "$uid" '. + {kubeSystemUid: $uid}' <<< "$INPUT")"
      )
    fi
    EXPECTED_UID=$uid
  }
  verify_plan() {
    local file nodes observed attributes rc
    local -a expected=()
    for file in verify.log nodes.json repeat.log verified.json; do may_write_log "$PLAN_DIR/$file"; done
    mapfile -t expected < <(jq -r '.[] | "node/" + .name' <<< "$TOPOLOGY")
    run_kubectl 100s "$KUBECONFIG_PATH" --request-timeout=90s wait --for=condition=Ready --timeout=90s \
      "${expected[@]}" >> "$PLAN_DIR/verify.log" 2>&1 || refuse 'The required baseline nodes did not become Ready.'
    check_snapshot
    if run_tofu 180s plan -no-color -input=false -lock-timeout=30s -state="$STATE" -detailed-exitcode \
      >> "$PLAN_DIR/repeat.log" 2>&1; then
      check_daemon
      attributes=$(state_attributes)
      [[ "$(digest < "$STATE")" == "$(jq -r .stateSha256 <<< "$APPLIED")" ]] ||
        refuse 'The registered state changed after the completed apply.'
      check_config "$attributes" "$KUBECONFIG_PATH"
      check_uid
      observed=$(run_kubectl 15s "$KUBECONFIG_PATH" --request-timeout=10s get nodes -o json 2>> "$PLAN_DIR/verify.log")
      nodes=$(jq -ce --argjson expected "$TOPOLOGY" '
        [.items[] | {name: .metadata.name, labels: .metadata.labels,
          ready: any(.status.conditions[]?; .type == "Ready" and .status == "True")}]
        | select((map(.name) | sort) == ($expected | map(.name) | sort))
        | select(. as $nodes | all($expected[]; . as $node |
          any($nodes[]; .name == $node.name and .ready and
            (.labels as $labels | all($node.labels | to_entries[]; $labels[.key] == .value)) and
            ($node.role == "control-plane" or (.labels | has("node-role.kubernetes.io/control-plane") | not)))))
      ' <<< "$observed") || refuse 'Unexpected baseline node inventory, role placement, or readiness.'
      write_record "$PLAN_DIR/nodes.json" "$nodes"
      write_record "$PLAN_DIR/verified.json" "$(jq -cS --arg uid "$EXPECTED_UID" \
        '. + {kubeSystemUid: $uid, repeatPlanExit: 0}' <<< "$APPLIED")"
      if path_exists "$PENDING"; then rm -- "$PENDING"; sync -f -- "$PLAN_ROOT"; fi
      printf '%s\n' 'Repeat plan reports zero changes.' | tee -a "$PLAN_DIR/verify.log"
    else
      rc=$?
      printf 'Repeat plan exited %s (2 means changes, 1 means error). See repeat.log.\n' "$rc" >&2
      exit "$rc"
    fi
  }
  check_approval() {
    [[ "$APPROVER" =~ [^[:space:]] && "$APPROVER" != *[[:cntrl:]]* ]] ||
      refuse 'Use a single-line approver name from the approval record.'
    [[ "$APPROVAL_UTC" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ &&
       "$(date -u -d "$APPROVAL_UTC" +%Y-%m-%dT%H:%M:%SZ)" == "$APPROVAL_UTC" ]] ||
      refuse 'Use the UTC approval time in YYYY-MM-DDTHH:MM:SSZ form.'
  }
  approved_operation() {
    operation_record | jq -cS --arg approver "$APPROVER" --arg time "$APPROVAL_UTC" \
      '. + {approver: $approver, approvalUtc: $time}'
  }
  apply_or_resume() {
    local attempts applied state_sha
    local attempted_file="$PLAN_DIR/apply-attempted.json" applied_file="$PLAN_DIR/applied.json"
    if [[ "$OPERATION" == apply ]]; then
      check_approval
      [[ ! -e "$attempted_file" && ! -L "$attempted_file" && ! -e "$applied_file" && ! -L "$applied_file" ]] &&
        ! path_exists "$PENDING" && ! path_exists "$PLAN_DIR/apply.log" ||
        refuse 'An apply was already attempted. Resume verification or use explicit Operations recovery.'
      check_backend
      check_daemon
      check_digest
      check_vacancy
      attempts=$(approved_operation)
      write_record "$PENDING" "$attempts"
      write_record "$attempted_file" "$attempts"
      check_digest
      run_tofu 300s apply -no-color -input=false -lock-timeout=30s \
        -state="$STATE" -state-out="$STATE" -backup="$STATE.backup" "$PLAN" \
        > "$PLAN_DIR/apply.log" 2>&1 || refuse 'OpenTofu apply did not confirm success. Use explicit Operations recovery.'
      private_file "$STATE"
      state_sha=$(digest < "$STATE")
      write_record "$applied_file" "$(jq -cS --arg state_sha "$state_sha" '. + {stateSha256: $state_sha}' <<< "$attempts")"
    fi
    path_exists "$applied_file" || refuse 'There is no confirmed apply. Inspect the attempt and use explicit Operations recovery.'
    attempts=$(evidence "$attempted_file")
    applied=$(evidence "$applied_file")
    [[ "$(jq -cS 'del(.approver, .approvalUtc)' <<< "$attempts")" == "$(operation_record | jq -cS .)" &&
       "$(jq -cS 'del(.stateSha256)' <<< "$applied")" == "$(jq -cS . <<< "$attempts")" ]] ||
      refuse 'The apply evidence differs from the exact saved plan and target.'
    if path_exists "$PENDING"; then
      [[ "$(evidence "$PENDING" | jq -cS .)" == "$(jq -cS . <<< "$attempts")" ]] ||
        refuse 'Another apply is pending for this registered target.'
    else
      evidence "$PLAN_DIR/verified.json" >/dev/null
    fi
    private_file "$STATE"
    [[ "$(digest < "$STATE")" == "$(jq -r .stateSha256 <<< "$applied")" ]] ||
      refuse 'The registered state changed after the completed apply.'
    APPLIED=$applied
    record_identity
    verify_plan
  }
  check_destroy_state() {
    private_file "$STATE"
    [[ "$(digest < "$STATE")" == "$STATE_SHA256" ]] || refuse 'The registered state changed after destroy planning.'
  }
  destroy_or_resume() {
    local sealed_plan=$1 attempts recorded applied state_sha retirement status
    local attempted_file="$PLAN_DIR/destroy-attempted.json" applied_file="$PLAN_DIR/destroy-applied.json"
    local verified_file="$PLAN_DIR/destroy-verified.json" retired_file="$PLAN_DIR/retired-registration.json"
    check_approval
    ! path_exists "$PENDING" || refuse 'An apply is pending. Resume verification or use explicit Operations recovery.'
    ! path_exists "$retired_file" || refuse 'A retired registration already exists in this attempt.'
    attempts=$(approved_operation | jq -cS --arg uid "$EXPECTED_UID" --arg state_sha "$STATE_SHA256" \
      '. + {intent: "destroy", kubeSystemUid: $uid, priorStateSha256: $state_sha}')
    if ! path_exists "$attempted_file"; then
      ! path_exists "$DESTROY_PENDING" && ! path_exists "$applied_file" && ! path_exists "$verified_file" &&
        ! path_exists "$PLAN_DIR/destroy.log" || refuse 'A destroy is already pending. Use explicit Operations recovery.'
      [[ "$(clean_revision)" == "$ENV_REVISION" ]] ||
        refuse 'The reviewed environment changed. Save and separately approve a new destroy plan.'
      check_destroy_state
      check_backend "$sealed_plan"
      check_managed_identity
      check_destroy_state
      write_record "$DESTROY_PENDING" "$attempts"
      write_record "$attempted_file" "$attempts"
      if run_tofu 300s apply -no-color -input=false -lock-timeout=30s \
        -state="$STATE" -state-out="$STATE" -backup="$STATE.backup" "$sealed_plan" > /dev/null 2>&1; then
        printf 'OpenTofu destroy exited 0.\n' > "$PLAN_DIR/destroy.log"
      else
        status=$?
        printf 'OpenTofu destroy exited %s.\n' "$status" > "$PLAN_DIR/destroy.log"
        refuse 'OpenTofu did not confirm destroy success. Use explicit Operations recovery.'
      fi
      private_file "$STATE"
      state_sha=$(digest < "$STATE")
      write_record "$applied_file" "$(jq -cS --arg sha "$state_sha" '. + {postDestroyStateSha256: $sha}' <<< "$attempts")"
    fi
    recorded=$(evidence "$attempted_file" | jq -cS .)
    [[ "$recorded" == "$attempts" ]] || refuse 'The destroy attempt differs from this exact plan and approval.'
    path_exists "$applied_file" || refuse 'There is no confirmed destroy. Use explicit Operations recovery.'
    applied=$(evidence "$applied_file")
    [[ "$(jq -cS 'del(.postDestroyStateSha256)' <<< "$applied")" == "$attempts" ]] ||
      refuse 'The confirmed destroy evidence differs from this exact plan and approval.'
    if path_exists "$DESTROY_PENDING"; then
      [[ "$(evidence "$DESTROY_PENDING" | jq -cS .)" == "$attempts" ]] ||
        refuse 'Another destroy is pending for the registered target.'
    else
      evidence "$verified_file" >/dev/null
    fi
    private_file "$STATE"
    [[ "$(digest < "$STATE")" == "$(jq -er .postDestroyStateSha256 <<< "$applied")" ]] &&
      jq -es 'length == 1 and .[0].resources == [] and .[0].outputs == {}' "$STATE" >/dev/null ||
      refuse 'The post-destroy state differs from the confirmed empty state.'
    check_daemon
    check_nodes_absent 'The registered kind nodes remain. The registration was retained.'
    retirement=$(jq -cS '. + {dockerNodesAbsent: true, emptyState: true}' <<< "$applied")
    if path_exists "$verified_file"; then
      [[ "$(evidence "$verified_file" | jq -cS .)" == "$retirement" ]] ||
        refuse 'The prior destroy verification differs from the current evidence.'
    else
      write_record "$verified_file" "$retirement"
    fi
    (
      lock_registry
      [[ "$(load_registration "$REGISTRATION" "$TARGET")" == "$INPUT" ]] ||
        refuse 'The registration changed before retirement.'
      mv -T -- "$REGISTRATION" "$retired_file" || refuse 'Could not archive the verified registration. Retry the same approved destroy.'
      sync -f -- "$retired_file"
      sync -f -- "$REGISTRY/$OWNER"
      rm -f -- "$DESTROY_PENDING"
      sync -f -- "$PLAN_ROOT"
    )
    printf '%s\n' "$retired_file"
  }
  clean_runtime() {
    local status=$?
    if [[ -n "${RUNTIME:-}" ]]; then rm -rf -- "$RUNTIME"; fi
    if [[ -n "${EXPORTED_CONFIG:-}" ]]; then rm -f -- "$EXPORTED_CONFIG"; fi
    if (( status != 0 )); then printf 'Operation stopped. Private evidence is in %s\n' "$PLAN_DIR" >&2; fi
  }

  USAGE='Select register TARGET ENVIRONMENT CANDIDATE, plan|plan-destroy|bootstrap TARGET ENVIRONMENT, apply|destroy TARGET PLAN SHA256 APPROVER APPROVAL_UTC, or verify TARGET PLAN.'
  [[ $# -ge 4 ]] || refuse "$USAGE"
  PROVIDER=$(realpath -e -- "$1")
  [[ "$(git_at "$PROVIDER" rev-parse --show-toplevel)" == "$PROVIDER" ]] ||
    refuse 'Select the provider checkout root.'
  OPERATION=$2
  INTENT=create
  if [[ "$OPERATION" == plan-destroy || "$OPERATION" == destroy ]]; then INTENT=destroy; fi
  TARGET=$3
  [[ "$TARGET" =~ ^[a-z0-9]+(-[a-z0-9]+)*/[a-z0-9]+(-[a-z0-9]+)*$ ]] ||
    refuse 'Select an explicit owner/target key.'
  REGISTRY=${KIND_REGISTRATIONS:?Set KIND_REGISTRATIONS to the Operations registry}
  private_dir "$REGISTRY"
  OWNER=${TARGET%/*}
  REGISTRATION="$REGISTRY/$TARGET.json"
  case "$OPERATION:$#" in
    register:5)
      ENVIRONMENT=$(realpath -e -- "$4")
      outside_checkouts "$REGISTRY"
      private_file "$5"
      CANDIDATE=$(canonical_private_path "$5")
      INPUT=$(read_record "$CANDIDATE")
      jq -e --arg target "$TARGET" '
        keys == ["declaration", "dockerHost", "kubeconfig", "lock", "state", "target"] and .target == $target
        and ([.declaration, .dockerHost, .state, .lock, .kubeconfig]
          | all(.[]; strings | length > 0 and (test("[[:cntrl:]]") | not)))
        ' <<< "$INPUT" >/dev/null || refuse 'Invalid registration candidate.'
      STATE=$(canonical_private_path "$(jq -r .state <<< "$INPUT")")
      LOCK=$(canonical_private_path "$(jq -r .lock <<< "$INPUT")")
      KUBECONFIG_PATH=$(canonical_private_path "$(jq -r .kubeconfig <<< "$INPUT")")
      ;;
    plan:4|apply:7|verify:4|bootstrap:4|plan-destroy:4|destroy:7)
      [[ -e "$REGISTRATION" || -L "$REGISTRATION" ]] || refuse 'No registration exists for this target.'
      private_dir "$REGISTRY/$OWNER"
      private_file "$REGISTRATION"
      ENVIRONMENT=$(jq -er '.environment | strings' "$REGISTRATION")
      [[ -d "$ENVIRONMENT" && "$(realpath -e -- "$ENVIRONMENT")" == "$ENVIRONMENT" && "$ENVIRONMENT" != / ]] ||
        refuse 'The registered environment checkout path is unavailable or redirected.'
      outside_checkouts "$REGISTRY"
      INPUT=$(load_registration "$REGISTRATION" "$TARGET")
      if [[ ( "$OPERATION" == plan || "$OPERATION" == plan-destroy || "$OPERATION" == bootstrap ) &&
          "$(realpath -e -- "$4")" != "$ENVIRONMENT" ]]; then
        refuse 'Use the environment checkout originally selected for this registration.'
      fi
      LOCK=$(jq -r .lock <<< "$INPUT")
      exec 9>>"$LOCK"
      flock -w 15 9 || refuse 'The target lock is busy.'
      [[ "$(load_registration "$REGISTRATION" "$TARGET")" == "$INPUT" ]] ||
        refuse 'The registration changed while acquiring the target lock.'
      STATE=$(jq -r .state <<< "$INPUT")
      KUBECONFIG_PATH=$(jq -r .kubeconfig <<< "$INPUT")
      CONTEXT=$(jq -r .context <<< "$INPUT")
      EXPECTED_UID=$(jq -r '.kubeSystemUid // empty' <<< "$INPUT")
      if [[ ( "$OPERATION" == plan || "$OPERATION" == apply ) && -n "$EXPECTED_UID" ]]; then
        refuse 'The reserved cluster already has a registered UID. Use verify with its applied plan.'
      fi
      ;;
    *) refuse "$USAGE" ;;
  esac

  DOCKER_HOST=$(jq -r .dockerHost <<< "$INPUT")
  DECLARATION_PATH=$(jq -r .declaration <<< "$INPUT")
  relative_json_path='^([A-Za-z0-9_-][A-Za-z0-9_.-]*/)*[A-Za-z0-9_-][A-Za-z0-9_.-]*\.json$'
  [[ "$DECLARATION_PATH" =~ $relative_json_path ]] || refuse 'Select a repository-relative JSON declaration path.'
  [[ "$DOCKER_HOST" =~ ^unix:///[^[:space:][:cntrl:]]+$ ]] ||
    refuse 'Use an explicit local Unix Docker socket.'
  unset DOCKER_CONTEXT DOCKER_TLS DOCKER_TLS_VERIFY DOCKER_CERT_PATH

  MODULE=clusters/local-kind/kind-baseline
  PROFILE_FILES=(main.tf .terraform.lock.hcl)
  PROVIDER_ADDRESS=registry.terraform.io/elioseverojunior/kind
  PLAN_ROOT="$REGISTRY/.plans/$TARGET"
  PENDING="$PLAN_ROOT/active-apply.json"
  DESTROY_PENDING="$PLAN_ROOT/active-destroy.json"
  if [[ "$OPERATION" == apply || "$OPERATION" == verify || "$OPERATION" == destroy ]]; then
    PLAN=$4
    PLAN_DIR=${PLAN%/*}
    attempt_prefix=plan
    if [[ "$INTENT" == destroy ]]; then attempt_prefix=destroy; fi
    [[ "$PLAN" == "$PLAN_ROOT/$attempt_prefix."*/saved.tfplan && "${PLAN_DIR%/*}" == "$PLAN_ROOT" ]] ||
      refuse 'The absolute saved plan path must belong to the selected registered target.'
    for directory in "$REGISTRY/.plans" "$REGISTRY/.plans/$OWNER" "$PLAN_ROOT" "$PLAN_DIR"; do private_dir "$directory"; done
    private_file "$PLAN_DIR/REVIEW.md"
    private_file "$PLAN_DIR/binding.json"
    BINDING=$(read_record "$PLAN_DIR/binding.json")
    jq -e --arg target "$TARGET" --arg intent "$INTENT" '
      def sha: strings | test("^[a-f0-9]{64}$");
      def commit: strings | test("^([a-f0-9]{40}|[a-f0-9]{64})$");
      (del(.priorStateSha256) | keys == ["declarationSha256", "environmentRevision", "intent", "profile", "providerPinSha256",
        "providerRevision", "registrationSha256", "schemaVersion", "target"])
      and .schemaVersion == 1 and .target == $target and .profile == "kind-baseline"
      and .intent == $intent
      and (if $intent == "destroy" then (.priorStateSha256 | sha) else (has("priorStateSha256") | not) end)
      and (.environmentRevision | commit) and (.providerRevision | commit)
      and ([.declarationSha256, .providerPinSha256, .registrationSha256] | all(.[]; sha))
    ' <<< "$BINDING" >/dev/null || refuse 'The plan binding is invalid or differs from the registered target.'
    BINDING_SHA256=$(digest < "$PLAN_DIR/binding.json")
    ENV_REVISION=$(jq -r .environmentRevision <<< "$BINDING")
    [[ "$(git_at "$ENVIRONMENT" rev-parse --show-toplevel)" == "$ENVIRONMENT" &&
       "$(git_at "$ENVIRONMENT" rev-parse --verify "$ENV_REVISION^{commit}")" == "$ENV_REVISION" ]] ||
      refuse 'The exact saved environment revision is unavailable.'
    if [[ "$OPERATION" == apply || "$OPERATION" == destroy ]]; then
      PLAN_SHA256=$5 APPROVER=$6 APPROVAL_UTC=$7
    else
      path_exists "$PLAN_DIR/applied.json" || refuse 'There is no confirmed apply. Inspect the attempt and use explicit Operations recovery.'
      PLAN_SHA256=$(evidence "$PLAN_DIR/applied.json" | jq -er .planSha256)
    fi
    check_digest
    TOFU_CONFIG="$PLAN_DIR/opentofu.tfrc"
  else
    ENV_REVISION=$(clean_revision)
  fi
  DECLARATION=$(stage_blob "$ENVIRONMENT" "$ENV_REVISION" "$DECLARATION_PATH")
  jq -es '
    length == 1 and (.[0] |
      keys == ["clusterName", "disposable", "profile", "providerPin"]
      and .profile == "kind-baseline" and (.disposable | type == "boolean")
      and (.clusterName | strings | test("^[a-z]([a-z0-9-]{0,47}[a-z0-9])?$"))
      and (.providerPin | type == "string" and (test("[[:cntrl:]]") | not)))
    ' <<< "$DECLARATION" >/dev/null || refuse 'Invalid declaration or unsupported kind-baseline inputs.'
  CLUSTER=$(jq -r .clusterName <<< "$DECLARATION")
  if [[ "$INTENT" == destroy ]]; then
    jq -e '.disposable == true' <<< "$DECLARATION" >/dev/null || refuse 'Only a reviewed disposable target can be destroyed.'
    [[ -n "$EXPECTED_UID" ]] || refuse 'Destroy requires a registered live kube-system UID.'
  fi
  PIN_PATH=$(jq -r .providerPin <<< "$DECLARATION")
  [[ "$PIN_PATH" =~ $relative_json_path ]] || refuse 'Select a repository-relative JSON provider pin.'
  PIN=$(stage_blob "$ENVIRONMENT" "$ENV_REVISION" "$PIN_PATH")
  PROVIDER_REVISION=$(jq -esr '
    select(length == 1) | .[0] |
    select(.schemaVersion == 1 and .source.repository == "https://github.com/stanimirivanov/k8s-infrastructure.git") |
    .source.revision | strings | select(test("^([a-f0-9]{40}|[a-f0-9]{64})$"))
    ' <<< "$PIN") || refuse 'The provider pin must select an exact k8s-infrastructure commit.'
  [[ "$(git_at "$PROVIDER" rev-parse --verify "$PROVIDER_REVISION^{commit}")" == "$PROVIDER_REVISION" ]] ||
    refuse 'The exact pinned provider commit is unavailable in this checkout.'
  # Refuse pins whose deployed root would differ from the staged root.
  git_at "$PROVIDER" ls-tree -z --name-only "$PROVIDER_REVISION:$MODULE" |
    while IFS= read -r -d '' file; do
      case "$file" in
        main.tf) ;;
        *.tf|*.tf.json|*.tofu|*.tofu.json|terraform.tfvars|terraform.tfvars.json|*.auto.tfvars|*.auto.tfvars.json)
          refuse 'Unsupported configuration in the pinned baseline root. Review the profile before using this pin.' ;;
      esac
    done
  for file in "${PROFILE_FILES[@]}"; do
    stage_blob "$PROVIDER" "$PROVIDER_REVISION" "$MODULE/$file" >/dev/null
  done
  if [[ "$OPERATION" == register || "$OPERATION" == plan || "$OPERATION" == plan-destroy || "$OPERATION" == bootstrap ]]; then
    [[ "$(clean_revision)" == "$ENV_REVISION" ]] || refuse 'The environment changed while staging its inputs.'
  fi
  DAEMON=$(observe_daemon "$DOCKER_HOST")

  if [[ "$OPERATION" == register ]]; then
    PROPOSED=$(jq -cS --arg environment "$ENVIRONMENT" --arg cluster "$CLUSTER" --arg daemon "$DAEMON" --arg state "$STATE" \
      --arg lock "$LOCK" --arg kubeconfig "$KUBECONFIG_PATH" '
      . + {schemaVersion: 1, clusterName: $cluster, context: ("kind-" + $cluster),
        daemonId: $daemon, environment: $environment, state: $state, lock: $lock, kubeconfig: $kubeconfig}
      ' <<< "$INPUT")
    PROPOSED_PATHS=$(record_paths "$PROPOSED")
    while IFS= read -r path; do canonical_private_path "$path" >/dev/null; done <<< "$PROPOSED_PATHS"

    lock_registry
    ! path_exists "$DESTROY_PENDING" || refuse 'A destroy is pending. Resume its verified retirement or use explicit Operations recovery.'

    shopt -s nullglob
    for owner_dir in "$REGISTRY"/*/; do
      owner_dir=${owner_dir%/}
      private_dir "$owner_dir"
      for file in "$owner_dir"/*.json; do
        key=${file#"$REGISTRY/"}
        row=$(load_registration "$file" "${key%.json}")
        if [[ "$file" == "$REGISTRATION" ]]; then
          [[ "$(jq -cS 'del(.kubeSystemUid)' <<< "$row")" == "$PROPOSED" ]] || refuse 'The target key already has a different registration.'
          continue
        fi
        jq -e --arg daemon "$DAEMON" --arg cluster "$CLUSTER" \
          '.daemonId != $daemon or .clusterName != $cluster' <<< "$row" >/dev/null ||
          refuse 'This Docker daemon and cluster name are already reserved.'
        paths=$(record_paths "$row")
        if grep -Fxq -f <(printf '%s\n' "$paths") <<< "$PROPOSED_PATHS"; then
          refuse 'Another target already reserves a canonical path.'
        fi
      done
    done
    if [[ -e "$REGISTRATION" ]]; then printf '%s\n' "$REGISTRATION"; exit 0; fi
    mkdir -p -- "$REGISTRY/$OWNER"
    private_dir "$REGISTRY/$OWNER"
    temporary=$(mktemp "$REGISTRY/$OWNER/.reservation.XXXXXX")
    trap 'rm -f -- "$temporary"' EXIT
    printf '%s\n' "$PROPOSED" > "$temporary"
    mv -T -- "$temporary" "$REGISTRATION"
    printf '%s\n' "$REGISTRATION"
    exit 0
  fi

  [[ "$CLUSTER" == "$(jq -r .clusterName <<< "$INPUT")" ]] ||
    refuse 'The declaration changes the reserved cluster name.'
  [[ "$DAEMON" == "$(jq -r .daemonId <<< "$INPUT")" ]] ||
    refuse 'Docker daemon ID differs from the registration.'
  if [[ "$OPERATION" != destroy ]]; then
    ! path_exists "$DESTROY_PENDING" || refuse 'A destroy is pending. Resume its verified retirement or use explicit Operations recovery.'
  fi
  if [[ "$OPERATION" == bootstrap ]]; then
    ! path_exists "$PENDING" || refuse 'An apply is pending. Resume verification or use explicit Operations recovery.'
    prepare_plan_root
    PLAN_DIR=$(mktemp -d "$PLAN_ROOT/bootstrap.XXXXXX")
    RUNTIME=$(mktemp -d "$PLAN_DIR/.runtime.XXXXXX")
    trap clean_runtime EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    bootstrap_argocd
    exit 0
  fi
  if [[ "$OPERATION" == apply || "$OPERATION" == verify || "$OPERATION" == destroy ]]; then
    [[ "$(jq -r .providerRevision <<< "$BINDING")" == "$PROVIDER_REVISION" ]] ||
      refuse 'The saved provider revision differs from the reviewed environment pin.'
    check_snapshot
    STAGED_REGISTRATION=$(load_registration "$PLAN_DIR/registration.json" "$TARGET")
    current_registration=$(jq -cS --arg intent "$INTENT" 'if $intent == "create" then del(.kubeSystemUid) else . end' <<< "$INPUT")
    [[ "$current_registration" == "$(jq -cS . <<< "$STAGED_REGISTRATION")" ]] ||
      refuse 'The current registration differs from the saved target binding or UID.'
    RUNTIME=$(mktemp -d "$PLAN_DIR/.runtime.XXXXXX")
    trap clean_runtime EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    if [[ "$INTENT" == destroy ]]; then
      load_destroy_key
      STATE_SHA256=$(jq -r .priorStateSha256 <<< "$BINDING")
      # Verify the copy in the fresh runtime. Keep PLAN as the approved path in receipts.
      sealed_plan="$RUNTIME/saved.tfplan"
      cat -- "$PLAN" > "$sealed_plan" || refuse 'Could not copy the approved destroy plan.'
      [[ "$(digest < "$sealed_plan")" == "$PLAN_SHA256" ]] ||
        refuse 'The destroy plan snapshot differs from the exact approved file.'
      run_tofu 120s init -no-color -input=false -get=false -lockfile=readonly -backend-config="path=$STATE" \
        > /dev/null 2>&1 || refuse 'OpenTofu could not initialize the encrypted destroy attempt.'
      check_destroy_plan "$sealed_plan"
      destroy_or_resume "$sealed_plan"
      exit 0
    fi
    check_plan_archive
    may_write_log "$PLAN_DIR/operation-init.log"
    # Reinstall using the reviewed lock and a fresh CLI data directory before using the plan.
    run_tofu 120s init -no-color -input=false -get=false -lockfile=readonly -backend-config="path=$STATE" \
      >> "$PLAN_DIR/operation-init.log" 2>&1
    TOPOLOGY=$(check_plan_shape) || refuse 'OpenTofu did not save the expected bound baseline create plan.'
    apply_or_resume
    exit 0
  fi

  ! path_exists "$PENDING" || refuse 'An apply is pending. Resume verification or use explicit Operations recovery.'
  if [[ "$INTENT" == create ]]; then check_vacancy; fi
  MIRROR=${KIND_PROVIDER_MIRROR:?Set KIND_PROVIDER_MIRROR to the qualified local provider mirror}
  [[ -d "$MIRROR" && "$(realpath -e -- "$MIRROR")" == "$MIRROR" && "$MIRROR" != *[[:cntrl:]]* ]] ||
    refuse 'Use a canonical directory path for the local provider mirror.'

  prepare_plan_root
  attempt_prefix=plan
  if [[ "$INTENT" == destroy ]]; then attempt_prefix=destroy; fi
  PLAN_DIR=$(mktemp -d "$PLAN_ROOT/$attempt_prefix.XXXXXX")
  trap clean_runtime EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  STATE_SHA256=''
  if [[ "$INTENT" == destroy ]]; then
    RUNTIME=$(mktemp -d "$PLAN_DIR/.runtime.XXXXXX")
    check_managed_identity
    STATE_SHA256=$(digest < "$STATE")
    load_destroy_key
  fi
  TOFU_CONFIG="$PLAN_DIR/opentofu.tfrc"
  jq -n --arg mirror "$MIRROR" --arg provider "$PROVIDER_ADDRESS" \
    '{provider_installation: [{filesystem_mirror: [{path: $mirror, include: [$provider]}]}]}' > "$TOFU_CONFIG"
  for file in "${PROFILE_FILES[@]}"; do
    stage_blob "$PROVIDER" "$PROVIDER_REVISION" "$MODULE/$file" > "$PLAN_DIR/$file"
  done
  stage_blob "$ENVIRONMENT" "$ENV_REVISION" "$DECLARATION_PATH" > "$PLAN_DIR/declaration.json"
  stage_blob "$ENVIRONMENT" "$ENV_REVISION" "$PIN_PATH" > "$PLAN_DIR/provider-pin.json"
  cp -- "$REGISTRATION" "$PLAN_DIR/registration.json"
  jq -nS --arg target "$TARGET" --arg environment "$ENV_REVISION" --arg provider "$PROVIDER_REVISION" \
    --arg intent "$INTENT" --arg state_sha "$STATE_SHA256" \
    --arg registration "$(digest < "$PLAN_DIR/registration.json")" \
    --arg declaration "$(digest < "$PLAN_DIR/declaration.json")" \
    --arg pin "$(digest < "$PLAN_DIR/provider-pin.json")" \
    '{schemaVersion: 1, target: $target, profile: "kind-baseline", intent: $intent,
      environmentRevision: $environment, providerRevision: $provider, registrationSha256: $registration,
      declarationSha256: $declaration, providerPinSha256: $pin}
    + (if $intent == "destroy" then {priorStateSha256: $state_sha} else {} end)' > "$PLAN_DIR/binding.json"
  BINDING_SHA256=$(digest < "$PLAN_DIR/binding.json")
  jq -n --arg name "$CLUSTER" --arg digest "$BINDING_SHA256" \
    '{cluster_name: $name, plan_binding_sha256: $digest}' > "$PLAN_DIR/target.auto.tfvars.json"

  PLAN="$PLAN_DIR/saved.tfplan"
  if [[ "$INTENT" == destroy ]]; then
    run_tofu 120s init -no-color -input=false -get=false -lockfile=readonly -backend-config="path=$STATE" \
      >/dev/null 2>&1 || refuse 'OpenTofu could not initialize the encrypted destroy attempt.'
    run_tofu 180s plan -destroy -no-color -input=false -refresh=false -lock-timeout=30s -out="$PLAN" \
      >/dev/null 2>&1 || refuse 'OpenTofu could not save the encrypted destroy plan.'
    check_destroy_plan "$PLAN"
    check_backend
    check_managed_identity
    check_destroy_state
    printf 'OpenTofu destroy plan for %s (%s)\n\n  - kind_cluster.baseline\n\nPlan: 0 to add, 0 to change, 1 to destroy.\n' \
      "$TARGET" "$CLUSTER" > "$PLAN_DIR/plan.txt"
  else
    run_tofu 120s init -no-color -input=false -get=false -lockfile=readonly -backend-config="path=$STATE" > "$PLAN_DIR/init.log" 2>&1
    run_tofu 180s plan -no-color -input=false -refresh=false -lock-timeout=30s -out="$PLAN" > "$PLAN_DIR/plan.log" 2>&1
    check_plan_shape >/dev/null 2>"$PLAN_DIR/show.log" || refuse 'The saved plan differs from the selected profile. See show.log.'
    run_tofu 60s show -no-color "$PLAN" > "$PLAN_DIR/plan.txt" 2>>"$PLAN_DIR/show.log"
  fi
  PLAN_SHA256=$(digest < "$PLAN")
  printf '%s  saved.tfplan\n' "$PLAN_SHA256" > "$PLAN_DIR/SHA256SUMS"
  printf '# Plan review\n\n- Target %s\n- Profile kind-baseline\n- Intent %s\n- Environment revision %s\n- Provider revision %s\n- Binding SHA-256 %s\n- Plan %s\n- SHA-256 %s\n' \
    "$TARGET" "$INTENT" "$ENV_REVISION" "$PROVIDER_REVISION" "$BINDING_SHA256" "$PLAN" "$PLAN_SHA256" > "$PLAN_DIR/.review"
  mv -T -- "$PLAN_DIR/.review" "$PLAN_DIR/REVIEW.md"
  printf '%s\n' "$PLAN_DIR/REVIEW.md"
)
```

## Qualify a fresh non-Perfeng target

Choose a new disposable target owned by a non-Perfeng environment. Reserve it,
save its create plan, and stop for approval of the exact path and digest.
After approval, apply and verify. Keep `verified.json`, `nodes.json`, and
`repeat.log` as proof of both Ready roles, the expected inventory, and a
zero-change repeat plan.

Authorize the qualification fixtures on this target. In Bash, set `TARGET`
to the registered key and `CREATE_EVIDENCE` to the absolute path to the
successful create attempt's `verified.json`. Use the same `PROVIDER_CHECKOUT`
and registry as the create operation. Run this block to create the reviewed
DNS and PVC probes and wait for their results. The probes use the pinned
BusyBox image and the `standard` storage class. The PVC job writes data from
one container and reads and compares it from a second container.

```bash
(
  set +x
  set -euo pipefail
  umask 077
  [[ "$TARGET" =~ ^[a-z0-9]+(-[a-z0-9]+)*/[a-z0-9]+(-[a-z0-9]+)*$ ]]
  registration="$KIND_REGISTRATIONS/$TARGET.json"
  plan_root="$KIND_REGISTRATIONS/.plans/$TARGET"
  attempt=${CREATE_EVIDENCE%/*}
  [[ "$CREATE_EVIDENCE" == "$plan_root/plan."*/verified.json && "${attempt%/*}" == "$plan_root" ]]
  private_file() {
    [[ -f "$1" && -O "$1" && ! -L "$1" && "$(stat -c '%a %h' -- "$1")" == '600 1' ]]
  }
  for file in "$registration" "$CREATE_EVIDENCE" "$attempt/binding.json"; do private_file "$file"; done
  record=$(cat -- "$registration")
  lock=$(jq -er .lock <<< "$record")
  private_file "$lock"
  exec 9>>"$lock"
  flock -w 15 9
  [[ "$(cat -- "$registration")" == "$record" && ! -e "$plan_root/active-apply.json" && ! -e "$plan_root/active-destroy.json" ]]
  digest() { sha256sum | cut -d ' ' -f 1; }
  config=$(jq -er .kubeconfig <<< "$record")
  state=$(jq -er .state <<< "$record")
  context=$(jq -er .context <<< "$record")
  daemon=$(jq -er .daemonId <<< "$record")
  docker_host=$(jq -er .dockerHost <<< "$record")
  uid=$(jq -er '.kubeSystemUid | select(test("^[a-zA-Z0-9][a-zA-Z0-9-]*$"))' <<< "$record")
  private_file "$state"
  private_file "$config"
  jq -e --arg target "$TARGET" --arg state "$state" --arg state_sha "$(digest < "$state")" \
    --arg binding "$(digest < "$attempt/binding.json")" --arg daemon "$daemon" --arg uid "$uid" '
    .target == $target and .state == $state and .stateSha256 == $state_sha and .bindingSha256 == $binding
    and .daemonId == $daemon and .kubeSystemUid == $uid and .repeatPlanExit == 0
  ' "$CREATE_EVIDENCE" >/dev/null
  revision=$(jq -er '.providerRevision | select(test("^([a-f0-9]{40}|[a-f0-9]{64})$"))' "$attempt/binding.json")
  proof=$(mktemp -d "$attempt/probes.XXXXXX")
  trap 'printf "Private probe evidence is in %s\n" "$proof"' EXIT
  kube() {
    local deadline=$1 kubeconfig=$2
    shift 2
    timeout --kill-after=5s "$deadline" env -i PATH="$PATH" HOME="$proof" \
      kubectl --kubeconfig "$kubeconfig" --context "$context" "$@"
  }
  config_identity() {
    kube 15s "$1" config view --minify --raw -o json |
      jq -ce 'select(.clusters | length == 1) | select(.users | length == 1 and
        (.[0].user | keys == ["client-certificate-data", "client-key-data"]))
        | .clusters[0].cluster | select(keys == ["certificate-authority-data", "server"]
          and (.server | test("^https://127\\.0\\.0\\.1:[0-9]+$")))'
  }
  check_target() {
    [[ "$(timeout --kill-after=5s 15s env -i PATH="$PATH" HOME="$proof" \
      docker --host "$docker_host" info --format '{{.ID}}')" == "$daemon" ]]
    local expected dedicated current_uid
    expected=$(jq -er '.resources[0].instances[0].attributes.kubeconfig' "$state" | config_identity /dev/stdin)
    dedicated=$(config_identity "$config")
    [[ "$expected" == "$dedicated" ]]
    current_uid=$(kube 15s "$config" --request-timeout=10s get namespace kube-system -o 'jsonpath={.metadata.uid}')
    [[ "$current_uid" == "$uid" ]]
  }
  export GIT_NO_LAZY_FETCH=1
  unset GIT_DIR GIT_COMMON_DIR GIT_WORK_TREE GIT_NAMESPACE GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES
  timeout --kill-after=5s 30s git --no-replace-objects -C "$PROVIDER_CHECKOUT" cat-file blob \
    "$revision:clusters/local-kind/iac-pilot/rehearsal/probes.yaml" > "$proof/probes.yaml"
  check_target
  existing_probe=$(kube 15s "$config" --request-timeout=10s get namespace kind-rehearsal --ignore-not-found -o name)
  [[ -z "$existing_probe" ]] || { printf 'The kind-rehearsal namespace is already in use.\n' >&2; exit 1; }
  kube 60s "$config" --request-timeout=30s create -f "$proof/probes.yaml" > "$proof/create.log" 2>&1
  kube 170s "$config" --request-timeout=160s -n kind-rehearsal wait --for=condition=Complete \
    --timeout=150s job/dns job/pvc > "$proof/wait.log" 2>&1
  kube 15s "$config" --request-timeout=10s -n kind-rehearsal logs job/dns > "$proof/dns.log" 2>&1
  kube 15s "$config" --request-timeout=10s -n kind-rehearsal get pvc probe-data \
    -o 'jsonpath={.status.phase}' > "$proof/pvc-phase.txt"
  [[ "$(cat -- "$proof/pvc-phase.txt")" == Bound ]]
  check_target
  printf 'DNS Job and PVC write/read Job completed on %s at provider revision %s.\n' "$TARGET" "$revision" \
    > "$proof/VERIFIED.txt"
)
```

Run bootstrap twice with the reviewed pin. Compare the two private receipts
and confirm that each has the same manifest digest, successful rollouts, an
empty diff, and the registered UID. Leave the disposable probes for the
approved teardown. If a probe stops, review the private logs, Job status, and
PVC events before retrying. The procedure requires a vacant probe namespace.

Save a separate encrypted destroy plan and stop for its exact approval.
After approval, run destroy and retain the verified absence and retirement
records. Submit a non-secret summary that identifies the exact provider
commit, qualification target, Ready roles, DNS and PVC outcomes, repeat-plan
result, both bootstrap results, and teardown outcome. Keep the approval,
state, kubeconfig, key, plans, and detailed execution logs in Operations
storage. Publish the reviewed provider revision only after qualification.

## Recover a stopped operation

- For a busy lock, wait for the holder to exit. Inspect the running operation
  before retrying. The lock files persist between operations.
- For a dirty environment checkout or unavailable pin, restore the reviewed
  inputs, obtain the pinned commit locally, and retry with the same key.
- If initialization fails, review the private `init.log` and verify the
  OpenTofu version and checksummed provider archive in the selected mirror.
- For a Docker ID mismatch, redirected path, existing state or occupied
  cluster name, have Operations verify the registration and the existing
  infrastructure before retrying.
- If an interrupted planner leaves an empty state file or local backend lock
  metadata, diagnose it under the registered target lock. Have Operations
  verify that OpenTofu has exited, any state file is empty, and no apply ran
  or cluster exists for the target. Retain private evidence and use an
  explicitly approved Operations recovery to move only artifacts proven to
  be abandoned before retrying.
- A failed plan can leave private `init.log`, `plan.log`, and `show.log`
  under its attempt directory. A completed review has `REVIEW.md`,
  `plan.txt`, `saved.tfplan`, and `SHA256SUMS`. After correcting the
  failure, run a fresh plan and use its own review and digest.

- An apply attempt writes `active-apply.json` in the target's private plan
  directory and `apply-attempted.json` in the selected `plan.*` attempt before
  starting OpenTofu. If `applied.json` is absent, preserve those files along
  with `apply.log`, state, and Docker inventory for explicit Operations
  recovery. An error or interruption can leave partial infrastructure. Have
  Operations establish the native outcome under the target lock and review
  the next action. Retrying the same or another plan is blocked while the
  outstanding attempt is unconfirmed.
- If `applied.json` exists, `verify` can finish identity registration and retry
  readiness and the repeat plan using the same saved snapshot. Review
  `identity-observed.json`, `verify.log`, `nodes.json`, and `repeat.log` as
  available. If state, daemon, kubeconfig endpoint, CA, or UID changed, preserve
  the evidence and have Operations review the binding before proceeding.
- `verified.json` records the exact approved plan and live UID after the
  repeat plan succeeds and the final node inventory passes. The active apply
  record is cleared after verification.
- If bootstrap stops before success, inspect the private render, ownership,
  apply, rollout and diff logs present in the attempt. For a foreign namespace
  or changed UID, have Operations review ownership and identity before any
  further mutation. For rollout or diff failures, review the controller events
  and the pinned overlay before retrying. A retry creates a new attempt.
- Destroy journals its exact approval before applying and preserves the
  registration if OpenTofu fails or the empty-state and absence checks fail.
  If `destroy-applied.json` and the live registration exist, run `destroy` again
  with the same arguments to resume the checks and retirement. If a crash left
  `active-destroy.json` after the registration was archived, have Operations
  match the journal to `destroy-verified.json` and `retired-registration.json`
  and confirm the empty state and node absence under the registry and target
  locks. Clear only the stale journal through explicit Operations recovery.
  If only `destroy-attempted.json` exists, use explicit Operations recovery to
  establish the state and Docker outcome under the target lock. Retain the key,
  ciphertext, registration, journals and diagnostics privately for that review.
