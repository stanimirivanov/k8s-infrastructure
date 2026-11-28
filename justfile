set positional-arguments

[private]
@default:
    {{quote(just_executable())}} --list

# Save a private OpenTofu plan for review (create or managed)
plan PROFILE INTENT: (_kind "plan" PROFILE INTENT)

# Apply the exact approved plan and verify the registered cluster
apply PROFILE INTENT ABSOLUTE_PLAN_PATH SHA256 APPROVER APPROVAL_UTC: (_kind "apply" PROFILE INTENT ABSOLUTE_PLAN_PATH SHA256 APPROVER APPROVAL_UTC)

# Resume readiness and repeat-plan verification after apply and registration
verify PROFILE ABSOLUTE_PLAN_PATH: (_kind "verify" PROFILE ABSOLUTE_PLAN_PATH)

[private]
_kind OPERATION PROFILE *ARGS:
    #!/usr/bin/env bash
    set -euo pipefail

    refuse() {
      printf '%s\n' "$1" >&2
      exit 1
    }

    OPERATION=$1
    PROFILE=$2
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

    case "$OPERATION" in
      plan) INTENT=$3 ;;
      apply)
        INTENT=$3
        PLAN=$4
        APPROVED_SHA256=$5
        APPROVED_BY=$6
        APPROVED_AT=$7
        ;;
      verify)
        INTENT=managed
        PLAN=$3
        ;;
    esac
    case "$INTENT" in
      create|managed) ;;
      *) refuse "${OPERATION^} intent must be create or managed." ;;
    esac

    check_reviewed_checkout() {
      SOURCE_REVISION=$(git rev-parse --verify 'HEAD^{commit}')
      INDEX_FLAGS=$(git ls-files -v | LC_ALL=C awk '/^[a-zS] / {print $1}')
      [[ -z "$INDEX_FLAGS" ]] ||
        refuse 'Use a clean, reviewed checkout without assume-unchanged or skip-worktree flags.'
      CHECKOUT_STATUS=$(git --no-optional-locks status --porcelain=v1 --untracked-files=all --ignore-submodules=none)
      [[ -z "$CHECKOUT_STATUS" && "$(git rev-parse --verify 'HEAD^{commit}')" == "$SOURCE_REVISION" ]] ||
        refuse 'Plan from a clean, reviewed checkout at a stable HEAD.'
    }

    if [[ "$OPERATION" == plan ]]; then
      check_reviewed_checkout
    fi

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
    BACKUP="$STATE.backup"
    PLANS="$STATE_DIR/plans"
    KCFG="$STATE_DIR/kubeconfig"
    IDENTITY="$STATE_DIR/identity.json"
    CTX="kind-$CLUSTER"
    [[ ! -L "$STATE" && ( ! -e "$STATE" || -f "$STATE" ) &&
       ! -L "$BACKUP" && ( ! -e "$BACKUP" || -f "$BACKUP" ) &&
       ! -L "$PLANS" && ( ! -e "$PLANS" || -d "$PLANS" ) ]] ||
      refuse 'The state file or plan directory is invalid or redirected.'
    [[ -f "$TF_CLI_CONFIG_FILE" && ! -L "$TF_CLI_CONFIG_FILE" ]] ||
      refuse 'The registered OpenTofu CLI configuration is missing or redirected.'

    run_tofu() {
      local deadline=$1
      shift
      timeout --verbose --kill-after=5s "$deadline" tofu -chdir="$PLAN_DIR" "$@"
    }

    run_kubectl() {
      local deadline=$1 config=$2
      shift 2
      timeout --kill-after=5s "$deadline" kubectl --kubeconfig "$config" --context "$CTX" "$@"
    }

    check_uid() {
      run_kubectl 15s "$KCFG" --request-timeout=10s \
        wait --for="jsonpath={.metadata.uid}=$EXPECTED_UID" --timeout=1s namespace/kube-system ||
        refuse 'The live kube-system UID could not be matched to the registration.'
    }

    require_vacant_registration() {
      [[ ! -e "$KCFG" && ! -L "$KCFG" && ! -e "$IDENTITY" && ! -L "$IDENTITY" ]] ||
        refuse 'The selected target already has a registration file.'
    }

    check_daemon() {
      DAEMON=$(timeout --kill-after=5s 15s docker --host "$DOCKER_HOST" info --format '{{{{.ID}}')
      [[ "$DAEMON" == "$EXPECTED_DAEMON" ]] || refuse 'Docker daemon ID differs from the registration.'
    }

    check_target_registration() {
      if [[ "$INTENT" == managed ]]; then
        [[ -f "$KCFG" && ! -L "$KCFG" && -f "$IDENTITY" && ! -L "$IDENTITY" ]] ||
          refuse 'The kubeconfig or identity record is missing or redirected.'
        EXPECTED_DAEMON=$(jq -er '.daemon | strings | select(length > 0)' "$IDENTITY") ||
          refuse 'The identity record has no valid Docker daemon ID.'
        EXPECTED_UID=$(jq -er '.uid | strings | select(length > 0)' "$IDENTITY") ||
          refuse 'The identity record has no valid kube-system UID.'
      else
        : "${EXPECTED_DAEMON:?Set the registered Docker daemon ID}"
        require_vacant_registration
      fi

      check_daemon
      if [[ "$INTENT" == create ]]; then
        CLUSTER_LABELS=$(timeout --kill-after=5s 15s docker --host "$DOCKER_HOST" ps -a \
          --filter "label=io.x-k8s.kind.cluster=$CLUSTER" --format '{{{{.Names}}')
        CLUSTER_NAMES=$(timeout --kill-after=5s 15s docker --host "$DOCKER_HOST" ps -a \
          --filter "name=^/$CLUSTER-" --format '{{{{.Names}}')
        [[ -z "$CLUSTER_LABELS$CLUSTER_NAMES" ]] || refuse 'The selected kind name is already in use.'
      fi
    }

    check_target_state() {
      if [[ "$INTENT" == managed ]]; then
        STATE_RESOURCES=$(run_tofu 60s state list -state="$STATE")
        [[ "$STATE_RESOURCES" == kind_cluster.pilot ]] || refuse 'The registered state must contain only kind_cluster.pilot.'
        NAME_MATCH=$(run_tofu 60s console -no-color -state="$STATE" \
          <<< "kind_cluster.pilot.name == var.cluster_name && var.cluster_name == \"$CLUSTER\"")
        [[ "$NAME_MATCH" == true ]] || refuse 'The state or staged cluster name differs from the registration.'
        check_uid
      else
        if [[ -e "$STATE" ]]; then
          STATE_RESOURCES=$(run_tofu 60s state list -state="$STATE")
          [[ -z "$STATE_RESOURCES" ]] || refuse 'The selected state already contains resources.'
        fi
        NAME_MATCH=$(run_tofu 60s console -no-color -state="$STATE" <<< "var.cluster_name == \"$CLUSTER\"")
        [[ "$NAME_MATCH" == true ]] || refuse 'The staged cluster name differs from the registration.'
      fi
    }

    save_plan() {
      check_target_registration
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
      run_tofu 120s init -no-color -input=false -lockfile=readonly -backend-config="path=$STATE"
      check_target_state
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
    }

    bind_saved_plan() {
      PLAN_DIR=${PLAN%/*}
      [[ "${PLAN_DIR%/*}" == "$PLANS" && "${PLAN_DIR##*/}" == plan.* &&
         "$PLAN" == "$PLAN_DIR/saved.tfplan" && -f "$PLAN" &&
         "$(realpath -e -- "$PLAN")" == "$PLAN" ]] ||
        refuse 'Use the canonical saved plan under the selected profile plans directory.'
      local input
      for input in main.tf .terraform.lock.hcl target.auto.tfvars source-revision.txt REVIEW.md; do
        [[ -f "$PLAN_DIR/$input" && ! -L "$PLAN_DIR/$input" ]] ||
          refuse 'The saved plan snapshot or review evidence is incomplete or redirected.'
      done
      if [[ "$PROFILE" == retained-434 ]]; then
        [[ -f "$PLAN_DIR/retained_override.tf" && ! -L "$PLAN_DIR/retained_override.tf" ]] ||
          refuse 'The saved plan snapshot is missing the retained lifecycle override.'
      fi
      [[ ! -L "$PLAN_DIR/provider.kubeconfig" && ! -L "$PLAN_DIR/.terraform" ]] ||
        refuse 'The saved provider paths are redirected.'

      REVIEW="$PLAN_DIR/REVIEW.md"
      grep -Fxq -- "- Profile $PROFILE" "$REVIEW" &&
        grep -Fxq -- "- Plan $PLAN" "$REVIEW" ||
        refuse 'The review record differs from the selected profile or plan.'
      export KUBECONFIG="$PLAN_DIR/provider.kubeconfig"
    }

    check_digest() {
      local recorded
      recorded=$(sha256sum < "$PLAN")
      [[ "${recorded%% *}" == "$1" ]] ||
        refuse 'The saved plan differs from the approved SHA-256.'
      grep -Fxq -- "- SHA-256 $1" "$REVIEW" ||
        refuse 'The review record differs from the approved SHA-256.'
    }

    write_apply_status() {
      local receipt
      receipt=$(mktemp "$PLAN_DIR/apply.status.XXXXXX")
      printf '%s %s\n' "$1" "$APPROVED_SHA256" > "$receipt"
      mv -T -- "$receipt" "$PLAN_DIR/apply.status"
    }

    report_failure() {
      local result=$?
      if (( result != 0 )); then
        printf 'The %s phase stopped. Private evidence is in %s. %s\n' \
          "$PHASE" "$PLAN_DIR" "$RECOVERY" >&2
      fi
    }

    apply_plan() {
      check_digest "$APPROVED_SHA256"
      grep -Fxq -- "- Intent $INTENT" "$REVIEW" ||
        refuse 'The review record differs from the selected apply intent.'
      [[ -n "${APPROVED_BY//[[:space:]]/}" && "$APPROVED_BY" != *$'\n'* && "$APPROVED_BY" != *$'\r'* ]] ||
        refuse 'Use a single-line approver name from the approval record.'
      [[ "$APPROVED_AT" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ &&
         "$(date -u -d "$APPROVED_AT" +%Y-%m-%dT%H:%M:%SZ)" == "$APPROVED_AT" ]] ||
        refuse 'Use the UTC approval time in YYYY-MM-DDTHH:MM:SSZ form.'
      [[ ! -e "$PLAN_DIR/apply.status" && ! -L "$PLAN_DIR/apply.status" &&
         ! -e "$PLAN_DIR/apply.log" && ! -L "$PLAN_DIR/apply.log" ]] ||
        refuse 'An apply was already attempted for this plan. Use the runbook recovery steps.'

      check_target_registration
      check_target_state
      run_tofu 60s show -json "$PLAN" |
        jq -e --arg cluster "$CLUSTER" '.variables.cluster_name.value == $cluster' >/dev/null ||
        refuse 'The saved plan names a different cluster.'
      PHASE=apply
      RECOVERY='Inspect apply.log, state and inventory before attempting another operation.'
      printf '\n- Approved SHA-256 %s\n- Approver %s\n- Approval UTC %s\n' \
        "$APPROVED_SHA256" "$APPROVED_BY" "$APPROVED_AT" >> "$REVIEW"
      write_apply_status started
      check_digest "$APPROVED_SHA256"
      run_tofu 300s apply -no-color -input=false -lock-timeout=30s \
        -state="$STATE" -state-out="$STATE" -backup="$BACKUP" "$PLAN" \
        > "$PLAN_DIR/apply.log" 2>&1
      write_apply_status complete
    }

    register_cluster() {
      PHASE=registration
      RECOVERY='Review the partial registration before resuming verification.'
      check_daemon
      local config="$PLAN_DIR/kubeconfig" identity="$PLAN_DIR/identity.json" api_server
      [[ ! -e "$config" && ! -L "$config" && ! -e "$identity" && ! -L "$identity" ]] ||
        refuse 'The saved plan already has staged registration files.'
      timeout --kill-after=5s 30s kind get kubeconfig --name "$CLUSTER" > "$config"
      api_server=$(run_kubectl 15s "$config" config view --minify -o 'jsonpath={.clusters[0].cluster.server}')
      [[ "$api_server" =~ ^https://127\.0\.0\.1:[0-9]+$ ]] ||
        refuse 'The new kubeconfig does not point to the loopback API.'
      EXPECTED_UID=$(run_kubectl 15s "$config" --request-timeout=10s \
        get namespace kube-system -o 'jsonpath={.metadata.uid}')
      [[ -n "$EXPECTED_UID" ]] || refuse 'The new kube-system namespace has no UID.'
      jq -n --arg daemon "$EXPECTED_DAEMON" --arg uid "$EXPECTED_UID" \
        '{daemon: $daemon, uid: $uid}' > "$identity"
      require_vacant_registration
      install -m 600 "$config" "$identity" "$STATE_DIR/"
      INTENT=managed
    }

    check_placement() {
      local selected
      selected=$(run_kubectl 15s "$KCFG" --request-timeout=10s get nodes -l "$1" -o name 2>> "$VERIFY_LOG")
      printf 'Selector %s returned %s\n' "$1" "$selected" >> "$VERIFY_LOG"
      [[ "$selected" == "node/$CLUSTER-$2" ]] ||
        refuse "Unexpected placement for node/$CLUSTER-$2. See verify.log."
    }

    verify_plan() {
      PHASE=verification
      RECOVERY='After registration is confirmed, resume verification of the same plan.'
      local VERIFY_LOG="$PLAN_DIR/verify.log" nodes evidence rc
      for evidence in "$VERIFY_LOG" "$PLAN_DIR/nodes.txt" "$PLAN_DIR/repeat.log"; do
        [[ ! -L "$evidence" && ( ! -e "$evidence" || -f "$evidence" ) ]] ||
          refuse 'A verification evidence path is redirected or invalid.'
      done
      printf '\nChecking cluster identity and nodes\n' >> "$VERIFY_LOG"
      check_target_state
      run_kubectl 100s "$KCFG" --request-timeout=90s wait --for=condition=Ready --timeout=90s \
        "node/$CLUSTER-control-plane" "node/$CLUSTER-worker" "node/$CLUSTER-worker2" >> "$VERIFY_LOG" 2>&1 ||
        refuse 'The required nodes did not all become Ready. See verify.log.'
      nodes=$(run_kubectl 15s "$KCFG" --request-timeout=10s get nodes -o name 2>> "$VERIFY_LOG" | LC_ALL=C sort)
      printf 'Observed nodes:\n%s\n' "$nodes" >> "$VERIFY_LOG"
      [[ "$nodes" == "$(printf 'node/%s\n' "$CLUSTER-control-plane" "$CLUSTER-worker" "$CLUSTER-worker2")" ]] ||
        refuse 'Unexpected node inventory. See verify.log.'
      check_placement workload=control-plane,node-role.kubernetes.io/control-plane control-plane
      check_placement workload=performance-generator,perfeng.io/node-type=generator worker
      check_placement workload=sut,perfeng.io/node-type=sut worker2
      run_kubectl 15s "$KCFG" --request-timeout=10s get nodes -L workload,perfeng.io/node-type \
        > "$PLAN_DIR/nodes.txt" 2>> "$VERIFY_LOG"
      if run_tofu 180s plan -no-color -input=false -lock-timeout=30s -state="$STATE" -detailed-exitcode \
        >> "$PLAN_DIR/repeat.log" 2>&1; then
        check_daemon
        check_uid
        printf '%s\n' 'Repeat plan reports zero changes.' | tee -a "$VERIFY_LOG"
      else
        rc=$?
        printf 'Repeat plan exited %s (2 means changes, 1 means error). See repeat.log.\n' "$rc" >&2
        exit "$rc"
      fi
    }

    check_completed_apply() {
      local receipt="$PLAN_DIR/apply.status" status
      [[ -f "$receipt" && ! -L "$receipt" ]] ||
        refuse 'The saved plan has no completed apply receipt. Use the runbook recovery steps.'
      status=$(cat -- "$receipt")
      [[ "$status" =~ ^complete\ ([0-9a-f]{64})$ ]] ||
        refuse 'The saved plan has no completed apply receipt. Use the runbook recovery steps.'
      check_digest "${BASH_REMATCH[1]}"
    }

    if [[ "$OPERATION" == plan ]]; then
      save_plan
      exit
    fi

    bind_saved_plan
    PHASE=preflight
    RECOVERY='Check the selected plan, approval and target registration.'
    trap report_failure EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    if [[ "$OPERATION" == apply ]]; then
      apply_plan
      if [[ "$INTENT" == create ]]; then
        register_cluster
      fi
    else
      PHASE=verification
      RECOVERY='Confirm that the original apply and registration completed.'
      check_completed_apply
      check_target_registration
    fi
    verify_plan
