set positional-arguments

provider_checkout := justfile_directory()

[private]
@default:
    {{quote(just_executable())}} --list

# Reserve a reviewed local kind target
register TARGET ENVIRONMENT CANDIDATE: (_kind provider_checkout "register" TARGET ENVIRONMENT CANDIDATE)

# Save a private baseline plan for a registered kind target
plan TARGET ENVIRONMENT: (_kind provider_checkout "plan" TARGET ENVIRONMENT)

# Apply an exact separately approved plan and verify the registered target
apply TARGET ABSOLUTE_PLAN_PATH SHA256 APPROVER APPROVAL_UTC: (_kind provider_checkout "apply" TARGET ABSOLUTE_PLAN_PATH SHA256 APPROVER APPROVAL_UTC)

# Resume identity registration and verification for a confirmed apply
verify TARGET ABSOLUTE_PLAN_PATH: (_kind provider_checkout "verify" TARGET ABSOLUTE_PLAN_PATH)

[private]
_kind PROVIDER OPERATION TARGET INPUT_PATH *ARGS:
    #!/usr/bin/env bash
    set -euo pipefail
    umask 077

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
      daemon=$(timeout --kill-after=5s 15s docker --host "$1" info --format '{{{{.ID}}') ||
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
      local path containers filter
      while IFS= read -r path; do
        [[ "$path" == "$LOCK" ]] || ! path_exists "$path" ||
          refuse 'A create plan requires vacant state, kubeconfig and backend sidecar paths.'
      done <<< "$(record_paths "$INPUT")"
      for filter in "label=io.x-k8s.kind.cluster=$CLUSTER" "name=^/$CLUSTER-(control-plane|worker)$"; do
        containers=$(timeout --kill-after=5s 15s docker --host "$DOCKER_HOST" ps -a \
          --filter "$filter" --format '{{{{.Names}}')
        [[ -z "$containers" ]] || refuse 'The reserved kind name is already in use.'
      done
    }
    run_tofu() {
      local deadline=$1
      shift
      local -a logging=()
      if [[ ${TRACE_PLAN_BACKEND:-} == yes ]]; then logging=(TF_LOG=JSON TF_LOG_PROVIDER=OFF); fi
      timeout --verbose --kill-after=5s "$deadline" env -i PATH="$PATH" HOME="$PLAN_DIR" \
        DOCKER_HOST="$DOCKER_HOST" KIND_EXPERIMENTAL_PROVIDER=docker TF_CLI_CONFIG_FILE="$TOFU_CONFIG" \
        TF_DATA_DIR="${RUNTIME:-$PLAN_DIR/.terraform}" TF_WORKSPACE=default CHECKPOINT_DISABLE=1 \
        KUBECONFIG="$PLAN_DIR/provider.kubeconfig" "${logging[@]}" tofu -chdir="$PLAN_DIR" "$@"
    }
    check_plan_shape() {
      run_tofu 60s show -json "$PLAN" |
        jq -ce --arg cluster "$CLUSTER" --arg binding "$BINDING_SHA256" --arg provider "$PROVIDER_ADDRESS" \
          --arg intent "$INTENT" '
          select(.variables.cluster_name.value == $cluster and .variables.plan_binding_sha256.value == $binding)
          | select((.resource_drift // []) == [] and (.deferred_changes // []) == [])
          | .resource_changes | select(type == "array" and length == 1) | .[0]
          | select(.address == "kind_cluster.baseline" and .mode == "managed" and .provider_name == $provider
            and .previous_address == null and .deposed == null and .change.importing == null
            and .change.after.name == $cluster
            and (if $intent == "create" then .change.actions == ["create"]
                 else .change.actions == ["no-op"] or .change.actions == ["update"] end))
          | .change.after.node
          | select(map(.role) == ["control-plane", "worker"])
          | map({name: ($cluster + "-" + .role), role: .role,
                 labels: ((.labels // {}) + (if .role == "control-plane"
                   then {"node-role.kubernetes.io/control-plane": ""} else {} end))})
        ' || refuse 'OpenTofu did not save the expected bound baseline plan.'
    }
    check_backend() {
      # OpenTofu 1.13.10 graph checks the embedded plan backend and state freshness read-only.
      local trace backend
      backend=$(printf 'backend/local: state manager for workspace "default" will:\n - read initial snapshot from %s\n - write new snapshots to %s\n - create any backup at %s.backup' \
        "$STATE" "$STATE" "$STATE")
      trace=$(TRACE_PLAN_BACKEND=yes run_tofu 60s graph -no-color -plan="$PLAN" 2>&1 >/dev/null) ||
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
      timeout --kill-after=5s "$deadline" env -i PATH="$PATH" HOME="$PLAN_DIR" \
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
    check_managed_target() {
      local attributes
      [[ -n "$EXPECTED_UID" ]] || refuse 'A managed plan requires a registered kube-system UID.'
      attributes=$(state_attributes)
      private_file "$KUBECONFIG_PATH"
      check_config "$attributes" "$KUBECONFIG_PATH"
      check_uid
    }
    record_identity() {
      local attributes uid observation recorded
      check_daemon
      attributes=$(state_attributes)
      if ! path_exists "$KUBECONFIG_PATH"; then
        [[ "$INTENT" == create && -z "$EXPECTED_UID" && ! -e "$PLAN_DIR/identity-observed.json" ]] ||
          refuse 'The registered kubeconfig is missing.'
        EXPORTED_CONFIG=$(mktemp "${KUBECONFIG_PATH%/*}/.kind-config.XXXXXX")
        timeout --kill-after=5s 30s env -i PATH="$PATH" HOME="$PLAN_DIR" DOCKER_HOST="$DOCKER_HOST" \
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
        [[ "$INTENT" != create || -z "$EXPECTED_UID" ]] || refuse 'The original UID observation is missing.'
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
      observed=$(run_kubectl 15s "$KUBECONFIG_PATH" --request-timeout=10s get nodes -o json 2>> "$PLAN_DIR/verify.log")
      jq -e --argjson expected "$TOPOLOGY" '
        (.items | map(.metadata.name) | sort) == ($expected | map(.name) | sort)
        and (.items as $nodes | all($expected[]; . as $node |
          any($nodes[]; .metadata.name == $node.name and
            (.metadata.labels as $labels | all($node.labels | to_entries[]; $labels[.key] == .value)) and
            ($node.role == "control-plane" or (.metadata.labels | has("node-role.kubernetes.io/control-plane") | not)))))
      ' <<< "$observed" >/dev/null || refuse 'Unexpected baseline node inventory or role placement.'
      run_kubectl 100s "$KUBECONFIG_PATH" --request-timeout=90s wait --for=condition=Ready --timeout=90s \
        "${expected[@]}" >> "$PLAN_DIR/verify.log" 2>&1 || refuse 'The required baseline nodes did not become Ready.'
      nodes=$(jq -c '[.items[] | .metadata | {name, labels}]' <<< "$observed")
      write_record "$PLAN_DIR/nodes.json" "$nodes"
      check_snapshot
      if run_tofu 180s plan -no-color -input=false -lock-timeout=30s -state="$STATE" -detailed-exitcode \
        >> "$PLAN_DIR/repeat.log" 2>&1; then
        check_daemon
        attributes=$(state_attributes)
        [[ "$(digest < "$STATE")" == "$(jq -r .stateSha256 <<< "$APPLIED")" ]] ||
          refuse 'The registered state changed after the completed apply.'
        check_config "$attributes" "$KUBECONFIG_PATH"
        check_uid
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
    apply_or_resume() {
      local attempts applied state_sha
      local attempted_file="$PLAN_DIR/apply-attempted.json" applied_file="$PLAN_DIR/applied.json"
      if [[ "$OPERATION" == apply ]]; then
        [[ "$APPROVER" =~ [^[:space:]] && "$APPROVER" != *[[:cntrl:]]* ]] ||
          refuse 'Use a single-line approver name from the approval record.'
        [[ "$APPROVAL_UTC" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ &&
           "$(date -u -d "$APPROVAL_UTC" +%Y-%m-%dT%H:%M:%SZ)" == "$APPROVAL_UTC" ]] ||
          refuse 'Use the UTC approval time in YYYY-MM-DDTHH:MM:SSZ form.'
        [[ ! -e "$attempted_file" && ! -L "$attempted_file" && ! -e "$applied_file" && ! -L "$applied_file" ]] &&
          ! path_exists "$PENDING" && ! path_exists "$PLAN_DIR/apply.log" ||
          refuse 'An apply was already attempted. Resume verification or use explicit Operations recovery.'
        if [[ "$INTENT" == create ]]; then
          [[ -z "$EXPECTED_UID" ]] || refuse 'The reserved cluster already has a registered UID.'
          check_vacancy
        else
          check_managed_target
        fi
        check_backend
        check_daemon
        check_digest
        attempts=$(operation_record | jq -cS --arg approver "$APPROVER" --arg time "$APPROVAL_UTC" \
          '. + {approver: $approver, approvalUtc: $time}')
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
    clean_runtime() {
      local status=$?
      if [[ -n "${RUNTIME:-}" ]]; then rm -rf -- "$RUNTIME"; fi
      if [[ -n "${EXPORTED_CONFIG:-}" ]]; then rm -f -- "$EXPORTED_CONFIG"; fi
      if (( status != 0 )); then printf 'Operation stopped. Private evidence is in %s\n' "$PLAN_DIR" >&2; fi
    }

    USAGE='Select register TARGET ENVIRONMENT CANDIDATE, plan TARGET ENVIRONMENT, apply TARGET PLAN SHA256 APPROVER APPROVAL_UTC, or verify TARGET PLAN.'
    [[ $# -ge 4 ]] || refuse "$USAGE"
    PROVIDER=$(realpath -e -- "$1")
    [[ "$(git_at "$PROVIDER" rev-parse --show-toplevel)" == "$PROVIDER" ]] ||
      refuse 'Select the provider checkout root.'
    OPERATION=$2
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
      plan:4|apply:7|verify:4)
        [[ -e "$REGISTRATION" || -L "$REGISTRATION" ]] || refuse 'No registration exists for this target.'
        private_dir "$REGISTRY/$OWNER"
        private_file "$REGISTRATION"
        ENVIRONMENT=$(jq -er '.environment | strings' "$REGISTRATION")
        [[ -d "$ENVIRONMENT" && "$(realpath -e -- "$ENVIRONMENT")" == "$ENVIRONMENT" && "$ENVIRONMENT" != / ]] ||
          refuse 'The registered environment checkout path is unavailable or redirected.'
        outside_checkouts "$REGISTRY"
        INPUT=$(load_registration "$REGISTRATION" "$TARGET")
        if [[ "$OPERATION" == plan && "$(realpath -e -- "$4")" != "$ENVIRONMENT" ]]; then
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
    if [[ "$OPERATION" == apply || "$OPERATION" == verify ]]; then
      PLAN=$4
      PLAN_DIR=${PLAN%/*}
      [[ "$PLAN" == "$PLAN_ROOT/plan."*/saved.tfplan && "${PLAN_DIR%/*}" == "$PLAN_ROOT" ]] ||
        refuse 'The absolute saved plan path must belong to the selected registered target.'
      for directory in "$REGISTRY/.plans" "$REGISTRY/.plans/$OWNER" "$PLAN_ROOT" "$PLAN_DIR"; do private_dir "$directory"; done
      private_file "$PLAN_DIR/REVIEW.md"
      private_file "$PLAN_DIR/binding.json"
      BINDING=$(read_record "$PLAN_DIR/binding.json")
      jq -e --arg target "$TARGET" '
        def sha: strings | test("^[a-f0-9]{64}$");
        def commit: strings | test("^([a-f0-9]{40}|[a-f0-9]{64})$");
        keys == ["declarationSha256", "environmentRevision", "intent", "profile", "providerPinSha256",
          "providerRevision", "registrationSha256", "schemaVersion", "target"]
        and .schemaVersion == 1 and .target == $target and .profile == "kind-baseline"
        and (.intent == "create" or .intent == "managed")
        and (.environmentRevision | commit) and (.providerRevision | commit)
        and ([.declarationSha256, .providerPinSha256, .registrationSha256] | all(.[]; sha))
      ' <<< "$BINDING" >/dev/null || refuse 'The plan binding is invalid or differs from the registered target.'
      BINDING_SHA256=$(digest < "$PLAN_DIR/binding.json")
      INTENT=$(jq -r .intent <<< "$BINDING")
      ENV_REVISION=$(jq -r .environmentRevision <<< "$BINDING")
      [[ "$(git_at "$ENVIRONMENT" rev-parse --show-toplevel)" == "$ENVIRONMENT" &&
         "$(git_at "$ENVIRONMENT" rev-parse --verify "$ENV_REVISION^{commit}")" == "$ENV_REVISION" ]] ||
        refuse 'The exact saved environment revision is unavailable.'
      if [[ "$OPERATION" == apply ]]; then
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
    if [[ "$OPERATION" == register || "$OPERATION" == plan ]]; then
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
    if [[ "$OPERATION" == apply || "$OPERATION" == verify ]]; then
      [[ "$(jq -r .providerRevision <<< "$BINDING")" == "$PROVIDER_REVISION" ]] ||
        refuse 'The saved provider revision differs from the reviewed environment pin.'
      check_snapshot
      STAGED_REGISTRATION=$(load_registration "$PLAN_DIR/registration.json" "$TARGET")
      STAGED_UID=$(jq -r '.kubeSystemUid // empty' <<< "$STAGED_REGISTRATION")
      BOUND_UID=''
      if [[ "$INTENT" == managed ]]; then BOUND_UID=$EXPECTED_UID; fi
      [[ "$(jq -cS 'del(.kubeSystemUid)' <<< "$INPUT")" == "$(jq -cS 'del(.kubeSystemUid)' <<< "$STAGED_REGISTRATION")" &&
         "$STAGED_UID" == "$BOUND_UID" ]] ||
        refuse 'The current registration differs from the saved target binding or UID.'
      check_plan_archive
      RUNTIME=$(mktemp -d "$PLAN_DIR/.runtime.XXXXXX")
      trap clean_runtime EXIT
      trap 'exit 130' INT
      trap 'exit 143' TERM
      may_write_log "$PLAN_DIR/operation-init.log"
      # Reinstall using the reviewed lock and a fresh CLI data directory before using the plan.
      run_tofu 120s init -no-color -input=false -get=false -lockfile=readonly -backend-config="path=$STATE" \
        >> "$PLAN_DIR/operation-init.log" 2>&1
      TOPOLOGY=$(check_plan_shape)
      apply_or_resume
      exit 0
    fi

    ! path_exists "$PENDING" || refuse 'An apply is pending. Resume verification or use explicit Operations recovery.'
    if [[ -z "$EXPECTED_UID" ]]; then
      INTENT=create
      check_vacancy
    else
      INTENT=managed
    fi
    MIRROR=${KIND_PROVIDER_MIRROR:?Set KIND_PROVIDER_MIRROR to the qualified local provider mirror}
    [[ -d "$MIRROR" && "$(realpath -e -- "$MIRROR")" == "$MIRROR" && "$MIRROR" != *[[:cntrl:]]* ]] ||
      refuse 'Use a canonical directory path for the local provider mirror.'

    for directory in "$REGISTRY/.plans" "$REGISTRY/.plans/$OWNER" "$REGISTRY/.plans/$TARGET"; do
      mkdir -p -- "$directory"
      private_dir "$directory"
    done
    PLAN_DIR=$(mktemp -d "$REGISTRY/.plans/$TARGET/plan.XXXXXX")
    trap 'if (( $? != 0 )); then printf "Planning stopped. Private diagnostics are in %s\n" "$PLAN_DIR" >&2; fi' EXIT
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
      --arg intent "$INTENT" --arg registration "$(digest < "$PLAN_DIR/registration.json")" \
      --arg declaration "$(digest < "$PLAN_DIR/declaration.json")" \
      --arg pin "$(digest < "$PLAN_DIR/provider-pin.json")" \
      '{schemaVersion: 1, target: $target, profile: "kind-baseline", intent: $intent,
        environmentRevision: $environment, providerRevision: $provider, registrationSha256: $registration,
        declarationSha256: $declaration, providerPinSha256: $pin}' > "$PLAN_DIR/binding.json"
    BINDING_SHA256=$(digest < "$PLAN_DIR/binding.json")
    jq -n --arg name "$CLUSTER" --arg digest "$BINDING_SHA256" \
      '{cluster_name: $name, plan_binding_sha256: $digest}' > "$PLAN_DIR/target.auto.tfvars.json"

    if [[ "$INTENT" == managed ]]; then check_managed_target; fi
    PLAN="$PLAN_DIR/saved.tfplan"
    run_tofu 120s init -no-color -input=false -get=false -lockfile=readonly -backend-config="path=$STATE" > "$PLAN_DIR/init.log" 2>&1
    run_tofu 180s plan -no-color -input=false -refresh=false -lock-timeout=30s -out="$PLAN" > "$PLAN_DIR/plan.log" 2>&1
    check_plan_shape >/dev/null 2>"$PLAN_DIR/show.log" || refuse 'The saved plan differs from the selected profile. See show.log.'
    run_tofu 60s show -no-color "$PLAN" > "$PLAN_DIR/plan.txt" 2>>"$PLAN_DIR/show.log"
    PLAN_SHA256=$(digest < "$PLAN")
    printf '%s  saved.tfplan\n' "$PLAN_SHA256" > "$PLAN_DIR/SHA256SUMS"
    printf '# Plan review\n\n- Target %s\n- Profile kind-baseline\n- Intent %s\n- Environment revision %s\n- Provider revision %s\n- Binding SHA-256 %s\n- Plan %s\n- SHA-256 %s\n' \
      "$TARGET" "$INTENT" "$ENV_REVISION" "$PROVIDER_REVISION" "$BINDING_SHA256" "$PLAN" "$PLAN_SHA256" > "$PLAN_DIR/.review"
    mv -T -- "$PLAN_DIR/.review" "$PLAN_DIR/REVIEW.md"
    printf '%s\n' "$PLAN_DIR/REVIEW.md"
