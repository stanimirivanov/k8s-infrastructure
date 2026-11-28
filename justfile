set positional-arguments

provider_checkout := justfile_directory()

[private]
@default:
    {{quote(just_executable())}} --list

# Reserve a reviewed local kind target
register TARGET ENVIRONMENT CANDIDATE: (_kind provider_checkout "register" TARGET ENVIRONMENT CANDIDATE)

# Save a private baseline create plan for a registered kind target
plan TARGET ENVIRONMENT: (_kind provider_checkout "plan" TARGET ENVIRONMENT)

[private]
_kind PROVIDER OPERATION TARGET ENVIRONMENT *ARGS:
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
      git --no-replace-objects --literal-pathspecs -C "$checkout" -c core.fsmonitor=false "$@"
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
      jq -er '[.state, .lock, .kubeconfig, (.state + ".backup"),
        (.state | split("/") | .[-1] |= ("." + . + ".lock.info") | join("/"))]
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
        keys == ["clusterName", "context", "daemonId", "declaration", "dockerHost", "environment", "kubeconfig", "lock", "schemaVersion", "state", "target"]
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

    [[ $# -ge 4 ]] || refuse 'Select register TARGET ENVIRONMENT CANDIDATE or plan TARGET ENVIRONMENT.'
    PROVIDER=$(realpath -e -- "$1")
    OPERATION=$2
    TARGET=$3
    ENVIRONMENT=$(realpath -e -- "$4")
    [[ "$TARGET" =~ ^[a-z0-9]+(-[a-z0-9]+)*/[a-z0-9]+(-[a-z0-9]+)*$ ]] ||
      refuse 'Select an explicit owner/target key.'
    REGISTRY=${KIND_REGISTRATIONS:?Set KIND_REGISTRATIONS to the Operations registry}
    private_dir "$REGISTRY"
    outside_checkouts "$REGISTRY"
    OWNER=${TARGET%/*}
    REGISTRATION="$REGISTRY/$TARGET.json"
    case "$OPERATION:$#" in
      register:5)
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
      plan:4)
        [[ -e "$REGISTRATION" || -L "$REGISTRATION" ]] || refuse 'No registration exists for this target.'
        private_dir "$REGISTRY/$OWNER"
        INPUT=$(load_registration "$REGISTRATION" "$TARGET")
        [[ "$(jq -r .environment <<< "$INPUT")" == "$ENVIRONMENT" ]] ||
          refuse 'Use the environment checkout originally selected for this registration.'
        LOCK=$(jq -r .lock <<< "$INPUT")
        exec 9>>"$LOCK"
        flock -w 15 9 || refuse 'The target lock is busy.'
        [[ "$(load_registration "$REGISTRATION" "$TARGET")" == "$INPUT" ]] ||
          refuse 'The registration changed while acquiring the target lock.'
        STATE=$(jq -r .state <<< "$INPUT")
        ;;
      *) refuse 'Select register TARGET ENVIRONMENT CANDIDATE or plan TARGET ENVIRONMENT.' ;;
    esac

    DOCKER_HOST=$(jq -r .dockerHost <<< "$INPUT")
    DECLARATION_PATH=$(jq -r .declaration <<< "$INPUT")
    relative_json_path='^([A-Za-z0-9_-][A-Za-z0-9_.-]*/)*[A-Za-z0-9_-][A-Za-z0-9_.-]*\.json$'
    [[ "$DECLARATION_PATH" =~ $relative_json_path ]] || refuse 'Select a repository-relative JSON declaration path.'
    [[ "$DOCKER_HOST" =~ ^unix:///[^[:space:][:cntrl:]]+$ ]] ||
      refuse 'Use an explicit local Unix Docker socket.'
    unset DOCKER_CONTEXT DOCKER_TLS DOCKER_TLS_VERIFY DOCKER_CERT_PATH

    ENV_REVISION=$(clean_revision)
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
    MODULE=clusters/local-kind/kind-baseline
    # Check both inputs before reserving an unusable profile or querying Docker.
    for file in main.tf .terraform.lock.hcl; do
      stage_blob "$PROVIDER" "$PROVIDER_REVISION" "$MODULE/$file" >/dev/null
    done
    [[ "$(clean_revision)" == "$ENV_REVISION" ]] || refuse 'The environment changed while staging its inputs.'
    DAEMON=$(observe_daemon "$DOCKER_HOST")

    if [[ "$OPERATION" == register ]]; then
      PROPOSED=$(jq -cS --arg environment "$ENVIRONMENT" --arg cluster "$CLUSTER" --arg daemon "$DAEMON" --arg state "$STATE" \
        --arg lock "$LOCK" --arg kubeconfig "$KUBECONFIG_PATH" '
        . + {schemaVersion: 1, clusterName: $cluster, context: ("kind-" + $cluster),
          daemonId: $daemon, environment: $environment, state: $state, lock: $lock, kubeconfig: $kubeconfig}
        ' <<< "$INPUT")
      PROPOSED_PATHS=$(record_paths "$PROPOSED")
      while IFS= read -r path; do canonical_private_path "$path" >/dev/null; done <<< "$PROPOSED_PATHS"

      directory_lock="$REGISTRY/.registry.lock"
      if [[ -e "$directory_lock" || -L "$directory_lock" ]]; then private_file "$directory_lock"; fi
      exec 8>>"$directory_lock"
      flock -w 15 8 || refuse 'The registration directory lock is busy.'

      shopt -s nullglob
      for owner_dir in "$REGISTRY"/*/; do
        owner_dir=${owner_dir%/}
        private_dir "$owner_dir"
        for file in "$owner_dir"/*.json; do
          key=${file#"$REGISTRY/"}
          row=$(load_registration "$file" "${key%.json}")
          if [[ "$file" == "$REGISTRATION" ]]; then
            [[ "$(jq -cS . <<< "$row")" == "$PROPOSED" ]] || refuse 'The target key already has a different registration.'
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
    paths=$(record_paths "$INPUT")
    while IFS= read -r path; do
      [[ "$path" == "$LOCK" || ( ! -e "$path" && ! -L "$path" ) ]] ||
        refuse 'A create plan requires vacant state, kubeconfig and backend sidecar paths.'
    done <<< "$paths"
    for filter in "label=io.x-k8s.kind.cluster=$CLUSTER" "name=^/$CLUSTER-"; do
      containers=$(timeout --kill-after=5s 15s docker --host "$DOCKER_HOST" ps -a \
        --filter "$filter" --format '{{{{.Names}}')
      [[ -z "$containers" ]] || refuse 'The reserved kind name is already in use.'
    done
    TOFU_CONFIG=${KIND_TOFU_CONFIG:?Set KIND_TOFU_CONFIG to the private OpenTofu CLI configuration}
    private_file "$TOFU_CONFIG"
    [[ "$(realpath -e -- "$TOFU_CONFIG")" == "$TOFU_CONFIG" ]] || refuse 'Use the canonical OpenTofu CLI configuration path.'
    outside_checkouts "$TOFU_CONFIG"

    for directory in "$REGISTRY/.plans" "$REGISTRY/.plans/$OWNER" "$REGISTRY/.plans/$TARGET"; do
      mkdir -p -- "$directory"
      private_dir "$directory"
    done
    PLAN_DIR=$(mktemp -d "$REGISTRY/.plans/$TARGET/plan.XXXXXX")
    trap 'if (( $? != 0 )); then printf "Planning stopped. Private diagnostics are in %s\n" "$PLAN_DIR" >&2; fi' EXIT
    for file in main.tf .terraform.lock.hcl; do
      stage_blob "$PROVIDER" "$PROVIDER_REVISION" "$MODULE/$file" > "$PLAN_DIR/$file"
    done
    stage_blob "$ENVIRONMENT" "$ENV_REVISION" "$DECLARATION_PATH" > "$PLAN_DIR/declaration.json"
    stage_blob "$ENVIRONMENT" "$ENV_REVISION" "$PIN_PATH" > "$PLAN_DIR/provider-pin.json"
    cp -- "$REGISTRATION" "$PLAN_DIR/registration.json"
    jq -nS --arg target "$TARGET" --arg environment "$ENV_REVISION" --arg provider "$PROVIDER_REVISION" \
      --arg registration "$(digest < "$PLAN_DIR/registration.json")" \
      --arg declaration "$(digest < "$PLAN_DIR/declaration.json")" \
      --arg pin "$(digest < "$PLAN_DIR/provider-pin.json")" \
      '{schemaVersion: 1, target: $target, profile: "kind-baseline", intent: "create",
        environmentRevision: $environment, providerRevision: $provider, registrationSha256: $registration,
        declarationSha256: $declaration, providerPinSha256: $pin}' > "$PLAN_DIR/binding.json"
    BINDING_SHA256=$(digest < "$PLAN_DIR/binding.json")
    jq -n --arg name "$CLUSTER" --arg digest "$BINDING_SHA256" \
      '{cluster_name: $name, plan_binding_sha256: $digest}' > "$PLAN_DIR/target.auto.tfvars.json"

    run_tofu() {
      local deadline=$1
      shift
      timeout --verbose --kill-after=5s "$deadline" env -i PATH="$PATH" HOME="$PLAN_DIR" \
        DOCKER_HOST="$DOCKER_HOST" KIND_EXPERIMENTAL_PROVIDER=docker TF_CLI_CONFIG_FILE="$TOFU_CONFIG" \
        TF_WORKSPACE=default CHECKPOINT_DISABLE=1 KUBECONFIG="$PLAN_DIR/provider.kubeconfig" \
        tofu -chdir="$PLAN_DIR" "$@"
    }
    PLAN="$PLAN_DIR/saved.tfplan"
    run_tofu 120s init -no-color -input=false -lockfile=readonly -backend-config="path=$STATE" > "$PLAN_DIR/init.log" 2>&1
    run_tofu 180s plan -no-color -input=false -refresh=false -lock-timeout=30s -out="$PLAN" > "$PLAN_DIR/plan.log" 2>&1
    run_tofu 60s show -json "$PLAN" 2>"$PLAN_DIR/show.log" |
      jq -e --arg cluster "$CLUSTER" --arg binding "$BINDING_SHA256" '
        .variables.cluster_name.value == $cluster and .variables.plan_binding_sha256.value == $binding
        and (.resource_changes | length == 1 and (.[0] |
          .address == "kind_cluster.baseline" and .provider_name == "registry.terraform.io/elioseverojunior/kind"
          and .change.actions == ["create"] and .change.after.name == $cluster))
      ' >/dev/null || refuse 'OpenTofu did not save the expected bound baseline create plan.'
    run_tofu 60s show -no-color "$PLAN" > "$PLAN_DIR/plan.txt" 2>>"$PLAN_DIR/show.log"
    PLAN_SHA256=$(digest < "$PLAN")
    printf '%s  saved.tfplan\n' "$PLAN_SHA256" > "$PLAN_DIR/SHA256SUMS"
    printf '# Plan review\n\n- Target %s\n- Profile kind-baseline\n- Intent create\n- Environment revision %s\n- Provider revision %s\n- Binding SHA-256 %s\n- Plan %s\n- SHA-256 %s\n' \
      "$TARGET" "$ENV_REVISION" "$PROVIDER_REVISION" "$BINDING_SHA256" "$PLAN" "$PLAN_SHA256" > "$PLAN_DIR/.review"
    mv -T -- "$PLAN_DIR/.review" "$PLAN_DIR/REVIEW.md"
    printf '%s\n' "$PLAN_DIR/REVIEW.md"
